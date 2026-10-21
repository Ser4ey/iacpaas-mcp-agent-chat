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
