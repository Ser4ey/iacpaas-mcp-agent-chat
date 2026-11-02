"""Проверки смысла графа, границ поиска и обновления индекса без сети и реальных учётных данных."""
import asyncio
import copy
import json
import threading
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


async def test_short_links_are_not_marked_as_truncated(service):
    data = graph_with_shared_cycle()
    data["successors"][0]["original"] = ("Pristup.SA@vvsu.ru / Мой Фонд / Загрузки / База свойств материалов$"
                                         "/Физические свойства/Плотность вещества;")
    service.store.import_graph("materials", data)
    node = (await service.get_subgraph("materials", "2", depth=0)).data["nodes"][0]
    # Ссылка переведена в короткий путь, но ничего не обрезано.
    assert node["links"] == ["Загрузки / База свойств материалов$/Физические свойства/Плотность вещества;"]
    assert "fields_truncated" not in node
    hit = service.search_graph("Алюминий", "materials").data["hits"][0]
    assert hit["node_id"] == "2" and hit["fields_truncated"] is False


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


def properties_graph():
    def vertex(node_id, name, meta, *successors):
        return {"id": node_id, "name": name, "meta": meta, "type": "НЕТЕРМИНАЛ", "successors": list(successors)}
    return {**vertex(1, "База свойств", "Онтология базы свойств материалов",
                     vertex(2, "Механические свойства", "Класс свойств",
                            vertex(3, "Твердость", "Свойство", vertex(4, "Возможные значения", "Возможные значения")),
                            vertex(5, "Предел прочности", "Свойство")),
                     vertex(6, "Физические свойства", "Класс свойств", vertex(7, "Плотность", "Свойство"))),
            "title": "База свойств", "type": "КОРЕНЬ", "json_type": "universal"}


def test_count_by_concept_answers_how_many_in_one_call(service):
    service.store.import_graph("other", properties_graph())
    whole = service.count_graph("other", None, "Свойство")
    assert whole.ok and whole.data["count"] == 3 and whole.data["nodes"] == 7
    assert whole.data["names"] == ["Плотность", "Предел прочности", "Твердость"] and not whole.data["names_truncated"]
    inside = service.count_graph("other", "2", "свойство")  # регистр не важен, охват — подграф класса
    assert inside.data["count"] == 2 and inside.data["concept"] == "Свойство"


def test_unknown_concept_lists_concepts_of_the_resource(service):
    service.store.import_graph("other", properties_graph())
    result = service.count_graph("other", None, "Сплав")
    assert result.error.code == "UNKNOWN_CONCEPT"
    assert "Свойство" in result.error.message and "Класс свойств" in result.error.message


# ---------- Проверка устаревания индекса: дата изменения корня (export-depth=0) против даты в индексе ----------

class DatedPlatform:
    """Подменённая IACPaaS: корень ресурса с датой изменения (depth=0) и полная выгрузка; запоминает вызовы."""

    def __init__(self, dates, errors=None, gate=None):
        self.dates, self.errors, self.gate, self.calls = dates, errors or {}, gate, []

    async def export(self, resource_path, depth=None, root_path=None):
        self.calls.append((resource_path, depth))
        if self.gate is not None:
            await asyncio.to_thread(self.gate.wait, 10)
        if resource_path in self.errors:
            raise self.errors[resource_path]
        graph = graph_with_shared_cycle()
        graph["date"] = self.dates[resource_path]
        if depth == 0:
            graph.pop("successors")
        else:
            graph["successors"].append({"id": 9, "name": "Новыйсплав", "type": "НЕТЕРМИНАЛ"})
        return graph


def indexed_service(tmp_path):
    """Оба ресурса в индексе с датой изменения 01.01.2026."""
    result = make_service(tmp_path)
    result.store.import_graph("other", graph_with_shared_cycle())
    return result


async def test_unchanged_resources_are_not_downloaded_again(tmp_path):
    service = indexed_service(tmp_path)
    before = {item: service.store.resource(item)["indexed_at"] for item in ("materials", "other")}
    service.client = DatedPlatform({"Тест / Материалы": "01.01.2026", "Тест / Другая база": "01.01.2026"})
    result = await service.refresh_changed()
    assert result["status"] == "complete" and result["mode"] == "changed"
    assert result["unchanged"] == ["materials", "other"] and result["updated"] == [] and result["failed"] == {}
    # Только лёгкие запросы корня, полной выгрузки нет.
    assert service.client.calls == [("Тест / Материалы", 0), ("Тест / Другая база", 0)]
    assert {item: service.store.resource(item)["indexed_at"] for item in ("materials", "other")} == before
    assert service.status()["last_check"]["unchanged"] == ["materials", "other"]
    assert service.status()["checking"] is False and service.status()["busy"] is False


async def test_only_the_changed_resource_is_reindexed(tmp_path):
    service = indexed_service(tmp_path)
    other_before = service.store.resource("other")["indexed_at"]
    service.client = DatedPlatform({"Тест / Материалы": "02.11.2026-13:35:10.766", "Тест / Другая база": "01.01.2026"})
    result = await service.refresh_changed()
    assert result["status"] == "complete"
    assert result["updated"] == ["materials"] and result["unchanged"] == ["other"]
    assert service.client.calls == [("Тест / Материалы", 0), ("Тест / Материалы", None), ("Тест / Другая база", 0)]
    assert service.store.resource("materials")["metadata"]["date"] == "02.11.2026-13:35:10.766"
    assert service.search_graph("Новыйсплав", "materials").data["total"] == 1
    assert service.search_graph("Новыйсплав", "other").data["total"] == 0
    assert service.store.resource("other")["indexed_at"] == other_before


async def test_platform_error_during_the_check_keeps_the_index(tmp_path):
    service = indexed_service(tmp_path)
    service.client = DatedPlatform({"Тест / Другая база": "01.01.2026"}, errors={
        "Тест / Материалы": PlatformError("PLATFORM_TIMEOUT", "IACPaaS не ответила вовремя.", True)})
    result = await service.refresh_changed()
    assert result["status"] == "partial_failure"
    assert result["failed"] == {"materials": "IACPaaS не ответила вовремя."} and result["unchanged"] == ["other"]
    # Индекс не тронут и работает; сбой проверки — не сбой синхронизации ресурса.
    assert service.status()["ready"] and service.status()["errors"] == {}
    assert service.store.resource("materials")["last_error"] is None
    assert service.search_graph("Алюминий", "materials").ok


async def test_missing_resource_is_loaded_and_failed_download_is_a_sync_error(tmp_path):
    service = make_service(tmp_path)  # «other» в индексе нет
    service.client = DatedPlatform({"Тест / Материалы": "05.05.2026", "Тест / Другая база": "01.01.2026"})
    result = await service.refresh_changed()
    assert result["updated"] == ["materials", "other"]
    assert ("Тест / Другая база", 0) not in service.client.calls  # сверять не с чем — выгружается сразу
    service.client = DatedPlatform({"Тест / Материалы": "06.06.2026", "Тест / Другая база": "01.01.2026"})
    original = service.client.export

    async def full_export_fails(resource_path, depth=None, root_path=None):
        if depth is None:
            raise PlatformError("PLATFORM_UNAVAILABLE", "Тестовая ошибка сети", True)
        return await original(resource_path, depth, root_path)
    service.client.export = full_export_fails
    result = await service.refresh_changed()
    assert result["status"] == "partial_failure" and result["updated"] == []
    assert service.status()["errors"] == {"materials": "Тестовая ошибка сети"}
    assert service.store.resource("materials")["metadata"]["date"] == "05.05.2026"  # прежний снимок остаётся


def test_web_start_checks_dates_in_background_without_delaying_start(tmp_path):
    from fastapi.testclient import TestClient

    from materials_chat.web import create_app

    service = indexed_service(tmp_path)
    settings = service.settings.model_copy(update={
        "auto_sync_on_start": True, "history_path": tmp_path / "history.sqlite3",
        "openrouter_model": "fake", "openrouter_base_url": "http://127.0.0.1:9"})
    app = create_app(settings)
    gate = threading.Event()
    app.state.graph.client = DatedPlatform({"Тест / Материалы": "01.01.2026", "Тест / Другая база": "01.01.2026"},
                                           gate=gate)
    with TestClient(app) as client:
        # Старт не ждёт платформу: индекс готов и работает, пока идёт сверка.
        index = client.get("/api/status").json()["index"]
        assert index["ready"] and index["refresh_running"] and index["checking"]
        gate.set()
        for _ in range(200):
            index = client.get("/api/status").json()["index"]
            if not index["refresh_running"]:
                break
            time.sleep(.02)
        assert index["refresh_error"] is None and index["refresh_result"]["mode"] == "changed"
        assert index["last_check"]["unchanged"] == ["materials", "other"]
        assert app.state.graph.client.calls == [("Тест / Материалы", 0), ("Тест / Другая база", 0)]
