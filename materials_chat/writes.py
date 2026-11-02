"""Запись в «Мой фонд» IACPaaS и чтение любого ресурса фонда по пути.

Порядок каждой записи: разбор пути → защита (базовые ресурсы, «Загрузки», PROTECTED_PATHS) →
проверка аргументов по онтологии и чтение ресурса → подтверждение пользователя → запрос к платформе.
Защищённый путь отклоняется раньше подтверждения и любого обращения к платформе.

Метод изменения вершины платформы (PUT /api/data/update) не работает («Неожиданное значение
параметра action: updateVertice», проверено 02.11.2026), поэтому update_concept меняет вершину
переимпортом: свежая выгрузка ресурса → правка вершины → импорт с очисткой (тот же ресурс).
"""
import asyncio
import copy
import logging
import time
from collections import deque
from typing import Awaitable, Callable

from .fund_paths import Protection, join_path, segment_key, split_path
from .graph_store import page_of_subgraph
from .ontology import ONTOLOGIES, Ontology, OntologyError, convert_value, describe_graph, node_label
from .platform import PlatformError, concept_path, utc_now
from .service import MAX_DEPTH, GraphService, ToolResult, fail

log = logging.getLogger(__name__)

# Текст подтверждения и признак опасной операции → None (подтверждено) или отказ.
Confirm = Callable[[str, bool], Awaitable[ToolResult | None]]
DELETE_MODES = {"trash": ("trashIR", "Удалить в корзину"), "delete": ("deleteIR", "Удалить безвозвратно")}


class WriteError(Exception):
    def __init__(self, result: ToolResult):
        super().__init__(result.error.message if result.error else "")
        self.result = result


def refuse(code: str, message: str) -> WriteError:
    return WriteError(fail(code, message))


class WriteService:
    def __init__(self, graph: GraphService):
        self.graph = graph
        self.settings = graph.settings
        self.protection = Protection(graph.definitions, graph.settings.protected_paths)

    @property
    def platform(self):
        return self.graph.client

    # ---------- Общие проверки ----------

    @staticmethod
    def _segments(text, field: str, allow_empty: bool = False) -> list[str]:
        if not isinstance(text, str):
            raise refuse("INVALID_ARGUMENTS", f"{field}: нужна строка — путь в «Моём фонде».")
        try:
            segments = split_path(text)
        except ValueError as error:
            raise refuse("INVALID_ARGUMENTS", f"{field}: {error}") from None
        if not segments and not allow_empty:
            raise refuse("INVALID_ARGUMENTS", f"{field}: укажите путь в «Моём фонде», например «Демо агента / Мои свойства».")
        return segments

    def _guard(self, *paths: list[str]):
        for segments in paths:
            rule = self.protection.rule(segments)
            if rule:
                raise refuse("PROTECTED_RESOURCE", f"Путь «{join_path(segments)}» защищён от записи: {rule}. "
                             "Базовые ресурсы проекта и папку «Загрузки» изменять нельзя.")

    @staticmethod
    def _name(value, field: str = "name") -> str:
        name = " ".join(str(value or "").split())
        if not name:
            raise refuse("INVALID_ARGUMENTS", f"Укажите {field}.")
        if "/" in name or "‴" in name:
            raise refuse("INVALID_ARGUMENTS", f"{field} не должен содержать «/».")
        return name

    @staticmethod
    def _place(folder: list[str]) -> str:
        return f"«{join_path(folder)}»" if folder else "корне фонда"

    async def _exists(self, path: str) -> bool:
        try:
            await self.platform.export(path, depth=0)
            return True
        except PlatformError as error:
            if error.code in ("PLATFORM_REJECTED", "RESOURCE_NOT_FOUND"):
                return False
            raise

    def _forget(self, path: str):
        """Сброс короткого кэша живого чтения по этому ресурсу после записи."""
        try:
            key = segment_key(join_path(split_path(path)))
        except ValueError:
            return
        for cached in [item for item in self.graph._live_cache if item[:2] == ("path", key)]:
            self.graph._live_cache.pop(cached, None)

    async def _run(self, operation: str, path: str, plan) -> ToolResult:
        try:
            result = await plan()
        except WriteError as error:
            result = error.result
        except PlatformError as error:
            result = fail(error.code, error.message, error.retryable)
        if result.ok:
            self._forget(path)
        log.warning("Запись IACPaaS: %s «%s» → %s", operation, path, "успех" if result.ok else result.error.code)
        return result

    @staticmethod
    def _done(operation: str, path: str, platform: dict, **data) -> ToolResult:
        return ToolResult(ok=True, data={"operation": operation, "path": path, **data, "platform": platform},
                          meta={"served_from": "iacpaas_live", "written_at": utc_now()})

    # ---------- Инструменты ----------

    async def write(self, request: dict, confirm: Confirm) -> ToolResult:
        operation = request.get("operation") if isinstance(request, dict) else None
        handler = {"create_folder": self._create_folder, "create_resource": self._create_resource,
                   "update_concept": self._update_concept}.get(operation)
        if handler is None:
            return fail("INVALID_ARGUMENTS", "operation: create_folder, create_resource или update_concept.")
        target = str(request.get("path") or request.get("folder_path") or request.get("parent_path") or "")
        return await self._run(operation, target, lambda: handler(request, confirm))

    async def delete(self, path, mode: str, confirm: Confirm) -> ToolResult:
        async def plan():
            segments = self._segments(path, "path")
            self._guard(segments)
            if mode not in DELETE_MODES:
                raise refuse("INVALID_ARGUMENTS", "mode: trash (в корзину) или delete (безвозвратно).")
            target = join_path(segments)
            if not await self._exists(target):
                raise refuse("NODE_NOT_FOUND", f"Ресурс «{target}» не найден в «Моём фонде». Удалять можно только ресурсы, не папки.")
            platform_mode, verb = DELETE_MODES[mode]
            denied = await confirm(f"{verb} ресурс «{target}».", True)
            if denied:
                return denied
            return self._done("delete", target, await self.platform.delete_resource(target, platform_mode), mode=mode)
        return await self._run("delete_" + str(mode), str(path), plan)

    async def _create_folder(self, request: dict, confirm: Confirm) -> ToolResult:
        parent = self._segments(request.get("parent_path") or "", "parent_path", allow_empty=True)
        name = self._name(request.get("name"))
        self._guard(parent, parent + [name])
        denied = await confirm(f"Создать папку «{name}» в {self._place(parent)}.", False)
        if denied:
            return denied
        result = await self.platform.create_folder(join_path(parent), name, str(request.get("description") or ""))
        return self._done("create_folder", join_path(parent + [name]), result)

    async def _create_resource(self, request: dict, confirm: Confirm) -> ToolResult:
        folder = self._segments(request.get("folder_path") or "", "folder_path", allow_empty=True)
        name = self._name(request.get("name"))
        self._guard(folder, folder + [name])
        if request.get("ontology") not in ONTOLOGIES:
            raise refuse("INVALID_ARGUMENTS", "ontology: " + ", ".join(ONTOLOGIES) + " — ресурсы создаются по онтологиям проекта.")
        try:
            ontology = await asyncio.to_thread(Ontology.load, self.graph.store, self.graph.definitions, request["ontology"])
            graph = ontology.build(name, str(request.get("comment") or "").strip(), request.get("items") or [])
        except OntologyError as error:
            raise refuse(error.code, error.message) from None
        target = join_path(folder + [name])
        if await self._exists(target):
            raise refuse("INVALID_ARGUMENTS", f"В «{join_path(folder) or 'корне фонда'}» уже есть ресурс «{name}». "
                         "Выберите другое имя или измените существующий через update_concept.")
        denied = await confirm(f"Создать ресурс «{name}» в {self._place(folder)} по онтологии «{ontology.title}»: "
                               f"{describe_graph(graph)}.", False)
        if denied:
            return denied
        result = await self.platform.import_resource(join_path(folder), graph, clear=False)
        return self._done("create_resource", target, result, ontology=request["ontology"])

    async def _update_concept(self, request: dict, confirm: Confirm) -> ToolResult:
        segments = self._segments(request.get("path"), "path")
        self._guard(segments)
        labels = self._segments(request.get("concept_path") or "", "concept_path", allow_empty=True)
        if not labels:
            raise refuse("INVALID_ARGUMENTS", "concept_path: путь вершины внутри ресурса (берите из get_subgraph); "
                         "переименовать сам ресурс нельзя.")
        changes = {key: request[key] for key in ("name", "value", "comment") if request.get(key) not in (None, "")}
        if not changes:
            raise refuse("INVALID_ARGUMENTS", "Укажите, что изменить: name, value или comment.")
        target = join_path(segments)
        original = await self.platform.export(target)
        if count_nodes(original) > self.settings.live_max_nodes:
            raise refuse("UNSUPPORTED_ONTOLOGY_FEATURE", f"Ресурс больше {self.settings.live_max_nodes} вершин: "
                         "изменение переимпортом для него не выполняется.")
        vertex = find_vertex(original, labels)
        if vertex.get("original") or vertex.get("link"):
            raise refuse("FOREIGN_CONCEPT", "Вершина — ссылка или клон вершины другого ресурса; менять её можно только в исходном ресурсе.")
        if has_inner_references(original):
            raise refuse("UNSUPPORTED_ONTOLOGY_FEATURE", "В ресурсе есть ссылки на его же вершины: после переименования "
                         "они бы сломались, поэтому изменение переимпортом для него не выполняется.")
        updated = copy.deepcopy(original)
        changed = find_vertex(updated, labels)
        before = node_label(vertex)
        try:
            if "value" in changes or (vertex.get("type") == "ТЕРМИНАЛ-ЗНАЧЕНИЕ" and "name" in changes):
                if vertex.get("type") != "ТЕРМИНАЛ-ЗНАЧЕНИЕ":
                    raise refuse("INVALID_ARGUMENTS", f"«{before}» — вершина: меняйте name, а не value.")
                changed["value"] = convert_value(str(vertex.get("meta", "")), str(vertex.get("valtype", "")),
                                                 changes.get("value", changes.get("name")))
            elif "name" in changes:
                changed["name"] = self._name(changes["name"])
        except OntologyError as error:
            raise refuse(error.code, error.message) from None
        if "comment" in changes:
            changed["comment"] = str(changes["comment"]).strip()
        new_label = str(changed["value"]) if "value" in changed else str(changed.get("name", ""))
        old_label = str(vertex["value"]) if "value" in vertex else str(vertex.get("name", ""))
        parts = []
        if new_label != old_label:
            parts.append(f"переименовать «{old_label}» ({vertex.get('meta')}) → «{new_label}»")
        if "comment" in changes:
            parts.append(f"комментарий «{changed['comment'][:200]}»")
        if not parts:
            raise refuse("INVALID_ARGUMENTS", "Новое значение совпадает с текущим: изменять нечего.")
        denied = await confirm(f"В ресурсе «{target}»: {'; '.join(parts)}. Ресурс перезаписывается целиком "
                               "переимпортом (тот же ресурс, у вершин будут новые id).", False)
        if denied:
            return denied
        folder = join_path(segments[:-1])
        try:
            result = await self.platform.import_resource(folder, updated, clear=True)
        except PlatformError as failure:
            try:
                await self.platform.import_resource(folder, original, clear=True)
                restored = "Ресурс восстановлен из выгрузки до изменения."
            except PlatformError:
                restored = "Восстановить ресурс не удалось: проверьте его в редакторе IACPaaS."
            log.warning("Запись IACPaaS: переимпорт «%s» не удался; %s", target, restored)
            raise PlatformError(failure.code, f"{failure.message} {restored}", failure.retryable) from None
        return self._done("update_concept", target, result, concept_path=concept_path(labels[:-1] + [new_label]))

    # ---------- Чтение по пути ----------

    async def _export(self, path: str, depth: int | None, root: str | None) -> dict:
        key = ("path", segment_key(path), root, depth)
        cached = self.graph._live_cache.get(key)
        if cached and time.monotonic() - cached[0] < self.settings.live_cache_seconds:
            return cached[1]
        graph = await self.platform.export(path, depth=depth, root_path=root)
        self.graph._live_cache[key] = (time.monotonic(), graph)
        return graph

    async def read_resource(self, path) -> ToolResult:
        try:
            segments = self._segments(path, "path")
            known = next((item for item in self.graph.definitions
                          if [segment_key(part) for part in split_path(item.path)] == [segment_key(part) for part in segments]), None)
            if known:  # базовый ресурс — обычное чтение по resource_id (с индексом и структурой онтологии)
                return await self.graph.get_inforesource(known.id)
            target = join_path(segments)
            graph = await self._export(target, 0, None)
        except WriteError as error:
            return error.result
        except PlatformError as error:
            return fail(error.code, error.message, error.retryable)
        data = {key: graph[key] for key in ("title", "code", "date", "creation", "comment") if graph.get(key)}
        if isinstance(graph.get("ontology"), str):
            data["ontology"] = GraphService._relative_path(graph["ontology"])
        data.update(path=target, hint="Содержимое: get_subgraph(path=…, depth до 8); путь вершины — concept_path.")
        return ToolResult(ok=True, data=data, meta={"served_from": "iacpaas_live", "fetched_at": utc_now()})

    async def read_subgraph(self, path, root: str | None, depth: int, max_nodes: int, offset: int) -> ToolResult:
        if not 0 <= depth <= MAX_DEPTH or not 1 <= max_nodes <= self.settings.max_result_nodes or not 0 <= offset <= 100000:
            return fail("LIMIT_EXCEEDED", f"Допустимы depth 0–{MAX_DEPTH}, max_nodes 1–{self.settings.max_result_nodes}, offset 0–100000.")
        try:
            segments = self._segments(path, "path")
            labels = self._segments(root or "", "root", allow_empty=True)
            target = join_path(segments)
            graph = await self._export(target, depth, concept_path(labels))
            vertex = find_vertex(graph, labels) if labels else graph
        except WriteError as error:
            return error.result
        except PlatformError as error:
            return fail(error.code, error.message, error.retryable)
        nodes, edges = [], []
        queue, seen = deque([(vertex, labels, 0)]), set()
        while queue:
            raw, path_labels, distance = queue.popleft()
            node_id = str(raw.get("id"))
            if node_id in seen:
                continue
            seen.add(node_id)
            nodes.append(slim_vertex(raw, path_labels))
            if distance >= depth:
                continue
            for child in raw.get("successors") or []:
                if isinstance(child, dict) and child.get("id") is not None:
                    edges.append({"from": node_id, "to": str(child["id"])})
                    queue.append((child, path_labels + [vertex_text(child)], distance + 1))
        data = page_of_subgraph(str(vertex.get("id")), nodes[offset:offset + max_nodes], edges, len(nodes), offset, max_nodes)
        meta = {"served_from": "iacpaas_live", "resource_path": target, "resource_title": graph.get("title"),
                "platform_modified": graph.get("date"), "fetched_at": utc_now()}
        return self.graph._fit_result(ToolResult(ok=True, data=data, meta=meta), "nodes", offset)


def vertex_text(node: dict) -> str:
    return str(node["value"]) if "value" in node else str(node.get("name", ""))


def slim_vertex(raw: dict, labels: list[str]) -> dict:
    node = {"node_id": str(raw.get("id")), "label": vertex_text(raw), "meta": str(raw.get("meta", "")),
            "concept_path": concept_path(labels) or ""}
    if raw.get("type") and raw["type"] not in ("НЕТЕРМИНАЛ", "КОРЕНЬ"):
        node["type"] = raw["type"]
    for key in ("comment", "original", "link"):
        if raw.get(key):
            node[key] = GraphService._relative_path(str(raw[key])) if key != "comment" else raw[key]
    return node


def find_vertex(graph: dict, labels: list[str]) -> dict:
    """Вершина по именам (значениям) от корня; одноимённые соседи — AMBIGUOUS_PATH."""
    current = graph
    for depth, label in enumerate(labels):
        children = [child for child in current.get("successors") or [] if isinstance(child, dict)]
        matches = [child for child in children if vertex_text(child) == label] or \
                  [child for child in children if segment_key(vertex_text(child)) == segment_key(label)]
        if not matches:
            names = ", ".join(f"«{vertex_text(child)}»" for child in children[:15]) or "вложенных вершин нет"
            where = "/".join(labels[:depth]) or "корне ресурса"
            raise refuse("NODE_NOT_FOUND", f"В «{where}» нет вершины «{label}». Есть: {names}. "
                         "Берите concept_path из get_subgraph(path=…).")
        if len(matches) > 1:
            raise refuse("AMBIGUOUS_PATH", f"В «{'/'.join(labels[:depth]) or 'корне ресурса'}» несколько вершин «{label}»: "
                         "путь неоднозначен, такую вершину через API изменить нельзя.")
        current = matches[0]
    return current


def count_nodes(graph: dict) -> int:
    stack, total = [graph], 0
    while stack:
        node = stack.pop()
        total += 1
        stack.extend(child for child in node.get("successors") or [] if isinstance(child, dict))
    return total


def has_inner_references(graph: dict) -> bool:
    """Есть ли ссылки или клоны, ведущие внутрь самого ресурса (путь «…/Ресурс$/…»)."""
    own = str(graph.get("path") or "").rstrip(";").rstrip("$")
    if not own:
        return True  # без пути ресурса не проверить — считаем, что есть
    own_key = segment_key(own.replace(" ", ""))
    stack = list(graph.get("successors") or [])
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for key in ("original", "link"):
                if node.get(key) and segment_key(str(node[key]).replace(" ", "")).startswith(own_key + "$"):
                    return True
            stack.extend(node.get("successors") or [])
    return False
