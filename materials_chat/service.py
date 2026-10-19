"""Единая доменная логика для HTTP MCP, stdio и локальной синхронизации."""
import asyncio
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .config import Settings
from .graph_store import GraphStore
from .platform import PlatformClient, PlatformError


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
            for item in self.definitions:
                self._current_resource = item.title
                try:
                    graph = await self.client.export(item.path)
                    # Разбор/индексация 94 тысяч вершин выполняется вне event loop.
                    result = await asyncio.to_thread(self.store.import_graph, item.id, graph)
                    results.append({"ok": True, **result})
                except PlatformError as error:
                    self._sync_errors[item.id] = error.message
                    self.store.record_error(item.id, error.message)
                    results.append({"ok": False, "resource_id": item.id, "error": error.code})
            self._current_resource = None
        return {"status": "complete" if not self._sync_errors else "partial_failure", "resources": results, **self.status()}

    def _ready(self, resource_id: str) -> tuple[ResourceDefinition | None, dict | None, ToolResult | None]:
        definition = self.allowed.get(resource_id)
        if definition is None:
            return None, None, fail("UNKNOWN_RESOURCE", "Неизвестный resource_id. Используйте list_inforesources; произвольные пути запрещены.")
        indexed = self.store.resource(resource_id)
        if indexed is None:
            return definition, None, fail("INDEX_NOT_READY", "Ресурс ещё не загружен. Нажмите «Обновить индекс» и проверьте статус синхронизации.", True)
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
        return ToolResult(ok=True, data={"resources": items[:limit], "total": len(items), "has_more": len(items)>limit},
                          meta={"scope": "configured_allowlist", "permissions": "read_only"})

    def get_inforesource(self, resource_id: str) -> ToolResult:
        definition, indexed, error = self._ready(resource_id)
        if error:
            return error
        metadata = dict(indexed["metadata"])
        metadata["path"] = definition.path
        for field in ("ontology",):
            if isinstance(metadata.get(field), str):
                metadata[field] = self._relative_path(metadata[field])
        metadata["context"] = [self._relative_path(value) for value in (metadata.get("context") or [])]
        return ToolResult(ok=True, data={**definition.model_dump(), "metadata": metadata,
                          "nodes": indexed["node_count"], "edges": indexed["edge_count"],
                          "root": str(metadata.get("id", ""))}, meta=self._metadata(definition, indexed))

    def _bounded_node(self, node: dict) -> dict:
        result = dict(node)
        shortened = False
        for key, cap in (("label", 500), ("meta", 300), ("path", 1800), ("comment", 900), ("value", 1500)):
            cap = min(cap, max(40, self.settings.max_result_chars // 14))
            if isinstance(result.get(key), str) and len(result[key]) > cap:
                result[key] = result[key][:cap] + "…"
                shortened = True
        link_cap = min(1800, max(40, self.settings.max_result_chars // 20))
        result["links"] = [self._relative_path(str(link))[:link_cap] for link in node.get("links", [])[:3]]
        shortened = shortened or result["links"] != node.get("links", [])
        result["fields_truncated"] = shortened
        return result

    def _fit_result(self, result: ToolResult, key: str, offset: int) -> ToolResult:
        """Ограничиваем весь JSON-конверт, сохраняя хотя бы один полезный элемент."""
        items = result.data[key]
        def length():
            return len(json.dumps(result.model_dump(), ensure_ascii=False))
        while length() > self.settings.max_result_chars and len(items) > 1:
            items.pop()
            result.data.update(has_more=True, next_offset=offset+len(items))
            result.meta["result_size_limited"] = True
            if key == "nodes":
                ids = {node["node_id"] for node in items}
                result.data["edges"] = [edge for edge in result.data["edges"] if edge["from"] in ids and edge["to"] in ids]
                result.data["truncated"] = True
        # При очень маленьком бюджете длинный путь/комментарий также сокращается.
        while length() > self.settings.max_result_chars and items:
            candidates = [(len(value), item, field) for item in items for field, value in item.items()
                          if field in ("path", "comment", "value", "snippet", "label") and isinstance(value, str) and len(value)>40]
            if not candidates:
                break
            _, item, field = max(candidates, key=lambda value: value[0])
            item[field] = item[field][:max(30,len(item[field])//2)] + "…"
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
        indexed_ids, sources, unavailable = [], [], []
        for item_id in ids:
            definition, indexed, error = self._ready(item_id)
            if error:
                if resource_id:
                    return error
                unavailable.append(item_id)
            else:
                indexed_ids.append(item_id)
                sources.append(self._metadata(definition, indexed))
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
            if hits and used+size > self.settings.max_result_chars-min(2500,self.settings.max_result_chars//3):
                break
            hits.append(node)
            used += size
        result = ToolResult(ok=True, data={"query": query, "hits": hits, "total": found["total"],
                "match_mode": found["match_mode"], "limit": limit, "offset": offset,
                "has_more": offset+len(hits)<found["total"],
                "next_offset": offset+len(hits) if offset+len(hits)<found["total"] else None,
                "coverage_complete": not unavailable, "unavailable_resources": unavailable},
                meta={"sources": sources, "search_type": "lexical_prefix", "no_match_scope": "only_indexed_resources",
                      "result_size_limited": len(hits)<len(found["hits"])})
        return self._fit_result(result, "hits", offset)

    def get_subgraph(self, resource_id: str, root: str, depth: int = 2, max_nodes: int = 40, offset: int = 0) -> ToolResult:
        definition, indexed, error = self._ready(resource_id)
        if error:
            return error
        if not 0 <= depth <= 5 or not 1 <= max_nodes <= self.settings.max_result_nodes or not 0 <= offset <= 100000:
            return fail("LIMIT_EXCEEDED", f"Допустимы depth 0–5, max_nodes 1–{self.settings.max_result_nodes}, offset 0–100000.")
        try:
            data = self.store.subgraph(resource_id, root, depth, max_nodes, offset)
        except KeyError:
            return fail("NODE_NOT_FOUND", "Вершина не найдена в выбранном ресурсе. Возьмите node_id из search_graph или root из get_inforesource.")
        data["nodes"] = [self._bounded_node(node) for node in data["nodes"]]
        while len(json.dumps(data, ensure_ascii=False)) > self.settings.max_result_chars-1000 and len(data["nodes"])>1:
            data["nodes"].pop()
            ids = {node["node_id"] for node in data["nodes"]}
            data["edges"] = [edge for edge in data["edges"] if edge["from"] in ids and edge["to"] in ids]
            data.update(truncated=True, has_more=True, next_offset=offset+len(data["nodes"]))
        return self._fit_result(ToolResult(ok=True, data=data, meta=self._metadata(definition, indexed)), "nodes", offset)

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
