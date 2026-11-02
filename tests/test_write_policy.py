"""Пути «Моего фонда» и защита базовых ресурсов: без сети, по сегментам пути."""
import json

import pytest

from materials_chat.config import PROJECT_ROOT
from materials_chat.fund_paths import Protection, join_path, split_path
from materials_chat.service import ResourceDefinition

DEFINITIONS = [ResourceDefinition.model_validate(item) for item in
               json.loads((PROJECT_ROOT / "config" / "resources.json").read_text(encoding="utf-8"))]


@pytest.mark.parametrize("text, segments", [
    ("Демо агента / Мои свойства", ["Демо агента", "Мои свойства"]),
    ("Мой Фонд / Демо агента", ["Демо агента"]),
    ("мой фонд/Демо агента/", ["Демо агента"]),
    ("Pristup.SA@vvsu.ru / Мой Фонд / Демо агента / Мои свойства$;", ["Демо агента", "Мои свойства"]),
    ("//Демо агента//  Мои свойства ", ["Демо агента", "Мои свойства"]),
    ("Демо агента/‴Вт/(м·К)‴", ["Демо агента", "Вт/(м·К)"]),
    ("", []),
    ("Мой Фонд", []),
])
def test_split_path_variants(text, segments):
    assert split_path(text) == segments


def test_my_fund_in_the_middle_is_rejected():
    with pytest.raises(ValueError):
        split_path("Демо агента / Мой Фонд / X")


def test_join_path_quotes_slashes():
    assert join_path(["Демо агента", "г/л"]) == "Демо агента / ‴г/л‴"


@pytest.mark.parametrize("text", [
    *[item.path for item in DEFINITIONS],
    "Загрузки",
    "Загрузки / Новая папка / Ресурс",
    "загрузки//справочник по материалам",
    "ЗАГРУЗКИ/База свойств материалов/",
    "Pristup.SA@vvsu.ru/Мой Фонд/Загрузки/Онтология базы свойств материалов$;",
    "Мой Фонд / Загрузки",
    "Личное / Секрет",
    "личное/секрет/вложенный",
])
def test_protection_matrix(text):
    protection = Protection(DEFINITIONS, "Личное / Секрет ; ;")
    assert protection.rule(split_path(text)) is not None


@pytest.mark.parametrize("text", ["Демо агента / Мои свойства", "Демо агента", "Загрузки2 / X", "Личное"])
def test_unprotected_paths(text):
    assert Protection(DEFINITIONS, "Личное / Секрет").rule(split_path(text)) is None


def test_rule_names_the_base_resource():
    rule = Protection(DEFINITIONS, "").rule(split_path("Загрузки / Справочник по материалам"))
    assert "Загрузки" in rule
