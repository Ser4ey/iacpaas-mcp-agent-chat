"""Обзор данных для вкладки «Данные»: синтетический граф, без сети и реального индекса."""
import hashlib
import itertools
import sqlite3
from datetime import datetime

import pytest

from materials_chat.graph_store import GraphStore
from materials_chat.overview import build_overview, format_number, plural

OWNER = "Владелец / Мой Фонд / Загрузки / "
SECTION_KEYS = {"id", "title", "kind", "description", "stats", "items", "examples", "related", "source"}

RESOURCES = [
    {"id": "catalog", "title": "Каталог сплавов", "path": "Загрузки / Каталог сплавов", "kind": "information"},
    {"id": "elements", "title": "Элементы", "path": "Загрузки / Элементы", "kind": "information"},
    {"id": "scheme", "title": "Схема каталога", "path": "Загрузки / Схема каталога", "kind": "ontology"},
    {"id": "extra", "title": "Прочий справочник", "path": "Загрузки / Прочий справочник", "kind": "information"},
]

NOTES = {
    "title": "Данные",
    "intro": "Вступление.",
    "stats": [{"label": "Марок", "resource": "catalog", "meta": "Материал"},
              {"label": "Чего-то из незагруженного", "resource": "missing", "meta": "X"}],
    "resources": {
        "catalog": {
            "description": "Марки по классам.",
            "stats": [{"label": "Марок", "meta": "Материал"},
                      {"label": "Элементов в составах", "meta": "Химический элемент", "distinct": True},
                      {"label": "Пустое", "meta": "Нет такого", "hide_zero": True}],
            "items": {"levels": [
                {"meta": ["Класс материалов"], "count": {"Материал": ["марка", "марки", "марок"]}},
                {"meta": ["Класс материалов"], "count": {"Материал": ["марка", "марки", "марок"]}},
            ]},
            "examples": ["Ст1", "Несуществующая марка", "Ст1"],
            "related": ["scheme", "elements", "unknown"],
        },
        "scheme": {
            "description": "Схема записи.",
            "stats": [{"label": "Разделов записи", "source": "children", "root": ["Класс материалов", "Материал"]},
                      {"label": "Заполнено", "source": "children", "root": ["Класс материалов", "Материал"], "used": True},
                      {"label": "Вне набора", "source": "external_refs"}],
            "items": {"root": ["Класс материалов", "Материал"],
                      "levels": [{"count": "usage", "sort": "source"}, {"count": "usage", "sort": "source"}]},
        },
    },
    "suggested_questions": [
        "Какой состав у Ст1?",
        {"text": "Что в прочем справочнике?", "resources": ["extra"]},
        {"text": "Вопрос к незагруженному", "resources": ["missing"]},
    ],
}


class Ids:
    def __init__(self, start):
        self.counter = itertools.count(start)

    def __call__(self):
        return next(self.counter)


def material(ids, name, symbols):
    components = [{"id": ids(), "name": str(index + 1), "meta": "Компонент", "successors": [
        {"id": ids(), "name": symbol, "meta": "Химический элемент",
         "original": OWNER + f"Элементы$/Химические элементы/{symbol};",
         "successors": [{"id": ids(), "value": 1.0, "meta": "Числовое значение"}]}]}
        for index, symbol in enumerate(symbols)]
    return {"id": ids(), "name": name, "meta": "Материал", "successors": [
        {"id": ids(), "name": "Элементный состав", "meta": "Элементный состав", "successors": components}]}


def catalog_graph():
    ids = Ids(100)
    stainless = {"id": ids(), "name": "Нержавеющие", "meta": "Класс материалов", "successors": [
        material(ids, "12Х18", ["Fe", "Cr"]), material(ids, "08Х18", ["Fe", "Cr", "Ni"])]}
    steels = {"id": ids(), "name": "Стали", "meta": "Класс материалов", "successors": [
        material(ids, "Ст1", ["Fe", "C"]), material(ids, "Ст2", ["Fe", "C"]), material(ids, "Ст3", ["Fe"]),
        stainless]}
    bronzes = {"id": ids(), "name": "Бронзы", "meta": "Класс материалов", "successors": [material(ids, "БрО10", ["Cu"])]}
    empty = {"id": ids(), "name": "Пустой класс", "meta": "Класс материалов", "successors": []}
    return {"title": "Каталог сплавов", "id": 1, "name": "Каталог сплавов", "meta": "Схема каталога",
            "type": "КОРЕНЬ", "date": "28.08.2026-11:32:09.036", "ontology": OWNER + "Схема каталога$;",
            "comment": "Тестовый каталог", "successors": [bronzes, empty, steels]}


def elements_graph():
    ids = Ids(2000)
    return {"title": "Элементы", "id": 2, "name": "Элементы", "meta": "Схема элементов",
            "ontology": "Схема элементов$;", "successors": [
                {"id": ids(), "name": "Химические элементы", "meta": "Химические элементы", "successors": [
                    {"id": ids(), "name": name, "meta": "Химический элемент"} for name in ("Fe", "Cr", "Ni", "C", "Cu")]}]}


def scheme_graph():
    ids = Ids(3000)
    record = {"id": ids(), "name": "Материал", "meta": "описать элемент списка", "successors": [
        {"id": ids(), "name": "Элементный состав", "meta": "описать элемент списка", "successors": [
            {"id": ids(), "name": "Компонент", "meta": "описать элемент списка"}]},
        {"id": ids(), "name": "Аналоги", "meta": "описать элемент списка", "successors": [
            {"id": ids(), "name": "Материал", "meta": "описать элемент списка"}]},
        {"id": ids(), "name": "Микроструктура", "meta": "описать элемент списка",
         "link": "Онтология вне набора$/Термин;"}]}
    return {"title": "Схема каталога", "id": 3, "name": "Схема каталога", "meta": "Язык ИРУО",
            "ontology": "Язык ИРУО$;", "successors": [
                {"id": ids(), "name": "Класс материалов", "meta": "описать элемент списка", "successors": [record]}]}


def extra_graph():
    ids = Ids(4000)
    return {"title": "Прочий справочник", "id": 4, "name": "Прочий справочник", "meta": "Схема прочего",
            "ontology": OWNER + "Схема прочего$;", "date": "01.02.2026-10:00:00.000", "successors": [
                {"id": ids(), "name": "Раздел А", "meta": "Раздел", "successors": [
                    {"id": ids(), "name": "Термин 1", "meta": "Термин", "successors": [
                        {"id": ids(), "name": "синоним", "meta": "синоним"}]},
                    {"id": ids(), "name": "Термин 2", "meta": "Термин"}]},
                {"id": ids(), "name": "Раздел Б", "meta": "Раздел", "successors": [
                    {"id": ids(), "name": "Термин 3", "meta": "Термин"}]},
                # Два одинаковых соседа различаются по комментарию.
                {"id": ids(), "name": "Характеристика", "meta": "Раздел", "comment": "вариантная"},
                {"id": ids(), "name": "Характеристика", "meta": "Раздел", "comment": "составная\nвторая строка"}]}


GRAPHS = {"catalog": catalog_graph, "elements": elements_graph, "scheme": scheme_graph, "extra": extra_graph}


@pytest.fixture
def index_path(tmp_path):
    path = tmp_path / "search.sqlite3"
    store = GraphStore(path)
    for resource_id, graph in GRAPHS.items():
        store.import_graph(resource_id, graph())
    return path


def sections_by_id(result):
    return {section["id"]: section for section in result["sections"]}


def walk_items(items):
    for item in items:
        yield item
        yield from item.get("children", [])


def test_response_follows_contract(index_path):
    result = build_overview(index_path, RESOURCES, NOTES)

    assert set(result) == {"ready", "generated_at", "title", "intro", "stats", "sections", "suggested_questions"}
    assert result["ready"] is True
    assert datetime.fromisoformat(result["generated_at"]).utcoffset().total_seconds() == 0
    assert (result["title"], result["intro"]) == ("Данные", "Вступление.")
    assert [section["id"] for section in result["sections"]] == [item["id"] for item in RESOURCES]
    for stat in result["stats"] + [stat for section in result["sections"] for stat in section["stats"]]:
        assert set(stat) == {"label", "value"} and isinstance(stat["value"], str)
    for section in result["sections"]:
        assert set(section) == SECTION_KEYS
        assert section["kind"] in ("data", "ontology")
        assert set(section["source"]) == {"path", "indexed_at", "platform_modified"}
        assert all(isinstance(item, str) for item in section["examples"] + section["related"])
        for item in section["items"]:
            assert set(item) <= {"title", "count", "count_label", "children"}
            for child in item.get("children", []):
                assert set(child) <= {"title", "count", "count_label"}  # не глубже двух уровней
        for item in walk_items(section["items"]):
            assert ("count" in item) == ("count_label" in item)
            assert "count" not in item or isinstance(item["count"], int)
    kinds = {section["id"]: section["kind"] for section in result["sections"]}
    assert kinds == {"catalog": "data", "elements": "data", "scheme": "ontology", "extra": "data"}


def test_counts_labels_and_stats_follow_notes(index_path):
    result = build_overview(index_path, RESOURCES, NOTES)
    catalog = sections_by_id(result)["catalog"]

    # Классы сортируются по числу марок; подклассы учитываются и у родителя.
    assert catalog["items"] == [
        {"title": "Стали", "count": 5, "count_label": "марок",
         "children": [{"title": "Нержавеющие", "count": 2, "count_label": "марки"}]},
        {"title": "Бронзы", "count": 1, "count_label": "марка"},
        {"title": "Пустой класс", "count": 0, "count_label": "марок"},
    ]
    assert catalog["stats"] == [{"label": "Марок", "value": "6"}, {"label": "Элементов в составах", "value": "5"},
                                {"label": "Вершин", "value": "50"}]
    assert catalog["description"] == "Марки по классам."
    assert catalog["examples"] == ["Ст1"]  # выдуманная марка и повтор отброшены
    assert catalog["related"] == ["scheme", "elements"]  # неизвестный id отброшен
    assert catalog["source"] == {"path": "Загрузки / Каталог сплавов", "indexed_at": catalog["source"]["indexed_at"],
                                 "platform_modified": "28.08.2026"}
    assert catalog["source"]["indexed_at"]
    assert result["stats"][:2] == [{"label": "Ресурсов", "value": "4"}, {"label": "Марок", "value": "6"}]
    assert {"label": "Вершин", "value": format_number(50 + 7 + 8 + 9)} in result["stats"]
    assert result["suggested_questions"] == ["Какой состав у Ст1?", "Что в прочем справочнике?"]


def test_ontology_counts_usage_in_described_base(index_path):
    scheme = sections_by_id(build_overview(index_path, RESOURCES, NOTES))["scheme"]

    assert scheme["items"] == [
        {"title": "Элементный состав", "count": 6, "count_label": "раз в базе",
         "children": [{"title": "Компонент", "count": 11, "count_label": "раз в базе"}]},
        # «Материал» внутри «Аналогов» не путается с самими марками.
        {"title": "Аналоги", "count": 0, "count_label": "раз в базе",
         "children": [{"title": "Материал", "count": 0, "count_label": "раз в базе"}]},
        {"title": "Микроструктура", "count": 0, "count_label": "раз в базе"},
    ]
    assert scheme["stats"][:3] == [{"label": "Разделов записи", "value": "3"}, {"label": "Заполнено", "value": "1"},
                                   {"label": "Вне набора", "value": "1"}]


def test_resource_without_notes_gets_tree_defaults(index_path):
    sections = sections_by_id(build_overview(index_path, RESOURCES, NOTES))
    extra, elements = sections["extra"], sections["elements"]

    assert extra["items"][:2] == [
        {"title": "Раздел А", "count": 3, "count_label": "вершины",
         "children": [{"title": "Термин 1", "count": 1, "count_label": "вершина"},
                      {"title": "Термин 2", "count": 0, "count_label": "вершин"}]},
        {"title": "Раздел Б", "count": 1, "count_label": "вершина",
         "children": [{"title": "Термин 3", "count": 0, "count_label": "вершин"}]},
    ]
    assert [item["title"] for item in extra["items"][2:]] == ["Характеристика — вариантная",
                                                             "Характеристика — составная"]
    assert extra["examples"] == ["Термин 1", "Термин 2", "Термин 3", "Характеристика — вариантная",
                                 "Характеристика — составная"]
    assert "Схема прочего" in extra["description"] and "нет среди выданных" in extra["description"]
    assert extra["stats"] == [{"label": "Вершин", "value": "9"}]
    assert extra["source"]["platform_modified"] == "01.02.2026"
    # Связи без заметок находятся по ссылкам: каталог ссылается на элементы.
    assert elements["related"] == ["catalog"]


def test_long_levels_are_truncated_with_rest_counter(tmp_path):
    path = tmp_path / "big.sqlite3"
    ids = Ids(10)
    GraphStore(path).import_graph("big", {"id": 1, "name": "Корень", "meta": "Корень", "successors": [
        {"id": ids(), "name": f"Пункт {index:02}", "meta": "Пункт"} for index in range(45)]})
    result = build_overview(path, [{"id": "big", "title": "Большой", "path": "Большой", "kind": "information"}], {})

    titles = [item["title"] for item in result["sections"][0]["items"]]
    assert len(titles) == 31 and titles[-1] == "…и ещё 15"


def test_missing_file_is_not_ready_and_not_created(tmp_path):
    path = tmp_path / "нет файла" / "search.sqlite3"
    result = build_overview(path, RESOURCES, NOTES)

    assert result["ready"] is False
    assert (result["sections"], result["stats"], result["suggested_questions"]) == ([], [], [])
    assert result["title"] == "Данные"
    assert not path.exists() and not path.parent.exists()


@pytest.mark.parametrize("kind", ["empty_file", "empty_schema"])
def test_empty_index_is_not_ready(tmp_path, kind):
    path = tmp_path / "search.sqlite3"
    if kind == "empty_file":
        sqlite3.connect(path).close()
    else:
        GraphStore(path)
    result = build_overview(path, RESOURCES, NOTES)

    assert result["ready"] is False and result["sections"] == []


def test_partially_loaded_index_marks_missing_resource(tmp_path):
    path = tmp_path / "search.sqlite3"
    GraphStore(path).import_graph("catalog", catalog_graph())
    result = build_overview(path, RESOURCES, NOTES)
    sections = sections_by_id(result)

    assert result["ready"] is False
    assert result["stats"][0] == {"label": "Ресурсов", "value": "1 из 4"}
    assert sections["catalog"]["items"]
    assert sections["extra"]["stats"] == [{"label": "Состояние", "value": "не загружен"}]
    assert sections["extra"]["items"] == [] and sections["extra"]["source"]["indexed_at"] is None
    assert result["suggested_questions"] == ["Какой состав у Ст1?"]


def test_index_is_read_only_and_connection_closed(index_path):
    before = hashlib.sha256(index_path.read_bytes()).hexdigest()
    build_overview(index_path, RESOURCES, NOTES)

    assert hashlib.sha256(index_path.read_bytes()).hexdigest() == before
    # На Windows открытый файл SQLite удалить нельзя: удаление доказывает, что соединение закрыто.
    for file in index_path.parent.glob(index_path.name + "*"):
        file.unlink()


def test_numbers_and_plural_forms():
    assert format_number(93593) == "93 593"
    assert format_number(1334) == "1 334"
    assert format_number(8) == "8"
    forms = ["марка", "марки", "марок"]
    assert [plural(n, forms) for n in (1, 2, 5, 11, 14, 21, 22, 25, 111, 1043)] == [
        "марка", "марки", "марок", "марок", "марок", "марка", "марки", "марок", "марок", "марки"]
    assert plural(7, "в базе") == "в базе"
