"""Единая доменная логика для HTTP MCP, stdio и локальной синхронизации.

Источники данных:
- поиск по тексту — локальный индекс (у HTTP API IACPaaS нет полнотекстового поиска);
- чтение ресурса и подграфа — живой запрос к IACPaaS; если платформа недоступна,
  ответ берётся из индекса с явной пометкой (meta.served_from).
"""
import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .composition import (RULES, ConditionError, class_names, in_class, parse_conditions, project,
                          resolve_element, select_materials)
from .config import Settings
from .graph_store import GraphStore, subgraph_from_export
from .platform import PlatformClient, PlatformError, concept_path, utc_now

log = logging.getLogger(__name__)

# Самая глубокая запись марки — 7 уровней от вершины марки: любая запись читается одним вызовом.
MAX_DEPTH = 8
COMPOSITION_RESOURCE = "materials"


class ResourceDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    title: str
    path: str
    kind: str


class ToolError(BaseModel):
    code: str
    message: str
    retryable: bool = False


class ToolResult(BaseModel):
    """Общая типизированная оболочка: ошибки не маскируются пустым поиском."""
    ok: bool
    data: dict[str, Any] = Field(default_factory=dict)
    error: ToolError | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


def fail(code: str, message: str, retryable: bool = False) -> ToolResult:
    return ToolResult(ok=False, error=ToolError(code=code, message=message, retryable=retryable))


class GraphService:
    def __init__(self, settings: Settings):
        self.settings = settings
        path = settings.resources_path
        if not path.exists():
            path = Path(__file__).parent / "defaults" / "resources.json"
        self.definitions = [ResourceDefinition.model_validate(item) for item in json.loads(path.read_text(encoding="utf-8"))]
        if len({item.id for item in self.definitions}) != len(self.definitions):
            raise ValueError("В конфигурации повторяются идентификаторы ресурсов.")
        self.allowed = {item.id: item for item in self.definitions}
        self.store = GraphStore(settings.data_dir / "search.sqlite3")
        self.client = PlatformClient(settings)
        self._refresh_lock = asyncio.Lock()
        self._sync_errors: dict[str, str] = {}
        self._current_resource: str | None = None
        # Короткий кэш живых выгрузок: модель часто листает один подграф по страницам.
        self._live_cache: dict[tuple, tuple[float, dict]] = {}

    def resources(self) -> list[dict]:
        return [item.model_dump() for item in self.definitions]

    def status(self) -> dict:
        entries = [self.store.resource(item.id) for item in self.definitions]
        existing = [item for item in entries if item]
        return {"ready": len(existing) == len(self.definitions) and bool(existing),
                "resources_indexed": len(existing), "total_resources": len(self.definitions),
                "nodes": sum(item["node_count"] for item in existing),
                "last_synced": min((item["indexed_at"] for item in existing), default=None),
                "busy": self._refresh_lock.locked(), "current_resource": self._current_resource,
                "errors": dict(self._sync_errors)}

    async def refresh_all(self) -> dict:
        if self._refresh_lock.locked():
            return {"status": "already_running"}
        results = []
        async with self._refresh_lock:
            self._sync_errors = {}
            try:
                for item in self.definitions:
                    self._current_resource = item.title
                    try:
                        graph = await self.client.export(item.path)
                        # Разбор/индексация 94 тысяч вершин выполняется вне event loop.
                        result = await asyncio.to_thread(self.store.import_graph, item.id, graph)
                        results.append({"ok": True, **result})
                    except PlatformError as error:
                        self._remember_sync_error(item.id, error.message)
                        results.append({"ok": False, "resource_id": item.id, "error": error.code})
                    except Exception:  # одна сломанная выгрузка не должна останавливать остальные
                        log.exception("Не удалось проиндексировать ресурс %s", item.id)
                        self._remember_sync_error(item.id, "Не удалось разобрать или сохранить ресурс.")
                        results.append({"ok": False, "resource_id": item.id, "error": "INDEX_FAILED"})
            finally:
                self._current_resource = None
        return {"status": "complete" if not self._sync_errors else "partial_failure", "resources": results, **self.status()}

    def _remember_sync_error(self, resource_id: str, message: str):
        self._sync_errors[resource_id] = message
        self.store.record_error(resource_id, message)

    def _ready(self, resource_id: str) -> tuple[ResourceDefinition | None, dict | None, ToolResult | None]:
        definition = self.allowed.get(resource_id)
        if definition is None:
            return None, None, fail("UNKNOWN_RESOURCE", "Неизвестный resource_id. Используйте list_inforesources; произвольные пути запрещены.")
        indexed = self.store.resource(resource_id)
        if indexed is None:
            return definition, None, fail("INDEX_NOT_READY", "Ресурс ещё не загружен. Нажмите «Обновить из IACPaaS» и проверьте статус синхронизации.", True)
        return definition, indexed, None

    @staticmethod
    def _metadata(definition: ResourceDefinition, indexed: dict) -> dict:
        return {"resource_id": definition.id, "resource_title": definition.title,
                "resource_path": definition.path, "indexed_at": indexed["indexed_at"],
                "platform_modified": indexed["metadata"].get("date"),
                "source": "IACPaaS", "served_from": "local_index", "last_sync_error": indexed.get("last_error")}

    @staticmethod
    def _relative_path(path: str) -> str:
        return path.split("Мой Фонд", 1)[-1].strip(" /") if "Мой Фонд" in path else path

    # ---------- Живое чтение с платформы ----------

    async def _live_export(self, definition: ResourceDefinition, depth: int, concept_path: str | None) -> dict:
        key = (definition.id, concept_path, depth)
        cached = self._live_cache.get(key)
        if cached and time.monotonic() - cached[0] < self.settings.live_cache_seconds:
            return cached[1]
        graph = await self.client.export(definition.path, depth=depth, root_path=concept_path)
        self._live_cache[key] = (time.monotonic(), graph)
        return graph

    @staticmethod
    def _descend(graph: dict, labels: list[str]) -> dict | None:
        """Платформа отдаёт и путь от корня до запрошенной вершины — спускаемся по именам."""
        current = graph
        for label in labels:
            current = next((child for child in current.get("successors", []) if isinstance(child, dict)
                            and str(child.get("name", child.get("value", ""))) == label), None)
            if current is None:
                return None
        return current

    def _live_meta(self, definition: ResourceDefinition, indexed: dict, graph: dict) -> dict:
        return {**self._metadata(definition, indexed), "served_from": "iacpaas_live", "fetched_at": utc_now(),
                "platform_modified": graph.get("date", indexed["metadata"].get("date"))}

    @staticmethod
    def _fallback_meta(meta: dict, reason: str, message: str) -> dict:
        return {**meta, "served_from": "local_index_fallback", "live_error": {"code": reason, "message": message}}

    # ---------- Инструменты ----------

    def list_inforesources(self, filter: str = "", limit: int = 20) -> ToolResult:
        if not 1 <= limit <= 30:
            return fail("LIMIT_EXCEEDED", "limit должен быть от 1 до 30.")
        items = []
        for definition in self.definitions:
            if filter.casefold() not in (definition.title + " " + definition.path + " " + definition.id).casefold():
                continue
            indexed = self.store.resource(definition.id)
            items.append({**definition.model_dump(), "indexed": indexed is not None,
                          "nodes": indexed["node_count"] if indexed else None,
                          "indexed_at": indexed["indexed_at"] if indexed else None})
        return ToolResult(ok=True, data={"resources": items[:limit], "total": len(items), "has_more": len(items) > limit},
                          meta={"scope": "configured_allowlist", "permissions": "read_only", "served_from": "configuration"})

    async def get_inforesource(self, resource_id: str) -> ToolResult:
        definition, indexed, error = self._ready(resource_id)
        if error:
            return error
        metadata = dict(indexed["metadata"])
        meta = self._metadata(definition, indexed)
        if self.settings.live_reads:
            try:
                # Глубина 0 — только корень: свежие дата изменения, онтология и комментарий.
                graph = await self._live_export(definition, 0, None)
                metadata.update({key: graph[key] for key in ("title", "date", "ontology", "context", "comment") if key in graph})
                meta = self._live_meta(definition, indexed, graph)
            except PlatformError as failure:
                meta = self._fallback_meta(meta, failure.code, failure.message)
        metadata["path"] = definition.path
        if isinstance(metadata.get("ontology"), str):
            metadata["ontology"] = self._relative_path(metadata["ontology"])
        metadata["context"] = [self._relative_path(value) for value in (metadata.get("context") or [])]
        return ToolResult(ok=True, data={**definition.model_dump(), "metadata": metadata,
                          "nodes": indexed["node_count"], "edges": indexed["edge_count"],
                          "root": str(metadata.get("id", ""))}, meta=meta)

    def _bounded_node(self, node: dict) -> dict:
        result = dict(node)
        shortened = False
        for key, cap in (("label", 500), ("meta", 300), ("path", 1800), ("comment", 900), ("value", 1500)):
            cap = min(cap, max(40, self.settings.max_result_chars // 14))
            if isinstance(result.get(key), str) and len(result[key]) > cap:
                result[key] = result[key][:cap] + "…"
                shortened = True
        link_cap = min(1800, max(40, self.settings.max_result_chars // 20))
        # Укорочение пути до «Мой Фонд» — не усечение: признак только при обрезке по длине или по числу ссылок.
        links = [self._relative_path(str(link)) for link in node.get("links", [])]
        shortened = shortened or len(links) > 3 or any(len(link) > link_cap for link in links)
        result["links"] = [link[:link_cap] for link in links[:3]]
        result["fields_truncated"] = shortened
        return result

    @staticmethod
    def _slim_node(node: dict, is_root: bool) -> dict:
        """Вершина подграфа без повторов: структуру уже задают дуги, ресурс — meta результата.

        Так целая запись материала (десятки вершин) помещается в одну выдачу.
        """
        slim = {"node_id": node["node_id"], "label": node["label"], "meta": node["meta"]}
        if is_root:
            slim.update(resource_id=node["resource_id"], path=node["path"])
        if node.get("type") and node["type"] != "НЕТЕРМИНАЛ":
            slim["type"] = node["type"]
        if node.get("value") is not None and str(node["value"]) != node["label"]:
            slim["value"] = node["value"]
        for key in ("comment", "links"):
            if node.get(key):
                slim[key] = node[key]
        if node.get("fields_truncated"):
            slim["fields_truncated"] = True
        return slim

    def _fit_result(self, result: ToolResult, key: str, offset: int) -> ToolResult:
        """Ограничиваем весь JSON-конверт, сохраняя хотя бы один полезный элемент."""
        items = result.data[key]

        def length():
            return len(json.dumps(result.model_dump(), ensure_ascii=False))
        while length() > self.settings.max_result_chars and len(items) > 1:
            items.pop()
            result.data.update(has_more=True, next_offset=offset + len(items))
            result.meta["result_size_limited"] = True
            if key == "nodes":
                ids = {node["node_id"] for node in items}
                result.data["edges"] = [edge for edge in result.data["edges"] if edge["from"] in ids and edge["to"] in ids]
                result.data["truncated"] = True
        # При очень маленьком бюджете длинный путь/комментарий также сокращается.
        while length() > self.settings.max_result_chars and items:
            candidates = [(len(value), item, field) for item in items for field, value in item.items()
                          if field in ("path", "comment", "value", "snippet", "label") and isinstance(value, str) and len(value) > 40]
            if not candidates:
                break
            _, item, field = max(candidates, key=lambda value: value[0])
            item[field] = item[field][:max(30, len(item[field]) // 2)] + "…"
            item["fields_truncated"] = True
        if length() > self.settings.max_result_chars:
            return fail("RESULT_TOO_LARGE", "Результат не помещается в бюджет. Укажите один resource_id и более узкий запрос.")
        return result

    def search_graph(self, query: str, resource_id: str | None = None, limit: int = 8, offset: int = 0) -> ToolResult:
        if not query.strip() or len(query) > 300:
            return fail("INVALID_QUERY", "Передайте непустой поисковый запрос до 300 символов.")
        if not 1 <= limit <= 20 or not 0 <= offset <= 10000:
            return fail("LIMIT_EXCEEDED", "Допустимы limit 1–20 и offset 0–10000.")
        ids = [resource_id] if resource_id else list(self.allowed)
        indexed_ids, snapshots, unavailable = [], {}, []
        for item_id in ids:
            definition, indexed, error = self._ready(item_id)
            if error:
                if resource_id:
                    return error
                unavailable.append(item_id)
            else:
                indexed_ids.append(item_id)
                snapshots[item_id] = indexed
        if not indexed_ids:
            return fail("INDEX_NOT_READY", "Поисковый индекс пуст. Запустите синхронизацию с IACPaaS.", True)
        found = self.store.search(query, indexed_ids, limit, offset)
        hits, used = [], 0
        for hit in found["hits"]:
            node = self._bounded_node(hit)
            definition = self.allowed[node["resource_id"]]
            node.update({"resource_title": definition.title,
                         "snippet": (node["path"] + (" — " + node["comment"] if node["comment"] else ""))[:1800],
                         "source": f"{definition.title} · вершина {node['node_id']}"})
            size = len(json.dumps(node, ensure_ascii=False))
            if hits and used + size > self.settings.max_result_chars - min(2500, self.settings.max_result_chars // 3):
                break
            hits.append(node)
            used += size
        # Только ресурсы с найденными вершинами и дата их снимка: полные метаданные всех
        # восьми ресурсов занимали больше половины ответа и уходили в модель с каждым поиском.
        sources = []
        for item_id in indexed_ids:
            if any(hit["resource_id"] == item_id for hit in hits):
                source = {"resource_id": item_id, "indexed_at": snapshots[item_id]["indexed_at"]}
                if snapshots[item_id].get("last_error"):
                    source["last_sync_error"] = snapshots[item_id]["last_error"]
                sources.append(source)
        result = ToolResult(ok=True, data={"query": query, "hits": hits, "total": found["total"],
                "match_mode": found["match_mode"], "limit": limit, "offset": offset,
                "has_more": offset + len(hits) < found["total"],
                "next_offset": offset + len(hits) if offset + len(hits) < found["total"] else None,
                "coverage_complete": not unavailable, "unavailable_resources": unavailable},
                meta={"served_from": "local_index", "indexed_at": min(item["indexed_at"] for item in snapshots.values()),
                      "sources": sources, "search_type": "lexical_prefix",
                      "no_match_scope": "only_indexed_resources", "result_size_limited": len(hits) < len(found["hits"])})
        return self._fit_result(result, "hits", offset)

    async def get_subgraph(self, resource_id: str, root: str, depth: int = 2, max_nodes: int = 40, offset: int = 0) -> ToolResult:
        definition, indexed, error = self._ready(resource_id)
        if error:
            return error
        if not 0 <= depth <= MAX_DEPTH or not 1 <= max_nodes <= self.settings.max_result_nodes or not 0 <= offset <= 100000:
            return fail("LIMIT_EXCEEDED", f"Допустимы depth 0–{MAX_DEPTH}, max_nodes 1–{self.settings.max_result_nodes}, offset 0–100000.")
        try:
            # Индекс отвечает мгновенно: по нему проверяем вершину и размер фрагмента.
            data = await asyncio.to_thread(self.store.subgraph, resource_id, root, depth, max_nodes, offset)
        except KeyError:
            return fail("NODE_NOT_FOUND", "Вершина не найдена в выбранном ресурсе. Возьмите node_id из search_graph или root из get_inforesource.")
        meta = self._metadata(definition, indexed)
        if self.settings.live_reads:
            data, meta = await self._live_subgraph(definition, indexed, root, depth, max_nodes, offset, data, meta)
        data["nodes"] = [self._slim_node(self._bounded_node(node), node["node_id"] == data["root"]) for node in data["nodes"]]
        counts = await asyncio.to_thread(self.store.child_counts, resource_id, [node["node_id"] for node in data["nodes"]])
        self._mark_unloaded(data, counts)
        while len(json.dumps(data, ensure_ascii=False)) > self.settings.max_result_chars - 1000 and len(data["nodes"]) > 1:
            data["nodes"].pop()
            ids = {node["node_id"] for node in data["nodes"]}
            data["edges"] = [edge for edge in data["edges"] if edge["from"] in ids and edge["to"] in ids]
            data.update(truncated=True, has_more=True, next_offset=offset + len(data["nodes"]))
            self._mark_unloaded(data, counts)
        return self._fit_result(ToolResult(ok=True, data=data, meta=meta), "nodes", offset)

    @staticmethod
    def _mark_unloaded(data: dict, counts: dict[str, int]):
        """children_not_loaded — сколько потомков вершины не попало в выдачу (глубина или страница).

        Без этого поля модель не отличит лист от вершины на границе глубины и перечитывает её.
        """
        shown: dict[str, set] = {}
        for edge in data["edges"]:
            shown.setdefault(edge["from"], set()).add(edge["to"])
        for node in data["nodes"]:
            missing = counts.get(node["node_id"], 0) - len(shown.get(node["node_id"], ()))
            if missing > 0:
                node["children_not_loaded"] = missing
            else:
                node.pop("children_not_loaded", None)

    async def _live_subgraph(self, definition, indexed, root, depth, max_nodes, offset, data, meta):
        """Тот же фрагмент, но прочитанный с платформы. При любой неудаче — данные индекса."""
        if data["total_within_depth"] > self.settings.live_max_nodes:
            return data, {**meta, "live_skipped": "fragment_too_large"}
        labels = await asyncio.to_thread(self.store.concept_labels, definition.id, root)
        if labels is None:
            return data, self._fallback_meta(meta, "PATH_NOT_FOUND", "Путь к вершине не восстановлен по индексу.")
        try:
            graph = await self._live_export(definition, depth, concept_path(labels))
        except PlatformError as failure:
            return data, self._fallback_meta(meta, failure.code, failure.message)
        target = self._descend(graph, labels)
        if target is None or str(target.get("id")) != root:
            return data, self._fallback_meta(meta, "NODE_MOVED", "Вершина на платформе не совпала с индексом; возможно, ресурс изменился.")
        base_path = "/".join([str(graph.get("name", definition.title)), *labels])
        live = subgraph_from_export(definition.id, target, base_path, depth, max_nodes, offset)
        return live, self._live_meta(definition, indexed, graph)

    def count_graph(self, resource_id: str, root: str | None = None) -> ToolResult:
        definition, indexed, error = self._ready(resource_id)
        if error:
            return error
        try:
            counts = self.store.counts(resource_id, root)
        except KeyError:
            return fail("NODE_NOT_FOUND", "Вершина не найдена в выбранном ресурсе.")
        return ToolResult(ok=True, data=counts, meta={**self._metadata(definition, indexed), "external_links_not_expanded": True})

    def validate_resource(self, resource_id: str) -> ToolResult:
        definition, indexed, error = self._ready(resource_id)
        if error:
            return error
        metadata = indexed["metadata"]
        issues = []
        if metadata.get("json_type") != "universal":
            issues.append("Формат отличается от universal.")
        if not metadata.get("ontology"):
            issues.append("Не задана онтология ресурса.")
        if indexed["node_count"] < 1:
            issues.append("Не найдена корневая вершина.")
        return ToolResult(ok=True, data={"valid": not issues, "issues": issues,
                "checked": ["universal_format", "ontology_reference", "nonempty_graph"],
                "not_checked": ["domain_consistency", "ontology_constraints", "external_reference_access"],
                "note": "Это базовая локальная проверка структуры, не проверка полноты знаний платформой."},
                meta=self._metadata(definition, indexed))

    def find_materials(self, conditions: list[dict] | None = None, material_class: str | None = None,
                       sort_by: str | None = None, order: str = "desc", limit: int = 20, offset: int = 0) -> ToolResult:
        """Подбор марок по условиям на состав; пороги проверяются по границам диапазона, а не по середине."""
        try:
            parsed = parse_conditions(conditions)
        except ConditionError as error:
            return fail("INVALID_CONDITION", str(error))
        if order not in ("desc", "asc"):
            return fail("INVALID_CONDITION", "order: desc или asc.")
        if not 1 <= limit <= 50 or not 0 <= offset <= 100000:
            return fail("LIMIT_EXCEEDED", "Допустимы limit 1–50 и offset 0–100000.")
        definition, indexed, error = self._ready(COMPOSITION_RESOURCE)
        if error:
            return error
        everything = self.store.composition(COMPOSITION_RESOURCE)
        records = everything
        if material_class:
            records = [record for record in everything if in_class(record, material_class)]
            if not records:
                return fail("UNKNOWN_CLASS", "Класс не найден. Классы: " + "; ".join(class_names(everything)) + ".")
        russian = self.store.element_symbols()
        in_any = {symbol for record in everything for symbol in record["contents"]}
        known = in_any | set(russian.values())

        def resolve(text: str) -> str:
            return resolve_element(text, known, russian)
        pairs = [(resolve(condition.element), condition) for condition in parsed]
        sort_symbol = resolve(sort_by) if sort_by and sort_by.strip() else None
        mentioned = list(dict.fromkeys([symbol for symbol, _ in pairs] + ([sort_symbol] if sort_symbol else [])))
        selected = select_materials(records, pairs, sort_symbol, order == "desc")
        page = [project(record, mentioned) for record in selected[offset:offset + limit]]
        more = offset + len(page) < len(selected)
        result = ToolResult(ok=True, data={
            "resource_id": definition.id, "total": len(selected), "materials": page, "limit": limit, "offset": offset,
            "has_more": more, "next_offset": offset + len(page) if more else None, "rules": RULES,
            "elements_not_in_compositions": [symbol for symbol in mentioned if symbol not in in_any]},
            meta=self._metadata(definition, indexed))
        return self._fit_result(result, "materials", offset)
