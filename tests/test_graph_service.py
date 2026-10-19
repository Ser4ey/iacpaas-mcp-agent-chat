"""Проверки смысла графа и границ поиска без сети и реальных учётных данных."""
import json

import pytest

from materials_chat.config import Settings
from materials_chat.graph_store import GraphStore
from materials_chat.platform import PlatformError
from materials_chat.service import GraphService


def graph_with_shared_cycle():
    # Повторные появления id — ссылки; Python-объекты сами не зациклены.
    return {
        "title": "Тестовый ресурс", "id": 1, "name": "Каталог", "type": "КОРЕНЬ",
        "json_type": "universal", "ontology": "Тестовая онтология$;",
        "successors": [
            {"id": 2, "name": "Алюминий", "type": "НЕТЕРМИНАЛ", "successors": [
                {"id": 3, "name": "Свойство", "type": "НЕТЕРМИНАЛ", "successors": [
                    {"id": 2, "name": "Алюминий", "link": "Каталог$ / Алюминий;"}
                ]}
            ]},
            {"id": 3, "name": "Свойство", "link": "Каталог$ / Алюминий / Свойство;"},
        ],
    }


@pytest.fixture
def service(tmp_path):
    config = tmp_path / "resources.json"
    config.write_text(json.dumps([
        {"id": "materials", "title": "Материалы", "path": "Тест / Материалы", "kind": "information"},
        {"id": "other", "title": "Другая база", "path": "Тест / Другая база", "kind": "information"},
    ], ensure_ascii=False), encoding="utf-8")
    # _env_file=None обязателен: тест не должен подхватывать рабочий .env.
    settings = Settings(_env_file=None, resources_path=config, data_dir=tmp_path / "data",
                        iacpaas_username="", iacpaas_password="", iacpaas_api_key="",
                        iacpaas_access_token="", openrouter_api_key="")
    result = GraphService(settings)
    result.store.import_graph("materials", graph_with_shared_cycle())
    return result


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


def test_unknown_resource_is_distinct_from_empty_search(service):
    unknown = service.search_graph("Алюминий", resource_id="not_allowed")
    absent = service.search_graph("несуществующийсплав987654", resource_id="materials")
    unloaded = service.search_graph("Алюминий", resource_id="other")

    assert not unknown.ok and unknown.error.code == "UNKNOWN_RESOURCE"
    assert absent.ok and absent.data["hits"] == [] and absent.data["total"] == 0
    assert not unloaded.ok and unloaded.error.code == "INDEX_NOT_READY"


def test_partial_index_does_not_claim_whole_domain_was_searched(service):
    result = service.search_graph("несуществующийсплав987654")
    assert result.ok
    assert result.data["total"] == 0
    assert not result.data["coverage_complete"]
    assert result.data["unavailable_resources"] == ["other"]
    assert result.meta["no_match_scope"] == "only_indexed_resources"


def test_paged_subgraph_has_no_duplicate_nodes_or_dangling_edges(service):
    first = service.get_subgraph("materials", "1", depth=5, max_nodes=2)
    second = service.get_subgraph("materials", "1", depth=5, max_nodes=2,
                                  offset=first.data["next_offset"])
    assert first.ok and second.ok
    ids = [node["node_id"] for page in (first, second) for node in page.data["nodes"]]
    assert len(ids) == len(set(ids)) == 3
    assert first.data["has_more"] and not second.data["has_more"]
    for page in (first, second):
        page_ids = {node["node_id"] for node in page.data["nodes"]}
        assert all(edge["from"] in page_ids and edge["to"] in page_ids for edge in page.data["edges"])


def test_depth_zero_is_only_the_requested_node(service):
    result = service.get_subgraph("materials", "2", depth=0)
    assert result.ok
    assert [node["node_id"] for node in result.data["nodes"]] == ["2"]
    assert result.data["edges"] == []


@pytest.mark.parametrize("arguments", [
    {"depth": 6}, {"depth": -1}, {"max_nodes": 100000}, {"offset": -1},
])
def test_invalid_subgraph_limits_are_diagnostic(service, arguments):
    result = service.get_subgraph("materials", "1", **arguments)
    assert not result.ok and result.error.code == "LIMIT_EXCEEDED"


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


def test_large_comments_are_shortened_and_marked(service):
    data = graph_with_shared_cycle()
    data["successors"][0]["comment"] = "Длинное описание " * 2000
    service.store.import_graph("materials", data)
    result = service.get_subgraph("materials", "2", depth=0)
    assert result.ok
    assert result.data["nodes"][0]["fields_truncated"]
    assert len(json.dumps(result.model_dump(), ensure_ascii=False)) < service.settings.max_result_chars


def test_small_result_budget_still_returns_a_bounded_useful_search_hit(service):
    service.settings.max_result_chars = 2000
    result = service.search_graph("Алюминий", "materials")
    assert result.ok and result.data["total"] > 0
    assert result.data["hits"], "Размер бюджета не должен превращать существующее совпадение в пустой поиск."
    assert len(json.dumps(result.model_dump(), ensure_ascii=False)) <= service.settings.max_result_chars


def test_small_result_budget_also_bounds_a_single_long_node(service):
    service.settings.max_result_chars = 2000
    data = graph_with_shared_cycle()
    data["successors"][0].update(name="Материал" * 400, comment="Описание" * 400,
                                  original="Ресурс / " + "Путь" * 600)
    service.store.import_graph("materials", data)
    result = service.get_subgraph("materials", "2", depth=0)
    assert result.ok and result.data["nodes"]
    assert result.data["nodes"][0]["fields_truncated"]
    assert len(json.dumps(result.model_dump(), ensure_ascii=False)) <= service.settings.max_result_chars


@pytest.mark.asyncio
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
    assert result.meta["sources"][0]["served_from"] == "local_index"


def test_validation_does_not_pretend_to_verify_domain_knowledge(service):
    result = service.validate_resource("materials")
    assert result.ok and result.data["valid"]
    assert "domain_consistency" in result.data["not_checked"]
    assert "ontology_constraints" in result.data["not_checked"]
