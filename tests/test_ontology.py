"""Проверка дерева по настоящим онтологиям проекта из локального индекса (без сети)."""
import json

import pytest

from materials_chat.config import PROJECT_ROOT
from materials_chat.graph_store import GraphStore
from materials_chat.ontology import Ontology, OntologyError, describe_graph
from materials_chat.service import ResourceDefinition

REAL_INDEX = PROJECT_ROOT / "data" / "search.sqlite3"
pytestmark = pytest.mark.skipif(not REAL_INDEX.exists(), reason="нет локального индекса data/search.sqlite3")
DEFINITIONS = [ResourceDefinition.model_validate(item) for item in
               json.loads((PROJECT_ROOT / "config" / "resources.json").read_text(encoding="utf-8"))]
DEMO = [{"concept": "Класс свойств", "name": "Тепловые свойства", "children": [
    {"concept": "Свойство", "name": "Теплопроводность", "children": [
        {"concept": "Синонимы", "children": [{"concept": "синоним", "value": "коэффициент теплопроводности"}]},
        {"concept": "Возможные значения", "children": [{"concept": "Числовое значение", "value": 12.5}]}]}]}]


@pytest.fixture(scope="module")
def properties():
    return Ontology.load(GraphStore(REAL_INDEX), DEFINITIONS, "properties_ontology")


def test_structure_of_properties_ontology(properties):
    text = "\n".join(properties.structure())
    assert text.startswith("Класс свойств [вершина, нужно имя]")
    assert "\n  Свойство [вершина, нужно имя]" in text
    assert "\n      синоним [значение STRING]" in text
    assert "Числовое значение [вариант альтернативы, значение REAL]" in text
    assert "единица измерения [значение STRING; не поддерживается" in text


def test_build_demo_resource(properties):
    graph = properties.build("Мои свойства", "", DEMO)
    assert {key: graph[key] for key in ("title", "name", "type", "json_type", "meta")} == {
        "title": "Мои свойства", "name": "Мои свойства", "type": "КОРЕНЬ", "json_type": "universal",
        "meta": "Онтология базы свойств материалов"}
    assert graph["ontology"].endswith("/ Мой Фонд / Загрузки / Онтология базы свойств материалов$;")
    prop = graph["successors"][0]["successors"][0]
    assert graph["successors"][0] == {**graph["successors"][0], "type": "НЕТЕРМИНАЛ", "name": "Тепловые свойства", "meta": "Класс свойств"}
    assert prop["successors"][0] == {"type": "НЕТЕРМИНАЛ", "name": "Синонимы", "meta": "Синонимы", "successors": [
        {"type": "ТЕРМИНАЛ-ЗНАЧЕНИЕ", "value": "коэффициент теплопроводности", "valtype": "STRING", "meta": "синоним"}]}
    assert prop["successors"][1]["successors"] == [
        {"type": "ТЕРМИНАЛ-ЗНАЧЕНИЕ", "value": 12.5, "valtype": "REAL", "meta": "Числовое значение"}]


def test_describe_names_every_vertex(properties):
    text = describe_graph(properties.build("Мои свойства", "", DEMO))
    assert "Класс свойств «Тепловые свойства» → Свойство «Теплопроводность» → Синонимы → синоним «коэффициент теплопроводности»" in text
    assert "Числовое значение «12.5»" in text


def test_link_children_come_from_characteristics_ontology(properties):
    graph = properties.build("Интервал", "", [{"concept": "Класс свойств", "name": "К", "children": [
        {"concept": "Свойство", "name": "С", "children": [{"concept": "Возможные значения", "children": [
            {"concept": "Числовой интервал", "children": [{"concept": "Нижняя граница"}]}]}]}]}])
    interval = graph["successors"][0]["successors"][0]["successors"][0]["successors"][0]
    assert interval["successors"][0]["meta"] == "Нижняя граница"


def error_of(ontology, items) -> OntologyError:
    with pytest.raises(OntologyError) as caught:
        ontology.build("X", "", items)
    return caught.value


def test_concept_not_allowed_lists_options(properties):
    error = error_of(properties, [{"concept": "Класс свойств", "name": "К", "children": [
        {"concept": "Свойство", "name": "С", "children": [{"concept": "Плотность", "name": "П"}]}]}])
    assert error.code == "CONCEPT_NOT_ALLOWED"
    assert "«Свойство»" in error.message and "«Плотность»" in error.message and "Синонимы" in error.message


def test_invalid_real_value(properties):
    error = error_of(properties, [{"concept": "Класс свойств", "name": "К", "children": [
        {"concept": "Свойство", "name": "С", "children": [{"concept": "Возможные значения", "children": [
            {"concept": "Числовое значение", "value": "много"}]}]}]}])
    assert error.code == "INVALID_VALUE" and "REAL" in error.message


def test_numeric_string_is_accepted_for_real(properties):
    graph = properties.build("X", "", [{"concept": "Класс свойств", "name": "К", "children": [
        {"concept": "Свойство", "name": "С", "children": [{"concept": "Возможные значения", "children": [
            {"concept": "Числовое значение", "value": "0,5"}]}]}]}])
    assert graph["successors"][0]["successors"][0]["successors"][0]["successors"][0]["value"] == 0.5


def test_clone_unit_is_unsupported(properties):
    error = error_of(properties, [{"concept": "Класс свойств", "name": "К", "children": [
        {"concept": "Свойство", "name": "С", "children": [{"concept": "единица измерения", "value": "Вт/(м·К)"}]}]}])
    assert error.code == "UNSUPPORTED_ONTOLOGY_FEATURE" and "другого ресурса" in error.message


def test_set_member_requires_name(properties):
    error = error_of(properties, [{"concept": "Класс свойств"}])
    assert error.code == "INVALID_ARGUMENTS" and "name" in error.message


def test_reference_to_other_resource_is_unsupported():
    materials = Ontology.load(GraphStore(REAL_INDEX), DEFINITIONS, "materials_ontology")
    error = error_of(materials, [{"concept": "Класс материалов", "name": "Стали", "children": [
        {"concept": "Материал", "name": "М1", "children": [{"concept": "Элементный состав", "children": [
            {"concept": "Компонент", "name": "Ком", "children": [{"concept": "Химический элемент", "name": "Fe"}]}]}]}]}])
    assert error.code == "UNSUPPORTED_ONTOLOGY_FEATURE"


def test_structures_of_all_ontologies_fit_the_result_budget():
    store = GraphStore(REAL_INDEX)
    for resource_id in ("materials_ontology", "properties_ontology", "characteristics_ontology"):
        lines = Ontology.load(store, DEFINITIONS, resource_id).structure()
        assert lines and len(json.dumps(lines, ensure_ascii=False)) < 14000, resource_id
