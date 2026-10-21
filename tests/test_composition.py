"""Таблица состава и поиск марок по составу: синтетический справочник в форме universal JSON."""
import itertools
import json
import sqlite3

import pytest

from materials_chat.graph_store import GraphStore

_ids = itertools.count(10_000)


def node(name, meta, *successors, value=None):
    item = {"id": next(_ids), "name": name, "meta": meta, "type": "НЕТЕРМИНАЛ", "successors": list(successors)}
    if value is not None:
        item.update(type="ТЕРМИНАЛ-ЗНАЧЕНИЕ", value=value)
    return item


def number(value):
    return node(str(value), "Числовое значение", value=value)


def element(symbol, *form):
    # Как в справочнике: под символом элемента — «%» и форма значения.
    return node(symbol, "Химический элемент", node("%", "%", value="%"), *form)


def interval(low, high):
    return node("Числовой интервал", "Числовой интервал",
                node("Нижняя граница", "Нижняя граница", number(low)),
                node("Верхняя граница", "Верхняя граница", number(high)))


def at_most(value):
    return node("Не более", "Не более", node("≤", "≤", value="≤"), number(value))


def at_least(value):
    return node("Не менее", "Не менее", node("≥", "≥", value="≥"), number(value))


def material(material_id, name, base, *components):
    parts = [node(str(index), "Компонент", part) for index, part in enumerate(components, 1)]
    return {"id": material_id, "name": name, "meta": "Материал", "type": "НЕТЕРМИНАЛ", "successors": [
        node("Элементный состав", "Элементный состав",
             node("Основа", "Основа", node(base, "Химический элемент", value=base)), *parts)]}


def material_class(name, *items):
    return node(name, "Класс материалов", *items)


def catalog():
    return {"title": "Справочник по материалам", "id": 1, "name": "Справочник по материалам", "type": "КОРЕНЬ",
            "meta": "Онтология справочника по материалам", "json_type": "universal", "successors": [
        material_class("Сплавы на основе железа",
            material(100, "12Х18Н10Т", "Fe", element("Fe", interval(66.7, 70.0)), element("Cr", interval(17.0, 19.0)),
                     element("Ni", interval(9.0, 11.0)), element("C", at_most(0.12)),
                     element("Ti", node("Расчетное", "Расчетное"))),
            material(200, "Х25", "Fe", element("Fe", interval(70.0, 75.0)), element("Cr", interval(23.0, 27.0)),
                     element("Mo", number(1.5))),
            material(300, "Х20", "Fe", element("Fe", interval(76.0, 80.0)), element("Cr", interval(20.0, 22.0)),
                     element("Ni", at_most(0.6))),
            material(350, "Х21", "Fe", element("Fe", interval(72.0, 79.0)), element("Cr", at_least(21.0)))),
        material_class("Сплавы на основе алюминия",
            material(400, "АМг3", "Al", element("Al", interval(93.8, 96.0)), element("Mg", interval(3.2, 3.8)),
                     element("Fe", at_most(0.5)), element("Cr", at_most(0.05)))),
        material_class("Редкоземельные сплавы", material_class("Магнитные",
            material(500, "КС25", "Sm", element("Sm", number(25.0)), element("Ni"),
                     node("Совокупность химических элементов", "Совокупность химических элементов",
                          node("Nb", "Химический элемент", value="Nb"), node("Ti", "Химический элемент", value="Ti"),
                          node("Содержание", "Содержание", node("%", "%", value="%"), at_most(0.12))),
                     node("Прочее", "Прочее", node("%", "%", value="%"), number(0.1))))),
    ]}


def elements_base():
    return {"title": "База химических элементов и веществ", "id": 900, "name": "База химических элементов и веществ",
            "type": "КОРЕНЬ", "json_type": "universal", "successors": [
        node("Химические элементы", "Химические элементы",
             node("Хром", "Химический элемент", node("Cr", "обозначение", value="Cr"),
                  node("24", "порядковый номер", value=24)),
             node("Железо", "Химический элемент", node("Fe", "обозначение", value="Fe")),
             node("Бром", "Химический элемент", node("Br", "обозначение", value="Br")))]}


ALL = ["12Х18Н10Т", "АМг3", "КС25", "Х20", "Х21", "Х25"]


@pytest.fixture
def store(tmp_path):
    result = GraphStore(tmp_path / "graph.sqlite3")
    result.import_graph("materials", catalog())
    return result


def by_name(records):
    return {item["name"]: item for item in records}


def test_value_forms_become_ranges(store):
    records = store.composition("materials")
    assert [item["name"] for item in records] == ALL
    steel = by_name(records)["12Х18Н10Т"]
    assert (steel["node_id"], steel["base"], steel["classes"]) == ("100", "Fe", ["Сплавы на основе железа"])
    assert steel["contents"]["Cr"] == {"form": "Числовой интервал", "low": 17.0, "high": 19.0, "text": "17.0–19.0 %"}
    assert steel["contents"]["C"] == {"form": "Не более", "low": 0.0, "high": 0.12, "text": "не более 0.12 %"}
    assert steel["contents"]["Ti"] == {"form": "Расчетное", "low": None, "high": None,
                                       "text": "расчётное значение, см. запись марки"}
    assert by_name(records)["Х21"]["contents"]["Cr"] == {"form": "Не менее", "low": 21.0, "high": 100.0,
                                                         "text": "не менее 21.0 %"}
    assert by_name(records)["Х25"]["contents"]["Mo"] == {"form": "Числовое значение", "low": 1.5, "high": 1.5,
                                                         "text": "1.5 %"}


def test_unknown_forms_nested_classes_and_other_components(store):
    alloy = by_name(store.composition("materials"))["КС25"]
    assert alloy["classes"] == ["Редкоземельные сплавы", "Магнитные"] and alloy["base"] == "Sm"
    assert set(alloy["contents"]) == {"Sm", "Ni", "Nb", "Ti"}  # «Прочее» — не элемент
    assert alloy["contents"]["Ni"] == {"form": "нет значения", "low": None, "high": None, "text": "значение не указано"}
    assert alloy["contents"]["Nb"]["form"] == alloy["contents"]["Ti"]["form"] == "Совокупность химических элементов"
    assert alloy["contents"]["Ti"]["low"] is None


def test_existing_index_gets_the_table_on_first_use(tmp_path):
    path = tmp_path / "graph.sqlite3"
    GraphStore(path).import_graph("materials", catalog())
    db = sqlite3.connect(path)  # индекс, построенный до появления таблицы состава
    db.executescript("DROP TABLE composition_materials; DROP TABLE composition_elements; DROP TABLE composition_state;")
    db.close()
    assert [item["name"] for item in GraphStore(path).composition("materials")] == ALL


def test_stale_table_is_rebuilt(store):
    with store.connect() as db:
        db.execute("DELETE FROM composition_materials")
        db.execute("UPDATE composition_state SET indexed_at='2000-01-01T00:00:00+00:00'")
    assert len(store.composition("materials")) == 6


def test_reimport_replaces_the_table(store):
    changed = catalog()
    changed["successors"] = changed["successors"][1:2]  # остался только класс алюминиевых сплавов
    store.import_graph("materials", changed)
    assert [item["name"] for item in store.composition("materials")] == ["АМг3"]


def test_unindexed_resource_has_no_composition(store):
    with pytest.raises(KeyError):
        store.composition("other")


def test_element_names_come_from_the_elements_base(store):
    store.import_graph("elements", elements_base())
    assert store.element_symbols() == {"хром": "Cr", "железо": "Fe", "бром": "Br"}


from materials_chat.config import Settings
from materials_chat.service import GraphService


def make_service(tmp_path, imported=True):
    config = tmp_path / "resources.json"
    config.write_text(json.dumps([
        {"id": "materials", "title": "Справочник по материалам", "path": "Тест / Материалы", "kind": "information"},
        {"id": "elements", "title": "Элементы", "path": "Тест / Элементы", "kind": "information"},
    ], ensure_ascii=False), encoding="utf-8")
    result = GraphService(Settings(_env_file=None, resources_path=config, data_dir=tmp_path / "data", live_reads=False,
                                   iacpaas_username="", iacpaas_password="", openrouter_api_key=""))
    if imported:
        result.store.import_graph("materials", catalog())
        result.store.import_graph("elements", elements_base())
    return result


@pytest.fixture
def service(tmp_path):
    return make_service(tmp_path)


def find(service, *conditions, **options):
    return service.find_materials(list(conditions), **options)


def names(result):
    assert result.ok, result.error
    return [item["name"] for item in result.data["materials"]]


def test_thresholds_are_guaranteed_by_the_range(service):
    assert names(find(service, {"element": "Cr", "op": ">", "value": 20})) == ["Х21", "Х25"]
    assert names(find(service, {"element": "Cr", "op": ">=", "value": 20})) == ["Х20", "Х21", "Х25"]
    assert names(find(service, {"element": "Fe", "op": "<", "value": 1})) == ["АМг3"]
    assert names(find(service, {"element": "Fe", "op": "<=", "value": 0.5})) == ["АМг3"]
    result = find(service, {"element": "Cr", "op": ">", "value": 20})
    assert (result.data["total"], result.data["has_more"], result.data["next_offset"]) == (2, False, None)
    assert result.data["materials"][0] == {"node_id": "350", "name": "Х21", "class": "Сплавы на основе железа",
        "base": "Fe", "contents": {"Cr": {"form": "Не менее", "low": 21.0, "high": 100.0, "text": "не менее 21.0 %"}}}
    assert result.data["resource_id"] == "materials" and result.data["rules"].startswith("«> X» — нижняя граница")
    assert result.meta["served_from"] == "local_index" and result.meta["indexed_at"]


def test_presence_absence_and_unknown_ranges(service):
    assert names(find(service, {"element": "Cr", "op": "present"}, {"element": "Ni", "op": "absent"})) == \
        ["АМг3", "Х21", "Х25"]
    # «Расчетное» и «совокупность»: элемент есть, диапазон неизвестен — числовые условия не проходят.
    assert names(find(service, {"element": "Ti", "op": "present"})) == ["12Х18Н10Т", "КС25"]
    assert names(find(service, {"element": "Ti", "op": ">", "value": 0})) == []
    assert names(find(service, {"element": "Ni", "op": "<", "value": 1})) == ["Х20"]


def test_unlisted_element_passes_less_than_only_with_the_flag(service):
    result = find(service, {"element": "Ni", "op": "<", "value": 1, "include_unlisted": True})
    assert names(result) == ["АМг3", "Х20", "Х21", "Х25"]
    assert result.data["materials"][0]["contents"]["Ni"] == {"form": "не указан в составе", "low": None,
                                                             "high": None, "text": "не указан в составе"}


def test_sorting_by_element_puts_impurity_limits_last(service):
    top = find(service, sort_by="Cr", limit=3)
    assert names(top) == ["Х25", "Х21", "Х20"] and top.data["total"] == 5 and top.data["has_more"]
    assert names(find(service, sort_by="Cr", order="asc")) == ["АМг3", "12Х18Н10Т", "Х20", "Х21", "Х25"]
    assert names(find(service, sort_by="Ti")) == ["12Х18Н10Т", "КС25"]  # неизвестные диапазоны — в конце


def test_elements_absent_from_all_compositions(service):
    present = find(service, {"element": "Br", "op": "present"})
    assert present.ok and present.data["total"] == 0 and present.data["elements_not_in_compositions"] == ["Br"]
    assert find(service, {"element": "W", "op": "absent"}).data["total"] == 6
    assert find(service, {"element": "Cr", "op": "present"}).data["elements_not_in_compositions"] == []


def test_element_spellings_and_russian_names_are_resolved(service):
    for spelling in ("Cr", "CR", " cr ", "Хром", "хром"):
        result = find(service, {"element": spelling, "op": ">", "value": 20})
        assert names(result) == ["Х21", "Х25"], spelling
        assert set(result.data["materials"][0]["contents"]) == {"Cr"}
    assert names(find(service, {"element": "железо", "op": "<", "value": 1})) == ["АМг3"]
    unknown = find(service, {"element": "унобтаний", "op": "present"})
    assert unknown.ok and unknown.data["elements_not_in_compositions"] == ["унобтаний"]


def test_class_filter_includes_nested_classes(service):
    assert names(find(service, material_class="магнитные")) == ["КС25"]
    assert names(find(service, material_class="Редкоземельные сплавы")) == ["КС25"]
    assert names(find(service, {"element": "Cr", "op": ">", "value": 20}, material_class="сплавы на основе железа")) \
        == ["Х21", "Х25"]
    assert find(service, material_class="Магнитные").data["materials"][0]["class"] == "Редкоземельные сплавы / Магнитные"
    unknown = find(service, material_class="Бронзы")
    assert not unknown.ok and unknown.error.code == "UNKNOWN_CLASS"
    assert "Сплавы на основе алюминия" in unknown.error.message


def test_overview_without_conditions_is_alphabetical_and_paged(service):
    pages, offset = [], 0
    while offset is not None:
        page = find(service, limit=4, offset=offset)
        pages.append(names(page))
        offset = page.data["next_offset"]
    assert pages == [["12Х18Н10Т", "АМг3", "КС25", "Х20"], ["Х21", "Х25"]]
    assert find(service, limit=1).data["materials"][0]["contents"] == {}


@pytest.mark.parametrize("conditions, options", [
    ([{"element": "Cr", "op": ">"}], {}),
    ([{"element": "Cr", "op": "present", "value": 1}], {}),
    ([{"element": "Cr", "op": "~", "value": 1}], {}),
    ([{"element": "Cr", "op": ">", "value": 150}], {}),
    ([{"element": "Cr", "op": ">", "value": 1, "include_unlisted": True}], {}),
    ([{"op": ">", "value": 1}], {}),
    ([{"element": "Cr", "op": "present"}] * 11, {}),
    ([], {"order": "up"}),
])
def test_invalid_conditions_are_diagnostic(service, conditions, options):
    result = service.find_materials(conditions, **options)
    assert not result.ok and result.error.code == "INVALID_CONDITION"


@pytest.mark.parametrize("options", [{"limit": 0}, {"limit": 51}, {"offset": -1}])
def test_paging_limits(service, options):
    result = service.find_materials([], **options)
    assert not result.ok and result.error.code == "LIMIT_EXCEEDED"


def test_index_not_ready(tmp_path):
    result = make_service(tmp_path, imported=False).find_materials([])
    assert not result.ok and result.error.code == "INDEX_NOT_READY"


def test_page_fits_the_result_budget(service):
    service.settings.max_result_chars = 1500
    result = find(service, {"element": "Cr", "op": "present"}, {"element": "Fe", "op": "present"}, limit=50)
    assert result.ok and result.data["has_more"] and result.data["total"] == 5
    assert result.data["next_offset"] == len(result.data["materials"]) < 5
    assert len(json.dumps(result.model_dump(), ensure_ascii=False)) <= 1500


from materials_chat.mcp_server import create_server
from materials_chat.prompts import SYSTEM_PROMPT


async def test_mcp_lists_find_materials_with_its_rules(tmp_path):
    settings = make_service(tmp_path).settings
    tools = {tool.name: tool for tool in await create_server(settings).list_tools()}
    assert len(tools) == 7
    assert {"conditions", "material_class", "sort_by", "order", "limit", "offset"} <= \
        set(tools["find_materials"].inputSchema["properties"])
    assert "include_unlisted" in json.dumps(tools["find_materials"].inputSchema, ensure_ascii=False)
    description = tools["find_materials"].description
    assert "get_subgraph" in description and "include_unlisted" in description and "next_offset" in description
    assert "до 20" in tools["search_graph"].description and "has_more: false" in tools["search_graph"].description


async def test_mcp_find_materials_returns_the_envelope(tmp_path):
    settings = make_service(tmp_path).settings
    _, structured = await create_server(settings).call_tool(
        "find_materials", {"conditions": [{"element": "Cr", "op": ">", "value": 20}], "sort_by": "Cr"})
    assert structured["ok"] and [item["name"] for item in structured["data"]["materials"]] == ["Х25", "Х21"]


def test_prompt_routes_composition_questions_to_find_materials():
    assert "find_materials" in SYSTEM_PROMPT and "has_more: false" in SYSTEM_PROMPT
    assert "заполнено у N записей" in SYSTEM_PROMPT
