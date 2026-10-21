"""Настоящий MCP-сервер на официальном SDK. Один набор tools, два транспорта.

Все инструменты асинхронные: работа с SQLite уходит в поток (asyncio.to_thread),
поэтому долгий запрос одного чата не останавливает сервер для остальных.
"""
import argparse
import asyncio
import logging

from mcp.server.fastmcp import FastMCP

from .composition import Condition
from .config import Settings, get_settings
from .service import GraphService, ToolResult


def create_server(settings: Settings | None = None) -> FastMCP:
    settings = settings or get_settings()
    service = GraphService(settings)
    mcp = FastMCP("IACPaaS Materials", host=settings.mcp_host, port=settings.mcp_port,
                  stateless_http=True, json_response=True)

    @mcp.tool()
    async def list_inforesources(filter: str = "", limit: int = 20) -> ToolResult:
        """Список разрешённых баз и онтологий. Это метаданные, не сведения о свойствах материалов."""
        return await asyncio.to_thread(service.list_inforesources, filter, limit)

    @mcp.tool()
    async def get_inforesource(resource_id: str) -> ToolResult:
        """Метаданные ресурса прямо с IACPaaS: root, онтология, дата изменения, размер графа."""
        return await service.get_inforesource(resource_id)

    @mcp.tool()
    async def search_graph(query: str, resource_id: str | None = None, limit: int = 8, offset: int = 0) -> ToolResult:
        """Лексический поиск по локальному индексу данных IACPaaS (у платформы нет поиска по тексту).
        Ищите короткими терминами/марками. Возвращает node_id, фрагменты, total и has_more.
        limit — до 20 (по умолчанию 8). Поиск идёт по полной копии ресурсов, поэтому has_more: false
        значит «найдено всё по этому запросу»; при has_more продолжайте с next_offset.
        Отсутствие совпадения относится только к указанному охвату и состоянию индекса.
        """
        return await asyncio.to_thread(service.search_graph, query, resource_id, limit, offset)

    @mcp.tool()
    async def find_materials(conditions: list[Condition] | None = None, material_class: str | None = None,
                             sort_by: str | None = None, order: str = "desc", limit: int = 20,
                             offset: int = 0) -> ToolResult:
        """Подбор марок справочника по химическому составу: пороги («больше/меньше X %»), топ,
        максимум или минимум по элементу (sort_by + limit), есть или нет элемента, класс материалов.
        Количество подходящих марок — total. Все conditions соединяются через «И»; для «или»
        делайте несколько вызовов.
        Сравнение гарантированное: «>» X — нижняя граница диапазона больше X, «<» X — верхняя меньше X
        («не более 0.5» — это диапазон [0; 0.5]). op present — элемент перечислен в любой форме,
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

    @mcp.tool()
    async def get_subgraph(resource_id: str, root: str, depth: int = 3, max_nodes: int = 80, offset: int = 0) -> ToolResult:
        """Прочитать вершину и её потомков прямо с IACPaaS (при недоступности — из индекса).
        root берите из результатов инструментов, не угадывайте. depth до 8, max_nodes до 150.
        Запись целиком (марку со всем составом и свойствами, свойство из базы свойств) читайте
        ОДНИМ вызовом от вершины записи с depth 8 и max_nodes 150: числа лежат на нижних уровнях
        записи, при меньшей глубине их не будет. Для обзора класса или раздела хватит depth 1.
        children_not_loaded у вершины — сколько её потомков не попало в выдачу. Нет этого поля —
        все потомки вершины в выдаче или их нет: перечитывать её не нужно.
        При has_more используйте next_offset или уточните корень.
        """
        return await service.get_subgraph(resource_id, root, depth, max_nodes, offset)

    @mcp.tool()
    async def count_graph(resource_id: str, root: str | None = None) -> ToolResult:
        """Точное число уникальных вершин и дуг графа или подграфа (по индексу), без раскрытия внешних ссылок."""
        return await asyncio.to_thread(service.count_graph, resource_id, root)

    @mcp.tool()
    async def validate_resource(resource_id: str) -> ToolResult:
        """Базовая проверка структуры индекса; не выполняет предметные вычисления и не меняет ресурс."""
        return await asyncio.to_thread(service.validate_resource, resource_id)

    return mcp


def main():
    parser = argparse.ArgumentParser(description="MCP-адаптер чтения IACPaaS")
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
