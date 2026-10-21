"""Готовность find_materials на копии реального индекса; без индекса тесты пропускаются."""
import json
import sqlite3
import time

import pytest

from materials_chat.config import PROJECT_ROOT, Settings
from materials_chat.service import GraphService

REAL_INDEX = PROJECT_ROOT / "data" / "search.sqlite3"
pytestmark = pytest.mark.skipif(not REAL_INDEX.exists(), reason="нет локального индекса data/search.sqlite3")
STEEL, AMG3 = "381405980786784", "381405980787068"  # 12Х18Н10Т и АМг3 — контрольные записи спецификации


@pytest.fixture(scope="module")
def copy(tmp_path_factory):
    folder = tmp_path_factory.mktemp("real-index")
    source = sqlite3.connect(REAL_INDEX.as_uri() + "?mode=ro", uri=True)
    target = sqlite3.connect(folder / "search.sqlite3")
    source.backup(target)  # копия с учётом WAL; рабочий индекс не меняется
    source.close()
    target.close()
    return folder


@pytest.fixture(scope="module")
def service(copy):
    return GraphService(Settings(_env_file=None, data_dir=copy, resources_path=PROJECT_ROOT / "config" / "resources.json",
                                 live_reads=False, iacpaas_username="", iacpaas_password="", openrouter_api_key=""))


@pytest.fixture(scope="module")
def ranges(copy):
    """{символ: {id марки: (низ, верх) или None}} — независимый обход, без composition.py."""
    db = sqlite3.connect(copy / "search.sqlite3")
    nodes = {row[0]: (row[1], row[2], row[3]) for row in db.execute(
        "SELECT node_id, label, meta, payload FROM nodes WHERE resource_id='materials'")}
    parent, kids = {}, {}
    for up, down in db.execute("SELECT parent_id, child_id FROM edges WHERE resource_id='materials'"):
        parent.setdefault(down, up)
        kids.setdefault(up, []).append(down)
    db.close()

    def meta(node_id):
        return nodes[node_id][1]

    def number(node_id):
        label, _, payload = nodes[node_id]
        for raw in ((json.loads(payload).get("value") if payload else None), label):
            try:
                return float(raw)
            except (TypeError, ValueError):
                pass
        return None

    def below(node_id, wanted):
        return next((kid for kid in kids.get(node_id, []) if meta(kid) == wanted), None)

    def value_under(node_id):
        holder = below(node_id, "Числовое значение")
        return None if holder is None else number(holder)

    def interval(element_id):
        for form in kids.get(element_id, []):
            if meta(form) == "Числовой интервал":
                low, high = below(form, "Нижняя граница"), below(form, "Верхняя граница")
                low = None if low is None else value_under(low)
                high = None if high is None else value_under(high)
                return None if low is None or high is None else (low, high)
            if meta(form) == "Не более":
                high = value_under(form)
                return None if high is None else (0.0, high)
            if meta(form) == "Не менее":
                low = value_under(form)
                return None if low is None else (low, 100.0)
            if meta(form) == "Числовое значение":
                value = number(form)
                return None if value is None else (value, value)
        return None  # «Расчетное», нет значения

    result = {}
    for node_id, (label, kind, _) in nodes.items():
        if kind != "Химический элемент":
            continue
        up = parent.get(node_id)
        if up is not None and meta(up) == "Совокупность химических элементов":
            aggregate, up = True, parent.get(up)
        else:
            aggregate = False
        if up is None or meta(up) != "Компонент":  # «Основа», «Расчетное» и прочие места не считаются
            continue
        composition = parent.get(up)
        material = parent.get(composition) if composition else None
        if material is None or meta(material) != "Материал":
            continue
        result.setdefault(label, {}).setdefault(material, None if aggregate else interval(node_id))
    return result


def pages(service, conditions, **options):
    items, offset = [], 0
    while offset is not None:
        result = service.find_materials(conditions, limit=50, offset=offset, **options)
        assert result.ok, result.error
        items += result.data["materials"]
        offset = result.data["next_offset"]
    assert len(items) == result.data["total"] == len({item["node_id"] for item in items})
    return items


def test_cr_greater_than_20_is_guaranteed(service, ranges):
    found = pages(service, [{"element": "Cr", "op": ">", "value": 20}])
    assert all(item["contents"]["Cr"]["low"] > 20 for item in found)
    assert STEEL not in {item["node_id"] for item in found}
    assert {item["node_id"] for item in found} == {key for key, value in ranges["Cr"].items() if value and value[0] > 20}


def test_top_three_by_chromium(service, ranges):
    names = {}  # id → имя, чтобы развести равные диапазоны так же, как сервис
    top = service.find_materials([], sort_by="Cr", limit=3).data["materials"]
    for item in pages(service, [{"element": "Cr", "op": "present"}]):
        names[item["node_id"]] = item["name"]
    known = [(key, value) for key, value in ranges["Cr"].items() if value]
    expected = sorted(known, key=lambda pair: (-pair[1][0], -pair[1][1], names[pair[0]]))[:3]
    assert [item["node_id"] for item in top] == [key for key, _ in expected]


def test_chromium_without_nickel(service, ranges):
    found = {item["node_id"] for item in pages(service, [{"element": "Cr", "op": "present"},
                                                         {"element": "Ni", "op": "absent"}])}
    assert found == set(ranges["Cr"]) - set(ranges["Ni"])


def test_iron_at_least_25_pages_add_up(service, ranges):
    found = pages(service, [{"element": "Fe", "op": ">=", "value": 25}])
    assert {item["node_id"] for item in found} == {key for key, value in ranges["Fe"].items() if value and value[0] >= 25}


def test_bromine_is_not_in_any_composition(service):
    result = service.find_materials([{"element": "Br", "op": "present"}])
    assert result.data["total"] == 0 and "Br" in result.data["elements_not_in_compositions"]


def test_control_records(service):
    steel = next(item for item in pages(service, [{"element": "Cr", "op": "present"}, {"element": "C", "op": "present"}])
                 if item["node_id"] == STEEL)
    assert (steel["contents"]["Cr"]["low"], steel["contents"]["Cr"]["high"]) == (17.0, 19.0)
    assert (steel["contents"]["C"]["low"], steel["contents"]["C"]["high"]) == (0.0, 0.12)
    amg3 = next(item for item in pages(service, [{"element": "Mg", "op": "present"}, {"element": "Fe", "op": "present"}])
                if item["node_id"] == AMG3)
    assert (amg3["contents"]["Mg"]["low"], amg3["contents"]["Mg"]["high"]) == (3.2, 3.8)
    assert (amg3["contents"]["Fe"]["low"], amg3["contents"]["Fe"]["high"]) == (0.0, 0.5)


def test_unlisted_nickel_adds_grades_without_nickel(service, ranges):
    listed = service.find_materials([{"element": "Ni", "op": "<", "value": 1}]).data["total"]
    flagged = service.find_materials([{"element": "Ni", "op": "<", "value": 1, "include_unlisted": True}]).data["total"]
    everything = service.find_materials([]).data["total"]
    assert flagged == listed + everything - len(ranges["Ni"])


def test_query_over_all_grades_is_fast(service):
    service.find_materials([])  # таблица построена
    started = time.perf_counter()
    service.find_materials([{"element": "Cr", "op": ">", "value": 20}], sort_by="Cr")
    assert time.perf_counter() - started < 1
