"""Сверка чисел в ответе модели с данными, которые вернули инструменты.

Чистые функции без сети. Число, которого нет ни в данных, ни в вопросе, ни в аргументах вызовов, — повод
показать пользователю предупреждение: возможно, модель его придумала или посчитала сама.
"""
import re
from decimal import Decimal

# Поля данных, значения которых модель может пересказывать; служебные (limit, offset, indexed_at и т. п.) не берём.
DATA_KEYS = {"label", "value", "comment", "name", "path", "snippet", "node_id", "id", "root", "total", "nodes",
             "edges", "contents", "low", "high", "text", "base", "class", "title"}

_BLANK = "[ \u00a0\u202f]"  # обычный, неразрывный и узкий неразрывный пробелы
_MASKED = re.compile(r"\b\d{1,2}\.\d{1,2}\.\d{4}\b|\b\d{4}-\d{2}-\d{2}\b|^[ \t]*\d+[.)](?=\s)", re.M)
# Сначала число с пробелами-разделителями тысяч, иначе «слово»: буквы и цифры через - . ,
_TOKEN = re.compile(rf"(?<![^\W_])(?P<grouped>\d{{1,3}}(?:{_BLANK}\d{{3}})+(?:[.,]\d+)?)(?![^\W_])"
                    r"|(?P<word>[^\W_]+(?:[-.,][^\W_]+)*)")
_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")


def _to_decimal(raw: str) -> Decimal:
    return Decimal(re.sub(_BLANK, "", raw).replace(",", "."))


def extract_numbers(text: str) -> list[tuple[str, Decimal]]:
    """Числа текста: (как записано, абсолютное значение) в порядке появления."""
    # Даты и маркеры списков закрываем пробелами той же длины: числа в них не считаются
    text = _MASKED.sub(lambda match: " " * len(match.group()), text)
    found = []
    for match in _TOKEN.finditer(text):
        if match["grouped"]:
            found.append((match["grouped"], _to_decimal(match["grouped"])))
            continue
        word = match["word"]
        # Слово с буквами (марка, артикул) пропускаем целиком; остальные делим по дефису: 17-19
        parts = word.split("-")
        if all(_NUMBER.fullmatch(part) for part in parts):
            found.extend((part, _to_decimal(part)) for part in parts)
    return found


def _numbers_of(value, keys=None, key=None) -> set[Decimal]:
    """Числа во вложенной структуре; keys — допустимые ключи (None — любые)."""
    if isinstance(value, dict):
        return set().union(*(_numbers_of(item, keys, name) for name, item in value.items()), set())
    if isinstance(value, list):
        return set().union(*(_numbers_of(item, keys, key) for item in value), set())
    if keys is not None and key not in keys:
        return set()
    if isinstance(value, bool):
        return set()
    if isinstance(value, (int, float)):
        return {abs(Decimal(str(value)))}
    if isinstance(value, str):
        return {number for _, number in extract_numbers(value)}
    return set()


def _successful(traces: list[dict]) -> list[dict]:
    return [trace["result"] for trace in traces
            if isinstance(trace.get("result"), dict) and trace["result"].get("ok")]


def data_numbers(traces: list[dict]) -> set[Decimal]:
    """Числа из полей данных успешных результатов; поля записи журнала и meta не учитываются."""
    return set().union(*(_numbers_of(result.get("data"), DATA_KEYS) for result in _successful(traces)), set())


def check_numbers(answer: str, question: str, traces: list[dict]) -> dict:
    """Сверяет числа ответа с данными вызовов; числа из вопроса и аргументов не проверяются: их назвал пользователь."""
    known = _numbers_of(question) | _numbers_of([trace.get("arguments") for trace in traces])
    unique: dict[Decimal, str] = {}
    for raw, value in extract_numbers(answer):
        if value not in known:
            unique.setdefault(value, raw)
    if not unique:
        return {"status": "no_numbers", "checked": 0, "confirmed": 0, "unconfirmed": []}
    if not _successful(traces):
        return {"status": "no_data", "checked": len(unique), "confirmed": 0, "unconfirmed": []}
    available = data_numbers(traces)
    missing = [raw for value, raw in unique.items() if value not in available]
    return {"status": "checked", "checked": len(unique), "confirmed": len(unique) - len(missing),
            "unconfirmed": missing}
