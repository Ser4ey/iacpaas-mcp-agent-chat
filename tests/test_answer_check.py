"""Сверка чисел ответа с данными инструментов: чистые функции, без сети."""
from decimal import Decimal

from materials_chat.answer_check import check_numbers, data_numbers, extract_numbers

SEARCH = {"id": "s", "name": "search_graph", "arguments": {"query": "12Х18Н10Т", "limit": 3}, "is_error": False,
          "duration_ms": 9500, "result": {"ok": True, "data": {
              "hits": [{"node_id": "381405980786784", "label": "12Х18Н10Т", "path": "Справочник/12Х18Н10Т",
                        "comment": "плотность 7900 кг/м³"}],
              "total": 24, "limit": 3, "offset": 0, "next_offset": 10300},
              "meta": {"indexed_at": "2026-10-18T09:59:24+00:00"}}}
FIND = {"id": "f", "name": "find_materials", "arguments": {"conditions": [{"element": "Cr", "op": ">", "value": 20}]},
        "is_error": False, "result": {"ok": True, "data": {"total": 2, "materials": [
            {"node_id": "200", "name": "Х25", "contents": {"Cr": {"form": "Числовой интервал", "low": 23.0,
                                                                  "high": 27.0, "text": "23.0–27.0 %"}}}]}}}
FAILED = {"id": "x", "name": "get_subgraph", "arguments": {"resource_id": "materials", "root": "555"}, "is_error": True,
          "result": {"ok": False, "error": {"code": "NODE_NOT_FOUND", "message": "Вершина 555 не найдена"}}}


def written(text):
    return [raw for raw, _ in extract_numbers(text)]


def test_numbers_are_extracted_as_written():
    assert written("Cr 17–19 %, C не более 0,12, плотность 7 900 кг/м³, всего 2 670 вершин.") == \
        ["17", "19", "0,12", "7 900", "2 670"]
    assert written("Диапазон 17-19 и −196 °C, итог 19.") == ["17", "19", "196", "19"]
    assert written("**17.0** в вершине `381405980786784`, Fe≤0,5, Ti до 5·C") == ["17.0", "381405980786784", "0,5", "5"]


def test_list_markers_grade_names_and_dates_are_skipped():
    assert written("1. Марка 12Х18Н10Т — хром 17.0\n2) АК4-1, Б83 и Д16\n- снимок от 21.10.2026 и 2026-10-18") == ["17.0"]


def test_dates_with_month_names_are_skipped():
    assert written("Снимок от 18 октября 2026 г., обновлён 1 марта 2027 года; хром 17.0") == ["17.0"]
    assert written("Дата снимка: 9 декабря 2026 г.") == []


def test_table_row_numbers_are_skipped():
    table = "| № | Марка | Cr, % |\n|---|---|---|\n| 1 | Х25 | 23–27 |\n|  2. | Х17 | 16–18 |\n| 3) | Х13 | 12–14 |"
    assert written(table) == ["23", "27", "16", "18", "12", "14"]
    top = "Топ по хрому:\n\n| № | Марка | Cr, % |\n|:-:|---|---|\n| 1 | Х25 | 23–27 |\n| 2 | Х25 | 23.0–27.0 |"
    assert check_numbers(top, "Где больше всего хрома?", [FIND]) == \
        {"status": "checked", "checked": 2, "confirmed": 2, "unconfirmed": []}


def test_values_compare_by_number():
    assert {value for _, value in extract_numbers("17 17.0 17,0")} == {Decimal("17")}
    assert extract_numbers("2 670")[0][1] == Decimal("2670")
    # Разделитель тысяч — неразрывный или узкий неразрывный пробел, как пишут модели.
    assert extract_numbers("2 670 и 7 900") == [("2 670", Decimal("2670")), ("7 900", Decimal("7900"))]


def test_only_data_fields_count():
    numbers = data_numbers([SEARCH, FIND, FAILED, {"id": "old", "name": "search_graph"}])
    assert {Decimal(item) for item in ("381405980786784", "7900", "24", "23", "27", "2")} <= numbers
    assert not {Decimal(item) for item in ("3", "0", "10300", "9500", "555", "2026")} & numbers


def test_answer_numbers_are_checked_against_data():
    answer = ("Хрома больше 20 % у 2 марок: **Х25** (23–27 %). Плотность 12Х18Н10Т — 7 900 кг/м³, "
              "вершина 381405980786784. Показаны 3 из 24.")
    assert check_numbers(answer, "Где хрома больше 20 %? Покажи 3", [SEARCH, FIND]) == \
        {"status": "checked", "checked": 6, "confirmed": 6, "unconfirmed": []}


def test_missing_numbers_are_listed_once_as_written():
    result = check_numbers("Прочность 9500 МПа, после отжига 10 300 МПа; снова 9500.", "Прочность?", [SEARCH])
    assert result == {"status": "checked", "checked": 2, "confirmed": 0, "unconfirmed": ["9500", "10 300"]}


def test_arguments_of_failed_calls_are_not_trusted():
    # Модель придумала вершину 555, получила NODE_NOT_FOUND и всё равно на неё сослалась.
    assert check_numbers("Подробности — в вершине 555.", "Состав Х25?", [FIND, FAILED]) == \
        {"status": "checked", "checked": 1, "confirmed": 0, "unconfirmed": ["555"]}
    # Порог из аргументов удачного вызова не проверяется: модель взяла его в запрос.
    assert check_numbers("Хрома больше 20 %: Х25.", "Где много хрома?", [FIND])["status"] == "no_numbers"


def test_no_data_and_no_numbers():
    assert check_numbers("Хром — 17 %.", "Хром?", []) == {"status": "no_data", "checked": 1, "confirmed": 0,
                                                          "unconfirmed": []}
    assert check_numbers("Хром — 17 %.", "Хром?", [FAILED])["status"] == "no_data"
    assert check_numbers("Здравствуйте! Спросите о сплавах.", "Привет", [SEARCH])["status"] == "no_numbers"
    assert check_numbers("Вы спрашивали про 20 %.", "Больше 20 %?", [SEARCH])["status"] == "no_numbers"
