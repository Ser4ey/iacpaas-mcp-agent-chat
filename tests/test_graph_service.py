"""Проверки смысла графа и границ поиска без сети и реальных учётных данных."""
import copy
import json
import time

import pytest

from materials_chat.config import Settings
from materials_chat.graph_store import GraphStore
from materials_chat.platform import PlatformError
from materials_chat.service import GraphService


def graph_with_shared_cycle():
    # Повторные появления id — ссылки; Python-объекты сами не зациклены.
    return {
        "title": "Тестовый ресурс", "id": 1, "name": "Каталог", "type": "КОРЕНЬ",
        "json_type": "universal", "ontology": "Тестовая онтология$;", "date": "01.01.2026",
        "successors": [
            {"id": 2, "name": "Алюминий", "type": "НЕТЕРМИНАЛ", "successors": [
                {"id": 3, "name": "Свойство", "type": "НЕТЕРМИНАЛ", "successors": [
                    {"id": 2, "name": "Алюминий", "link": "Каталог$ / Алюминий;"}
                ]}
            ]},
            {"id": 3, "name": "Свойство", "link": "Каталог$ / Алюминий / Свойство;"},
        ],
    }


def make_service(tmp_path, live_reads=False):
    config = tmp_path / "resources.json"
    config.write_text(json.dumps([
        {"id": "materials", "title": "Материалы", "path": "Тест / Материалы", "kind": "information"},
        {"id": "other", "title": "Другая база", "path": "Тест / Другая база", "kind": "information"},
    ], ensure_ascii=False), encoding="utf-8")
    # _env_file=None обязателен: тест не должен подхватывать рабочий .env.
    settings = Settings(_env_file=None, resources_path=config, data_dir=tmp_path / "data",
                        iacpaas_username="", iacpaas_password="", iacpaas_api_key="",
                        iacpaas_access_token="", openrouter_api_key="", live_reads=live_reads)
    result = GraphService(settings)
    result.store.import_graph("materials", graph_with_shared_cycle())
    return result


@pytest.fixture
def service(tmp_path):
    return make_service(tmp_path)


class FakePlatform:
    """Подменяет IACPaaS: отдаёт путь от корня до запрошенной вершины, как настоящий API."""

    def __init__(self, graph=None, error=None):
        self.graph, self.error, self.calls = graph or graph_with_shared_cycle(), error, []

    async def export(self, resource_path, depth=None, root_path=None):
        self.calls.append((resource_path, depth, root_path))
        if self.error:
            raise self.error
        graph = copy.deepcopy(self.graph)
        graph["date"] = "19.10.2026"
        if root_path == "/Алюминий":
            graph["successors"] = [graph["successors"][0]]
            graph["successors"][0]["name"] = "Алюминий"
            graph["successors"][0]["successors"][0]["name"] = "Свойство (с платформы)"
        return graph


def test_cycle_and_shared_node_are_counted_once(tmp_path):
    store = GraphStore(tmp_path / "graph.sqlite3")
    store.import_graph("materials", graph_with_shared_cycle())

    assert store.counts("materials") == {
        "nodes": 3, "edges": 4, "scope": "whole_indexed_resource"
    }
    # Из вершины 2 достижимы 2 и 3; обратная связь не создаёт бесконечного обхода.
    assert store.counts("materials", "2") == {
        "nodes": 2, "edges": 2, "scope": "reachable_subgraph", "root": "2"
    }
    result = store.subgraph("materials", "1", depth=5, limit=20, offset=0)
    assert {node["node_id"] for node in result["nodes"]} == {"1", "2", "3"}
    assert len(result["edges"]) == 4
    assert not result["truncated"]


def test_count_of_a_large_subgraph_is_fast(tmp_path):
    # Раньше подсчёт был квадратичным: 4 тыс. вершин — больше минуты.
    store = GraphStore(tmp_path / "graph.sqlite3")
    classes = [{"id": 10_000 + index, "name": f"Класс {index}", "successors": [
        {"id": 100_000 + index * 100 + item, "name": f"Марка {index}-{item}"} for item in range(100)]}
        for index in range(200)]
    store.import_graph("materials", {"id": 1, "name": "Корень", "successors": classes})
    started = time.perf_counter()
    counts = store.counts("materials", "1")
    assert counts["nodes"] == 1 + 200 + 20_000 and counts["edges"] == 200 + 20_000
    assert time.perf_counter() - started < 2


def test_concept_labels_follow_the_path_from_the_root(service):
    assert service.store.concept_labels("materials", "1") == []
    assert service.store.concept_labels("materials", "3") == ["Алюминий", "Свойство"]
    assert service.store.concept_labels("materials", "404") is None


def test_unknown_resource_is_distinct_from_empty_search(service):
    unknown = service.search_graph("Алюминий", resource_id="not_allowed")
    absent = service.search_graph("несуществующийсплав987654", resource_id="materials")
    unloaded = service.search_graph("Алюминий", resource_id="other")

    assert not unknown.ok and unknown.error.code == "UNKNOWN_RESOURCE"
    assert absent.ok and absent.data["hits"] == [] and absent.data["total"] == 0
    assert absent.meta["served_from"] == "local_index"
    assert not unloaded.ok and unloaded.error.code == "INDEX_NOT_READY"


def test_partial_index_does_not_claim_whole_domain_was_searched(service):
    result = service.search_graph("несуществующийсплав987654")
    assert result.ok
    assert result.data["total"] == 0
    assert not result.data["coverage_complete"]
    assert result.data["unavailable_resources"] == ["other"]
    assert result.meta["no_match_scope"] == "only_indexed_resources"


async def test_paged_subgraph_has_no_duplicate_nodes_or_dangling_edges(service):
    first = await service.get_subgraph("materials", "1", depth=5, max_nodes=2)
    second = await service.get_subgraph("materials", "1", depth=5, max_nodes=2, offset=first.data["next_offset"])
    assert first.ok and second.ok
    ids = [node["node_id"] for page in (first, second) for node in page.data["nodes"]]
    assert len(ids) == len(set(ids)) == 3
    assert first.data["has_more"] and not second.data["has_more"]
    for page in (first, second):
        page_ids = {node["node_id"] for node in page.data["nodes"]}
        assert all(edge["from"] in page_ids and edge["to"] in page_ids for edge in page.data["edges"])


async def test_depth_zero_is_only_the_requested_node(service):
    result = await service.get_subgraph("materials", "2", depth=0)
    assert result.ok
    assert [node["node_id"] for node in result.data["nodes"]] == ["2"]
    assert result.data["edges"] == []


async def test_unread_children_are_marked_and_a_deep_record_is_read_at_once(service):
    record = {
        "title": "Записи", "id": 1, "name": "Записи", "type": "КОРЕНЬ", "json_type": "universal",
        "ontology": "Тестовая онтология$;", "successors": [
            {"id": 2, "name": "Марка", "type": "НЕТЕРМИНАЛ", "successors": [
                {"id": 3, "name": "Состав", "type": "НЕТЕРМИНАЛ", "successors": [
                    {"id": 4, "name": "1", "type": "НЕТЕРМИНАЛ", "successors": [
                        {"id": 5, "name": "Cr", "type": "НЕТЕРМИНАЛ", "successors": [
                            {"id": 6, "name": "Числовой интервал", "type": "НЕТЕРМИНАЛ", "successors": [
                                {"id": 7, "name": "Нижняя граница", "type": "НЕТЕРМИНАЛ", "successors": [
                                    {"id": 8, "value": "17.0", "type": "ТЕРМИНАЛ-ЗНАЧЕНИЕ"}]}]}]}]}]},
                {"id": 9, "name": "Стандарт", "type": "НЕТЕРМИНАЛ"}]}],
    }
    service.store.import_graph("materials", record)
    shallow = await service.get_subgraph("materials", "2", depth=1)
    marks = {node["node_id"]: node.get("children_not_loaded") for node in shallow.data["nodes"]}
    assert marks == {"2": None, "3": 1, "9": None}  # «Стандарт» — лист, перечитывать нечего

    whole = await service.get_subgraph("materials", "2", depth=8)
    assert whole.ok and "8" in {node["node_id"] for node in whole.data["nodes"]}
    assert not any("children_not_loaded" in node for node in whole.data["nodes"])


def test_search_meta_lists_only_resources_with_hits(service):
    service.store.import_graph("other", {"title": "Другая база", "id": 50, "name": "Другая база", "type": "КОРЕНЬ",
                                         "json_type": "universal", "successors": [{"id": 51, "name": "Медь"}]})
    result = service.search_graph("Алюминий")
    assert result.ok and result.data["coverage_complete"]
    assert result.meta["sources"] == [{"resource_id": "materials",
                                       "indexed_at": service.store.resource("materials")["indexed_at"]}]
    assert result.meta["indexed_at"] == min(service.store.resource(item)["indexed_at"] for item in ("materials", "other"))


@pytest.mark.parametrize("arguments", [
    {"depth": 9}, {"depth": -1}, {"max_nodes": 100000}, {"offset": -1},
])
async def test_invalid_subgraph_limits_are_diagnostic(service, arguments):
    result = await service.get_subgraph("materials", "1", **arguments)
    assert not result.ok and result.error.code == "LIMIT_EXCEEDED"


async def test_live_subgraph_reads_the_platform_by_concept_path(tmp_path):
    service = make_service(tmp_path, live_reads=True)
    service.client = platform = FakePlatform()
    result = await service.get_subgraph("materials", "2", depth=1)
    assert platform.calls == [("Тест / Материалы", 1, "/Алюминий")]
    assert result.ok and result.meta["served_from"] == "iacpaas_live"
    assert result.meta["platform_modified"] == "19.10.2026" and result.meta["fetched_at"]
    labels = {node["node_id"]: node["label"] for node in result.data["nodes"]}
    assert labels == {"2": "Алюминий", "3": "Свойство (с платформы)"}
    assert result.data["edges"] == [{"from": "2", "to": "3"}]
    # Повторное чтение той же страницы берётся из короткого кэша.
    await service.get_subgraph("materials", "2", depth=1)
    assert len(platform.calls) == 1


async def test_live_root_and_resource_metadata(tmp_path):
    service = make_service(tmp_path, live_reads=True)
    service.client = platform = FakePlatform()
    root = await service.get_subgraph("materials", "1", depth=1)
    info = await service.get_inforesource("materials")
    assert platform.calls == [("Тест / Материалы", 1, None), ("Тест / Материалы", 0, None)]
    assert root.meta["served_from"] == "iacpaas_live"
    assert info.meta["served_from"] == "iacpaas_live" and info.data["metadata"]["date"] == "19.10.2026"


async def test_unavailable_platform_falls_back_to_the_index(tmp_path):
    service = make_service(tmp_path, live_reads=True)
    service.client = FakePlatform(error=PlatformError("PLATFORM_TIMEOUT", "IACPaaS не ответила вовремя.", True))
    result = await service.get_subgraph("materials", "2", depth=1)
    assert result.ok and result.data["nodes"]
    assert result.meta["served_from"] == "local_index_fallback"
    assert result.meta["live_error"]["code"] == "PLATFORM_TIMEOUT"


async def test_moved_node_falls_back_and_slash_names_are_quoted(tmp_path):
    service = make_service(tmp_path, live_reads=True)
    moved = graph_with_shared_cycle()
    moved["successors"][0]["id"] = 999
    service.client = FakePlatform(graph=moved)
    result = await service.get_subgraph("materials", "2", depth=1)
    assert result.meta["live_error"]["code"] == "NODE_MOVED"

    slashed = graph_with_shared_cycle()
    slashed["successors"][0]["name"] = "кг/м³"
    service.store.import_graph("materials", slashed)
    service.client = platform = FakePlatform(graph=slashed)
    result = await service.get_subgraph("materials", "2", depth=1)
    assert platform.calls == [("Тест / Материалы", 1, "/‴кг/м³‴")]
    assert result.meta["served_from"] == "iacpaas_live"


async def test_large_fragment_is_served_from_the_index(tmp_path):
    service = make_service(tmp_path, live_reads=True)
    service.settings.live_max_nodes = 2
    service.client = platform = FakePlatform()
    result = await service.get_subgraph("materials", "1", depth=5)
    assert platform.calls == [] and result.meta["live_skipped"] == "fragment_too_large"


def test_search_limits_and_paging(service):
    data = graph_with_shared_cycle()
    data["successors"] = [
        {"id": index + 10, "name": f"Сталь образец {index}", "type": "НЕТЕРМИНАЛ"}
        for index in range(25)
    ]
    service.store.import_graph("materials", data)
    first = service.search_graph("стали", "materials", limit=7)
    second = service.search_graph("стали", "materials", limit=7, offset=first.data["next_offset"])
    assert first.ok and second.ok
    assert first.data["total"] == 25
    assert len(first.data["hits"]) == len(second.data["hits"]) == 7
    assert not ({node["node_id"] for node in first.data["hits"]} &
                {node["node_id"] for node in second.data["hits"]})
    excessive = service.search_graph("сталь", "materials", limit=21)
    assert not excessive.ok and excessive.error.code == "LIMIT_EXCEEDED"


async def test_large_comments_are_shortened_and_marked(service):
    data = graph_with_shared_cycle()
    data["successors"][0]["comment"] = "Длинное описание " * 2000
    service.store.import_graph("materials", data)
    result = await service.get_subgraph("materials", "2", depth=0)
    assert result.ok
    assert result.data["nodes"][0]["fields_truncated"]
    assert len(json.dumps(result.model_dump(), ensure_ascii=False)) < service.settings.max_result_chars


def test_small_result_budget_still_returns_a_bounded_useful_search_hit(service):
    service.settings.max_result_chars = 2000
    result = service.search_graph("Алюминий", "materials")
    assert result.ok and result.data["total"] > 0
    assert result.data["hits"], "Размер бюджета не должен превращать существующее совпадение в пустой поиск."
    assert len(json.dumps(result.model_dump(), ensure_ascii=False)) <= service.settings.max_result_chars


async def test_small_result_budget_also_bounds_a_single_long_node(service):
    service.settings.max_result_chars = 2000
    data = graph_with_shared_cycle()
    data["successors"][0].update(name="Материал" * 400, comment="Описание" * 400,
                                  original="Ресурс / " + "Путь" * 600)
    service.store.import_graph("materials", data)
    result = await service.get_subgraph("materials", "2", depth=0)
    assert result.ok and result.data["nodes"]
    assert result.data["nodes"][0]["fields_truncated"]
    assert len(json.dumps(result.model_dump(), ensure_ascii=False)) <= service.settings.max_result_chars


async def test_failed_refresh_keeps_old_snapshot_and_reports_error(service):
    class UnavailablePlatform:
        async def export(self, resource_path):
            raise PlatformError("PLATFORM_UNAVAILABLE", "Тестовая ошибка сети", True)

    service.client = UnavailablePlatform()
    refreshed = await service.refresh_all()
    assert refreshed["status"] == "partial_failure"
    assert refreshed["resources_indexed"] == 1
    result = service.search_graph("Алюминий", "materials")
    assert result.ok and result.data["hits"]
    assert result.meta["sources"][0]["last_sync_error"] == "Тестовая ошибка сети"
    assert result.meta["served_from"] == "local_index"


async def test_broken_resource_does_not_stop_the_whole_refresh(service):
    class HalfBrokenPlatform:
        async def export(self, resource_path):
            if resource_path.endswith("Материалы"):
                return {"broken": True}  # без id/successors не проиндексируется как ожидается
            return graph_with_shared_cycle()

    def explode(resource_id, graph):
        if graph.get("broken"):
            raise ValueError("сломанная выгрузка")
        return original(resource_id, graph)

    original = service.store.import_graph
    service.store.import_graph = explode
    service.client = HalfBrokenPlatform()
    refreshed = await service.refresh_all()
    assert refreshed["status"] == "partial_failure"
    assert [item["ok"] for item in refreshed["resources"]] == [False, True]
    assert service.status()["current_resource"] is None


def test_validation_does_not_pretend_to_verify_domain_knowledge(service):
    result = service.validate_resource("materials")
    assert result.ok and result.data["valid"]
    assert "domain_consistency" in result.data["not_checked"]
    assert "ontology_constraints" in result.data["not_checked"]
