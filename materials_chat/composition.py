"""Состав марок: разбор записей справочника в таблицу «марка → элемент → диапазон»."""
import json
import sqlite3

from pydantic import BaseModel, Field, ValidationError

FORM_UNLISTED = "не указан в составе"
FORM_NONE = "нет значения"
FORM_AGGREGATE = "Совокупность химических элементов"
FORM_CALCULATED = "Расчетное"

_TEXT_CALCULATED = "расчётное значение, см. запись марки"
_TEXT_AGGREGATE = "в совокупности с другими элементами, см. запись марки"
_TEXT_NONE = "значение не указано"


def _number(node: dict) -> float | None:
    """Число берётся из value, при его отсутствии — из label."""
    for raw in (node["value"], node["label"]):
        try:
            return float(raw)
        except (TypeError, ValueError):
            continue
    return None


def _child_by_meta(nodes, children, parent, meta):
    for child in children.get(parent, []):
        if nodes[child]["meta"] == meta:
            return child
    return None


def _bound(nodes, children, parent, meta):
    """Числовое значение под вершиной границы («Нижняя граница») или прямо под parent, если meta не задана."""
    holder = _child_by_meta(nodes, children, parent, meta) if meta else parent
    if holder is None:
        return None
    value = _child_by_meta(nodes, children, holder, "Числовое значение")
    return None if value is None else _number(nodes[value])


def _entry(form, low, high, text):
    return {"form": form, "low": low, "high": high, "text": text}


def _element_entry(nodes, children, element_id) -> dict:
    """Форма значения под элементом; «%» и прочие незнакомые потомки пропускаются."""
    for child in children.get(element_id, []):
        form = nodes[child]["meta"]
        if form == "Числовой интервал":
            low = _bound(nodes, children, child, "Нижняя граница")
            high = _bound(nodes, children, child, "Верхняя граница")
            if low is not None and high is not None:
                return _entry(form, low, high, f"{low}–{high} %")
        elif form == "Не более":
            high = _bound(nodes, children, child, None)
            if high is not None:
                return _entry(form, 0.0, high, f"не более {high} %")
        elif form == "Не менее":
            low = _bound(nodes, children, child, None)
            if low is not None:
                return _entry(form, low, 100.0, f"не менее {low} %")
        elif form == "Числовое значение":
            value = _number(nodes[child])
            if value is not None:
                return _entry(form, value, value, f"{value} %")
        elif form == FORM_CALCULATED:
            return _entry(form, None, None, _TEXT_CALCULATED)
    return _entry(FORM_NONE, None, None, _TEXT_NONE)


def _material_record(nodes, children, material_id, classes) -> dict:
    record = {"node_id": material_id, "name": nodes[material_id]["label"], "classes": list(classes),
              "base": None, "contents": {}}
    contents = record["contents"]
    composition = _child_by_meta(nodes, children, material_id, "Элементный состав")
    for part in children.get(composition, []):
        meta = nodes[part]["meta"]
        if meta == "Основа":
            base = _child_by_meta(nodes, children, part, "Химический элемент")
            if base is not None:
                record["base"] = nodes[base]["label"]
        elif meta == "Компонент":
            for item in children.get(part, []):
                kind = nodes[item]["meta"]
                if kind == "Химический элемент":
                    contents.setdefault(nodes[item]["label"], _element_entry(nodes, children, item))
                elif kind == FORM_AGGREGATE:
                    for member in children.get(item, []):
                        if nodes[member]["meta"] == "Химический элемент":
                            contents.setdefault(nodes[member]["label"],
                                                _entry(FORM_AGGREGATE, None, None, _TEXT_AGGREGATE))
                # «Прочее» — не элемент, в таблицу не попадает.
    return record


def material_records(nodes: dict[str, dict], children: dict[str, list[str]]) -> list[dict]:
    """Марки ресурса: обход от корней по классам материалов, копя цепочку классов."""
    has_parent = {child for kids in children.values() for child in kids}
    stack = [(node_id, []) for node_id in reversed(list(nodes)) if node_id not in has_parent]
    seen, result = set(), []
    while stack:
        node_id, classes = stack.pop()
        if node_id in seen:
            continue
        seen.add(node_id)
        meta = nodes[node_id]["meta"]
        if meta == "Материал":
            result.append(_material_record(nodes, children, node_id, classes))
            continue
        chain = classes + [nodes[node_id]["label"]] if meta == "Класс материалов" else classes
        stack.extend((child, chain) for child in reversed(children.get(node_id, [])) if child in nodes)
    return result


def rebuild_composition(db: sqlite3.Connection, resource_id: str, indexed_at: str) -> int:
    """Переписывает таблицы состава ресурса в транзакции вызывающего; возвращает число марок."""
    nodes = {}
    for row in db.execute("SELECT node_id, label, meta, payload FROM nodes WHERE resource_id=?", (resource_id,)):
        payload = json.loads(row[3]) if row[3] else {}
        nodes[row[0]] = {"label": row[1] or "", "meta": row[2] or "", "value": payload.get("value")}
    children: dict[str, list[str]] = {}
    for parent, child in db.execute("SELECT parent_id, child_id FROM edges WHERE resource_id=?", (resource_id,)):
        children.setdefault(parent, []).append(child)
    records = material_records(nodes, children)
    for table in ("composition_materials", "composition_elements", "composition_state"):
        db.execute(f"DELETE FROM {table} WHERE resource_id=?", (resource_id,))
    for record in records:
        db.execute("INSERT OR IGNORE INTO composition_materials VALUES (?,?,?,?,?)",
                   (resource_id, record["node_id"], record["name"],
                    json.dumps(record["classes"], ensure_ascii=False), record["base"]))
        db.executemany("INSERT OR IGNORE INTO composition_elements VALUES (?,?,?,?,?,?,?)",
                       [(resource_id, record["node_id"], symbol, item["form"], item["low"], item["high"], item["text"])
                        for symbol, item in record["contents"].items()])
    db.execute("INSERT INTO composition_state VALUES (?,?)", (resource_id, indexed_at))
    return len(records)


RULES = ("«> X» — нижняя граница диапазона больше X; «< X» — верхняя граница меньше X; "
         "марки без элемента в составе проходят «<» только с include_unlisted")
MAX_CONDITIONS = 10
_NUMERIC = {">", ">=", "<", "<="}
_OPS = _NUMERIC | {"present", "absent"}


class ConditionError(ValueError):
    """Условие или параметр запроса некорректны; текст сообщения уходит модели как подсказка."""


class Condition(BaseModel):
    """Одно условие на содержание элемента; все условия запроса объединяются через «И»."""
    element: str = Field(description="Элемент: символ (Cr, Ni) или русское название (хром, никель).")
    op: str = Field(description="Операция: «>», «>=», «<», «<=» (граница в процентах), "
                                "«present» — элемент есть в составе, «absent» — его нет.")
    value: float | None = Field(default=None, description="Порог в процентах, 0–100; для present/absent не задаётся.")
    include_unlisted: bool = Field(default=False, description="Только для «<» и «<=»: считать подходящими и марки, "
                                   "у которых элемент в составе не указан.")


def parse_conditions(raw: list[dict] | None) -> list[Condition]:
    """Проверяет условия; первое же нарушение — ConditionError с указанием номера условия."""
    raw = raw or []
    if len(raw) > MAX_CONDITIONS:
        raise ConditionError(f"Условий не больше {MAX_CONDITIONS}.")
    result = []
    for number, item in enumerate(raw, 1):
        try:
            condition = Condition.model_validate(item)
        except ValidationError as error:
            raise ConditionError(f"Условие {number}: нужны element и op, value — число ({error.errors()[0]['msg']}).")
        if not condition.element.strip():
            raise ConditionError(f"Условие {number}: не указан element.")
        if condition.op not in _OPS:
            raise ConditionError(f"Условие {number}: op должен быть одним из {', '.join(sorted(_OPS))}.")
        if condition.op in _NUMERIC and condition.value is None:
            raise ConditionError(f"Условие {number}: для «{condition.op}» нужен value.")
        if condition.op not in _NUMERIC and condition.value is not None:
            raise ConditionError(f"Условие {number}: для «{condition.op}» value не задаётся.")
        if condition.value is not None and not 0 <= condition.value <= 100:
            raise ConditionError(f"Условие {number}: value в процентах, от 0 до 100.")
        if condition.include_unlisted and condition.op not in ("<", "<="):
            raise ConditionError(f"Условие {number}: include_unlisted допустим только для «<» и «<=».")
        result.append(condition)
    return result


def resolve_element(text: str, symbols: set[str], russian: dict[str, str]) -> str:
    """Символ элемента: без учёта регистра среди известных, затем по русскому названию, иначе строка как есть."""
    from .graph_store import normalized  # graph_store сам импортирует этот модуль
    text = text.strip()
    by_case = {symbol.lower(): symbol for symbol in symbols}
    if text.lower() in by_case:
        return by_case[text.lower()]
    return russian.get(normalized(text), text)


def _passes(entry: dict | None, condition: Condition) -> bool:
    if condition.op == "present":
        return entry is not None
    if condition.op == "absent":
        return entry is None
    if entry is None:
        return condition.include_unlisted and condition.op in ("<", "<=")
    low, high, value = entry["low"], entry["high"], condition.value
    if low is None or high is None:
        return False  # диапазон неизвестен — гарантировать нечего
    return {">": low > value, ">=": low >= value, "<": high < value, "<=": high <= value}[condition.op]


def class_names(records: list[dict]) -> list[str]:
    return sorted({name for record in records for name in record["classes"]})


def in_class(record: dict, material_class: str) -> bool:
    """Класс совпадает с любым звеном цепочки или со всей цепочкой через « / », без учёта регистра."""
    wanted = material_class.strip().lower()
    return wanted in {name.lower() for name in record["classes"]} or wanted == " / ".join(record["classes"]).lower()


def _order_key(record: dict, symbol: str, descending: bool):
    entry = record["contents"][symbol]
    if entry["low"] is None or entry["high"] is None:
        return (1, 0, 0, record["name"])  # неизвестный диапазон — в конце при любом порядке
    sign = -1 if descending else 1
    return (0, sign * entry["low"], sign * entry["high"], record["name"])


def select_materials(records: list[dict], conditions: list[tuple[str, Condition]], sort_symbol: str | None,
                     descending: bool = True) -> list[dict]:
    """Марки, прошедшие все условия, в порядке сортировки; conditions — пары (символ, условие)."""
    found = [record for record in records
             if all(_passes(record["contents"].get(symbol), condition) for symbol, condition in conditions)
             and (sort_symbol is None or sort_symbol in record["contents"])]
    if sort_symbol is None:
        return found  # records уже по алфавиту
    return sorted(found, key=lambda record: _order_key(record, sort_symbol, descending))


def project(record: dict, symbols: list[str]) -> dict:
    """Карточка марки для ответа: только запрошенные элементы, отсутствующие помечены явно."""
    unlisted = {"form": FORM_UNLISTED, "low": None, "high": None, "text": FORM_UNLISTED}
    return {"node_id": record["node_id"], "name": record["name"], "class": " / ".join(record["classes"]),
            "base": record["base"],
            "contents": {symbol: dict(record["contents"].get(symbol, unlisted)) for symbol in symbols}}
