"""Настоящий MCP-сервер на официальном SDK. Один набор tools, два транспорта.

Все инструменты асинхронные: работа с SQLite уходит в поток (asyncio.to_thread),
поэтому долгий запрос одного чата не останавливает сервер для остальных.

Записывающие инструменты (WRITE_ENABLED=true) перед записью запрашивают у пользователя
подтверждение через MCP elicitation. По HTTP это работает только в режиме с сессией:
запрос подтверждения идёт от сервера к клиенту посреди вызова инструмента.
"""
import argparse
import asyncio
import logging
from typing import Literal

from mcp import types
from mcp.server.fastmcp import Context, FastMCP
from pydantic import BaseModel, Field

from .composition import Condition
from .config import Settings, get_settings
from .service import GraphService, ToolResult, fail
from .writes import WriteService

log = logging.getLogger(__name__)
READ_ONLY = types.ToolAnnotations(readOnlyHint=True, openWorldHint=True)
# По определению MCP destructiveHint=false значит «только добавляет»; изменение вершины меняет существующее.
WRITES = types.ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True)


class Item(BaseModel):
    """Вершина нового ресурса: понятие онтологии и имя (для вершины) или значение (для значения)."""
    concept: str = Field(description="Имя понятия онтологии, как в structure из get_inforesource")
    name: str | None = Field(default=None, description="Имя вершины; для значений не указывается")
    value: str | float | None = Field(default=None, description="Значение (REAL — число, STRING — строка)")
    comment: str | None = None
    children: list["Item"] = Field(default_factory=list)


class CreateFolder(BaseModel):
    operation: Literal["create_folder"]
    parent_path: str = Field(default="", description="Родительская папка в «Моём фонде»; пусто — корень фонда")
    name: str
    description: str = ""


class CreateResource(BaseModel):
    operation: Literal["create_resource"]
    folder_path: str = Field(description="Папка в «Моём фонде», куда положить ресурс")
    name: str
    ontology: Literal["materials_ontology", "properties_ontology", "characteristics_ontology"]
    comment: str = ""
    items: list[Item] = Field(default_factory=list, description="Дерево вершин по структуре онтологии")


class UpdateConcept(BaseModel):
    operation: Literal["update_concept"]
    path: str = Field(description="Путь ресурса в «Моём фонде»")
    concept_path: str = Field(description="concept_path вершины из get_subgraph(path=…)")
    name: str | None = Field(default=None, description="Новое имя вершины")
    value: str | float | None = Field(default=None, description="Новое значение (для вершин-значений)")
    comment: str | None = Field(default=None, description="Новый комментарий")


class ConfirmWrite(BaseModel):
    confirm: bool = Field(default=True, description="Подтвердить запись в IACPaaS")


def create_server(settings: Settings | None = None) -> FastMCP:
    settings = settings or get_settings()
    service = GraphService(settings)
    writes = WriteService(service)
    # Режим с сессией и потоком SSE: без него ctx.elicit не доходит до клиента (проверено на mcp 1.30).
    mcp = FastMCP("IACPaaS Materials", host=settings.mcp_host, port=settings.mcp_port,
                  stateless_http=False, json_response=False)
    mcp.graph_service = service  # для тестов: подмена клиента платформы

    async def confirmation(ctx: Context, message: str) -> ToolResult | None:
        """None — пользователь явно подтвердил; иначе отказ. Решает только человек в клиенте MCP."""
        wanted = types.ClientCapabilities(elicitation=types.ElicitationCapability())
        if not ctx.session.check_client_capability(wanted):
            return fail("CONFIRMATION_UNAVAILABLE", "Клиент MCP не умеет запрашивать подтверждение у пользователя "
                        "(elicitation), поэтому запись невозможна.")
        timeout = settings.write_confirm_timeout_seconds
        try:
            answer = await asyncio.wait_for(ctx.elicit(message, ConfirmWrite), timeout)
        except asyncio.TimeoutError:
            return fail("CONFIRMATION_TIMEOUT", f"Пользователь не ответил за {timeout:g} с — запись отменена.")
        except Exception:
            log.exception("Запрос подтверждения не удался")
            return fail("CONFIRMATION_UNAVAILABLE", "Не удалось запросить подтверждение у пользователя — запись отменена.")
        if answer.action == "accept" and answer.data is not None and answer.data.confirm:
            return None
        return fail("CONFIRMATION_DECLINED", "Пользователь отменил запись. Не повторяйте её без новой просьбы.")

    @mcp.tool(annotations=READ_ONLY)
    async def list_inforesources(filter: str = "", limit: int = 20) -> ToolResult:
        """Список разрешённых баз и онтологий. Это метаданные, не сведения о свойствах материалов."""
        return await asyncio.to_thread(service.list_inforesources, filter, limit)

    if settings.write_enabled:
        @mcp.tool(annotations=READ_ONLY)
        async def get_inforesource(resource_id: str | None = None, path: str | None = None) -> ToolResult:
            """Метаданные ресурса прямо с IACPaaS: root, онтология, дата изменения, размер графа.
            resource_id — базовый ресурс из list_inforesources; path — любой ресурс «Моего фонда»
            («Демо агента / Мои свойства»), живое чтение. Для онтологий проекта (materials_ontology,
            properties_ontology, characteristics_ontology) в data.structure — дерево понятий: читайте его
            перед create_resource.
            """
            if path:
                return await writes.read_resource(path)
            if not resource_id:
                return fail("INVALID_ARGUMENTS", "Укажите resource_id или path.")
            return await service.get_inforesource(resource_id)
    else:
        @mcp.tool(name="get_inforesource", annotations=READ_ONLY)
        async def get_inforesource_readonly(resource_id: str) -> ToolResult:
            """Метаданные ресурса прямо с IACPaaS: root, онтология, дата изменения, размер графа."""
            return await service.get_inforesource(resource_id)

    @mcp.tool(annotations=READ_ONLY)
    async def search_graph(query: str, resource_id: str | None = None, limit: int = 8, offset: int = 0) -> ToolResult:
        """Лексический поиск по локальному индексу данных IACPaaS (у платформы нет поиска по тексту).
        Ищите короткими терминами/марками. Возвращает node_id, фрагменты, total и has_more.
        limit — до 20 (по умолчанию 8). Поиск идёт по полной копии ресурсов, поэтому has_more: false
        значит «найдено всё по этому запросу»; при has_more продолжайте с next_offset.
        Отсутствие совпадения относится только к указанному охвату и состоянию индекса.
        """
        return await asyncio.to_thread(service.search_graph, query, resource_id, limit, offset)

    @mcp.tool(annotations=READ_ONLY)
    async def find_materials(conditions: list[Condition] | None = None, material_class: str | None = None,
                             sort_by: str | None = None, order: str = "desc", limit: int = 20,
                             offset: int = 0) -> ToolResult:
        """Подбор марок справочника по химическому составу: пороги («больше/меньше X %»), топ,
        максимум или минимум по элементу (sort_by + limit), есть или нет элемента, класс материалов.
        Количество подходящих марок — total. Все conditions соединяются через «И»; для «или»
        делайте несколько вызовов.
        Сравнение гарантированное: «>» X — нижняя граница диапазона больше X, «<» X — верхняя меньше X
        («не более 0.5» — это диапазон [0; 0.5]). Основа сплава, записанная «не более X», — остаток
        до 100 %: нижняя граница = 100 % минус верхние границы остальных элементов (форма «Основа (остаток)»),
        поэтому «железа больше 5 %» находит стали. op present — элемент перечислен в любой форме,
        включая «не более»; гарантированное присутствие — op ">" и value 0.
        include_unlisted у «<» и «<=» добавляет марки, в составе которых элемент не указан
        («где никеля меньше 1 %»). sort_by подразумевает, что элемент в составе есть: sort_by по тому же
        элементу, что и «<»/«<=» с include_unlisted, отбросит марки без него; для «меньше всего X, включая
        марки без X» sort_by по этому элементу не ставьте.
        elements_not_in_compositions — элементы, которые не встречаются ни в одном составе.
        Источник — локальный снимок (indexed_at). Запись марки читается через
        get_subgraph(resource_id="materials", root=node_id, depth=8, max_nodes=150).
        limit 1–50; при has_more продолжайте с next_offset.
        """
        return await asyncio.to_thread(
            service.find_materials, [item.model_dump() for item in conditions or []],
            material_class, sort_by, order, limit, offset)

    subgraph_doc = """Прочитать вершину и её потомков прямо с IACPaaS (при недоступности — из индекса).
        root берите из результатов инструментов, не угадывайте. depth до 8, max_nodes до 150.
        Запись целиком (марку со всем составом и свойствами, свойство из базы свойств) читайте
        ОДНИМ вызовом от вершины записи с depth 8 и max_nodes 150: числа лежат на нижних уровнях
        записи, при меньшей глубине их не будет. Для обзора класса или раздела хватит depth 1.
        children_not_loaded у вершины — сколько её потомков не попало в выдачу. Нет этого поля —
        все потомки вершины в выдаче или их нет: перечитывать её не нужно.
        При has_more используйте next_offset или уточните корень.
        """
    if settings.write_enabled:
        @mcp.tool(annotations=READ_ONLY, description=subgraph_doc + """
        path — любой ресурс «Моего фонда» («Демо агента / Мои свойства»): только живое чтение, без индекса;
        тогда root — concept_path вершины (пусто — корень ресурса), у каждой вершины в выдаче есть concept_path
        для update_concept.""")
        async def get_subgraph(resource_id: str | None = None, root: str | None = None, path: str | None = None,
                               depth: int = 3, max_nodes: int = 80, offset: int = 0) -> ToolResult:
            if path:
                return await writes.read_subgraph(path, root, depth, max_nodes, offset)
            if not resource_id or not root:
                return fail("INVALID_ARGUMENTS", "Укажите resource_id и root (node_id) либо path ресурса «Моего фонда».")
            return await service.get_subgraph(resource_id, root, depth, max_nodes, offset)
    else:
        @mcp.tool(name="get_subgraph", annotations=READ_ONLY, description=subgraph_doc)
        async def get_subgraph_readonly(resource_id: str, root: str, depth: int = 3, max_nodes: int = 80,
                                        offset: int = 0) -> ToolResult:
            return await service.get_subgraph(resource_id, root, depth, max_nodes, offset)

    @mcp.tool(annotations=READ_ONLY)
    async def count_graph(resource_id: str, root: str | None = None, concept: str | None = None) -> ToolResult:
        """Точное число уникальных вершин и дуг графа или подграфа (по индексу), без раскрытия внешних ссылок.
        concept — имя понятия онтологии (meta вершин): «Свойство», «Класс свойств», «Химический элемент»,
        «Материал», «Класс материалов», «Термин»… Тогда count — число вершин этого понятия, names — их имена
        (до 50). Так «сколько свойств в базе» или «сколько химических элементов» отвечается одним вызовом;
        вершины по одной не пересчитывайте. Неизвестное понятие — ошибка со списком понятий ресурса.
        """
        return await asyncio.to_thread(service.count_graph, resource_id, root, concept)

    @mcp.tool(annotations=READ_ONLY)
    async def validate_resource(resource_id: str) -> ToolResult:
        """Базовая проверка структуры индекса; не выполняет предметные вычисления и не меняет ресурс."""
        return await asyncio.to_thread(service.validate_resource, resource_id)

    if not settings.write_enabled:
        return mcp

    @mcp.tool(annotations=WRITES)
    async def write_inforesource(request: CreateFolder | CreateResource | UpdateConcept, ctx: Context) -> ToolResult:
        """Создать или изменить данные в «Моём фонде» IACPaaS. Писать можно в любом месте фонда, кроме
        защищённых ресурсов (базовые ресурсы проекта и папка «Загрузки» — отказ PROTECTED_RESOURCE).
        Каждую запись пользователь подтверждает сам в интерфейсе; отмена — CONFIRMATION_DECLINED.
        request.operation:
        - create_folder: parent_path (пусто — корень фонда), name.
        - create_resource: folder_path, name, ontology и items — дерево {concept, name | value, comment, children}.
          Сначала прочитайте get_inforesource(resource_id=<онтология>) и берите понятия из data.structure;
          JSON платформы собирает сервер.
        - update_concept: path ресурса, concept_path вершины (из get_subgraph(path=…)), новое name или value,
          comment. Вершины берите из результатов чтения, не угадывайте.
        """
        return await writes.write(request.model_dump(exclude_none=True), lambda message, danger: confirmation(ctx, message))

    @mcp.tool(annotations=WRITES)
    async def delete_inforesource(path: str, ctx: Context, mode: Literal["trash", "delete"] = "trash") -> ToolResult:
        """Удалить ресурс «Моего фонда»: mode trash — в корзину (по умолчанию), delete — безвозвратно.
        Защищённые ресурсы (базовые, папка «Загрузки») удалить нельзя. Удаление подтверждает пользователь."""
        return await writes.delete(path, mode, lambda message, danger: confirmation(ctx, message))

    return mcp


def main():
    parser = argparse.ArgumentParser(description="MCP-адаптер IACPaaS: чтение и подтверждаемая запись")
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="streamable-http")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()
    settings = get_settings()
    if args.host:
        settings.mcp_host = args.host
    if args.port:
        settings.mcp_port = args.port
    # stdout зарезервирован под JSON-RPC при stdio.
    logging.basicConfig(level=logging.WARNING)
    create_server(settings).run(transport=args.transport)


if __name__ == "__main__":
    main()
