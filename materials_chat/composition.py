"""Состав марок: разбор записей справочника в таблицу «марка → элемент → диапазон»."""
import json
import sqlite3

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
