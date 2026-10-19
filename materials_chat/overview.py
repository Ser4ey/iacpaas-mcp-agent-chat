"""Обзор выданных данных для вкладки «Данные» (``GET /api/data/overview``).

Модуль читает локальный индекс ``data/search.sqlite3`` строго в режиме «только
чтение» и собирает понятное описание: какие ресурсы выданы, что лежит в каждом,
сколько там сущностей и как ресурсы связаны между собой.

Знания о предметной области (что считать «маркой», как подписать число, какие
вопросы предложить) лежат в ``config/data_notes.json``. Сам код о материалах
ничего не знает: для ресурса без заметок он показывает верхние уровни дерева
с числом вложенных вершин, а связи между ресурсами находит по ссылкам в данных.

Формат заметок (все поля необязательны)::

    {
      "title": "...", "intro": "...",
      "stats": [{"label": "Марок", "resource": "materials", "meta": "Материал"}],
      "resources": {
        "<id>": {
          "description": "...",
          "stats": [{"label": "...", "meta": "..." | [...], "distinct": true,
                     "source": "nodes|edges|depth|external_refs|children",
                     "root": ["метка"], "used": true,   # только для source=children
                     "resource": "<id>", "hide_zero": true}],
          "items": {
            "root": ["метка", "метка"],        # откуда начинать дерево (по умолчанию корень)
            "levels": [                         # не больше двух уровней
              {"meta": ["..."],                 # каких метапонятий брать детей (по умолчанию всех)
               "count": {"<meta>": ["марка", "марки", "марок"]} | "nodes" | "usage" | null,
               "count_label": ["...", "...", "..."],
               "detail_meta": ["обозначение"],  # дописать в скобках метку такого потомка
               "sort": "count|source|title", "max": 30}
            ]
          },
          "examples": ["..."], "related": ["<id>"]
        }
      },
      "suggested_questions": ["...", {"text": "...", "resources": ["<id>"]}]
    }

Счёт ``"usage"`` предназначен для онтологий: сколько раз понятие встречается
в базах, построенных по этой онтологии (сравниваются пары «родитель — понятие»).
"""
from __future__ import annotations

import json
import logging
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

log = logging.getLogger(__name__)

DEFAULT_TITLE = "Данные проекта"
DEFAULT_INTRO = ("Ресурсы платформы IACPaaS, выданные проекту только для чтения. "
                 "Чат отвечает по их локальной копии; цифры ниже посчитаны по ней.")
NODE_FORMS = ("вершина", "вершины", "вершин")
USAGE_FORMS = ("раз в базе", "раза в базе", "раз в базе")
DEFAULT_MAX_ITEMS = 30       # пунктов первого уровня
DEFAULT_MAX_CHILDREN = 12    # пунктов второго уровня у одного пункта
REFERENCE_FIELDS = ("original", "link")
ONTOLOGY_LANGUAGE = "язык ируо"  # метаязык, которым описаны сами онтологии


# ---------- Форматирование ----------

def format_number(value: int) -> str:
    """93593 → «93 593»: тысячи отделяются пробелом."""
    return f"{int(value):,}".replace(",", " ")


def plural(count: int, forms: Any) -> str:
    """Подпись к числу: plural(1, ["марка", "марки", "марок"]) → «марка».

    Строка возвращается как есть — для неизменяемых подписей вроде «в базе».
    """
    if isinstance(forms, str):
        return forms
    one, few, many = forms
    rest = abs(int(count)) % 100
    if 11 <= rest <= 14:
        return many
    rest %= 10
    if rest == 1:
        return one
    if 2 <= rest <= 4:
        return few
    return many


def load_notes(path: Path) -> dict:
    """Читает config/data_notes.json. Нет файла — пустые заметки и обзор по умолчанию."""
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _norm(text: Any) -> str:
    return " ".join(str(text or "").replace("ё", "е").replace("Ё", "Е").split()).casefold()


def _resource_name(reference: str) -> str:
    """«Владелец / Мой Фонд / Загрузки / База X$/путь;» → «База X»."""
    return reference.split("$", 1)[0].rsplit(" / ", 1)[-1].strip().rstrip(";").strip()


def _as_set(value: Any) -> set[str] | None:
    if value is None:
        return None
    return {value} if isinstance(value, str) else set(value)


def _short_date(platform_date: Any) -> str | None:
    """«28.08.2026-11:32:09.036» → «28.08.2026»."""
    return str(platform_date).split("-", 1)[0] if platform_date else None


def _local_date(iso: str | None) -> str | None:
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%d.%m.%Y") if iso else None
    except ValueError:
        return None


def _open_readonly(db_path: Path) -> sqlite3.Connection:
    # mode=ro: файл не создаётся и не меняется. isolation_level=None + BEGIN дают
    # один снимок на всё чтение, даже если синхронизация пишет новый ресурс.
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    db = sqlite3.connect(uri, uri=True, timeout=5, isolation_level=None)
    db.execute("BEGIN")
    return db


# ---------- Граф одного ресурса в памяти ----------

class _Graph:
    """Метки, метапонятия и связи «родитель → дети» одного ресурса."""

    def __init__(self, db: sqlite3.Connection, resource_id: str, root_hint: Any):
        rows = db.execute("SELECT rowid, node_id, label, meta FROM nodes WHERE resource_id=?",
                          (resource_id,)).fetchall()
        rows.sort()  # rowid идут в порядке выгрузки платформы — это «исходный» порядок
        self.order: dict[str, int] = {}
        self.label: dict[str, str] = {}
        self.meta: dict[str, str] = {}
        for position, (_, node_id, label, meta) in enumerate(rows):
            self.order[node_id] = position
            self.label[node_id] = label or ""
            self.meta[node_id] = meta or ""
        self.children: dict[str, list[str]] = defaultdict(list)
        has_parent: set[str] = set()
        for parent, child in db.execute("SELECT parent_id, child_id FROM edges WHERE resource_id=?", (resource_id,)):
            if parent in self.label and child in self.label:
                self.children[parent].append(child)
                has_parent.add(child)
        for kids in self.children.values():
            kids.sort(key=self.order.__getitem__)
        hint = str(root_hint) if root_hint is not None else None
        if hint in self.label:
            self.root = hint
        else:
            self.root = next((node for node in self.label if node not in has_parent), next(iter(self.label), ""))
        self._meta_counts: Counter | None = None
        self._pairs: Counter | None = None

    def descendants(self, start: str) -> Iterator[str]:
        """Все вершины под start (без неё самой); общие вершины и циклы — по одному разу."""
        seen = {start}
        stack = list(reversed(self.children.get(start, ())))
        while stack:
            node = stack.pop()
            if node in seen:
                continue
            seen.add(node)
            yield node
            stack.extend(reversed(self.children.get(node, ())))

    def find(self, labels: list[str]) -> str | None:
        current = self.root
        for label in labels:
            current = next((child for child in self.children.get(current, ()) if self.label[child] == label), None)
            if current is None:
                return None
        return current

    def depth(self) -> int:
        level, frontier, seen = 0, [self.root], {self.root}
        while True:
            frontier = [child for node in frontier for child in self.children.get(node, ()) if child not in seen]
            if not frontier:
                return level
            seen.update(frontier)
            level += 1

    @property
    def meta_counts(self) -> Counter:
        if self._meta_counts is None:
            self._meta_counts = Counter(self.meta.values())
        return self._meta_counts

    @property
    def pairs(self) -> Counter:
        """Сколько раз метапонятие child встречается прямо под метапонятием parent."""
        if self._pairs is None:
            self._pairs = Counter((self.meta[parent], self.meta[child])
                                  for parent, kids in self.children.items() for child in kids)
        return self._pairs


# ---------- Сборка обзора ----------

class _Builder:
    def __init__(self, db: sqlite3.Connection, definitions: list[dict], rows: dict[str, dict], notes: dict):
        self.db = db
        self.definitions = definitions
        self.rows = rows
        self.notes = notes
        self.configured = {item["id"] for item in definitions}
        self._graphs: dict[str, _Graph] = {}
        self._references: dict[str, Counter] = {}
        self.ids_by_name: dict[str, str] = {}
        for item in definitions:
            names = [item.get("title"), str(item.get("path", "")).rsplit(" / ", 1)[-1],
                     rows.get(item["id"], {}).get("metadata", {}).get("title")]
            for name in names:
                if name:
                    self.ids_by_name.setdefault(_norm(name), item["id"])

    # --- данные ---

    def graph(self, resource_id: str) -> _Graph | None:
        if resource_id not in self.rows:
            return None
        if resource_id not in self._graphs:
            self._graphs[resource_id] = _Graph(self.db, resource_id, self.rows[resource_id]["metadata"].get("id"))
        return self._graphs[resource_id]

    def references(self, resource_id: str) -> Counter:
        """Имена ресурсов, на которые ссылаются вершины (поля original/link) → число ссылок."""
        if resource_id not in self._references:
            found: Counter = Counter()
            query = ("SELECT payload FROM nodes WHERE resource_id=? AND "
                     "(instr(payload, '\"original\"') > 0 OR instr(payload, '\"link\"') > 0)")
            for (payload,) in self.db.execute(query, (resource_id,)):
                try:
                    data = json.loads(payload)
                except (TypeError, ValueError):
                    continue
                for field in REFERENCE_FIELDS:
                    if isinstance(data.get(field), str) and data[field].strip():
                        found[_resource_name(data[field])] += 1
            self._references[resource_id] = found
        return self._references[resource_id]

    def resolve(self, name: str | None) -> str | None:
        return self.ids_by_name.get(_norm(name)) if name else None

    def ontology_name(self, resource_id: str) -> str | None:
        reference = self.rows.get(resource_id, {}).get("metadata", {}).get("ontology")
        return _resource_name(reference) if isinstance(reference, str) and reference.strip() else None

    def described_by(self, ontology_id: str) -> list[str]:
        """Проиндексированные базы, построенные по этой онтологии."""
        return [item["id"] for item in self.definitions
                if item["id"] != ontology_id and item["id"] in self.rows
                and self.resolve(self.ontology_name(item["id"])) == ontology_id]

    def kind(self, definition: dict) -> str:
        if definition.get("kind"):
            return "ontology" if definition["kind"] == "ontology" else "data"
        return "ontology" if _norm(self.ontology_name(definition["id"])) == ONTOLOGY_LANGUAGE else "data"

    def note(self, resource_id: str) -> dict:
        return (self.notes.get("resources") or {}).get(resource_id) or {}

    def title(self, definition: dict) -> str:
        metadata = self.rows.get(definition["id"], {}).get("metadata", {})
        return definition.get("title") or metadata.get("title") or definition["id"]

    def auto_related(self, resource_id: str, kind: str) -> list[str]:
        related = set()
        if kind == "ontology":
            related.update(self.described_by(resource_id))
        else:
            related.add(self.resolve(self.ontology_name(resource_id)))
        related.update(self.resolve(name) for name in self.references(resource_id))
        for other in self.rows:  # обратные ссылки: кто ссылается на этот ресурс
            if other != resource_id and other in self.configured and any(
                    self.resolve(name) == resource_id for name in self.references(other)):
                related.add(other)
        return [item["id"] for item in self.definitions if item["id"] in related and item["id"] != resource_id]

    def external_references(self, resource_id: str) -> list[str]:
        """Ресурсы вне выданного набора, на которые ссылается этот ресурс."""
        return sorted(name for name in self.references(resource_id) if name and self.resolve(name) is None)

    # --- числа ---

    def stat(self, spec: dict, resource_id: str | None) -> dict | None:
        resource_id = spec.get("resource", resource_id)
        row = self.rows.get(resource_id)
        if row is None or not spec.get("label"):
            return None
        source = spec.get("source")
        if source == "nodes":
            value = row["node_count"] or 0
        elif source == "edges":
            value = row["edge_count"] or 0
        elif source == "depth":
            value = self.graph(resource_id).depth()
        elif source == "external_refs":
            value = len(self.external_references(resource_id))
        elif source == "children":
            # Дети вершины root; с used — только те, что встречаются в базах по этой онтологии.
            graph = self.graph(resource_id)
            node = graph.find(list(spec.get("root") or []))
            if node is None:
                return None
            kids = graph.children.get(node, [])
            if spec.get("used"):
                kids = [kid for kid in kids if self.usage(resource_id, graph.label[node], graph.label[kid])]
            value = len(kids)
        elif "meta" in spec or spec.get("distinct"):
            graph = self.graph(resource_id)
            metas = _as_set(spec.get("meta"))
            if spec.get("distinct"):
                value = len({graph.label[node] for node in graph.label if node != graph.root
                             and (metas is None or graph.meta[node] in metas)})
            else:
                value = sum(graph.meta_counts[meta] for meta in metas or ())
        else:
            return None
        if spec.get("hide_zero") and not value:
            return None
        return {"label": spec["label"], "value": format_number(value)}

    def usage(self, ontology_id: str, parent_label: str, label: str) -> int | None:
        described = self.described_by(ontology_id)
        if not described:
            return None
        return sum(self.graph(item).pairs[(parent_label, label)] for item in described)

    def count(self, resource_id: str, graph: _Graph, parent: str, node: str, level: dict, default_rule: Any):
        rule = level.get("count", default_rule)
        if not rule:
            return None, None
        if rule == "nodes":
            value, forms = sum(1 for _ in graph.descendants(node)), NODE_FORMS
        elif rule == "usage":
            value, forms = self.usage(resource_id, graph.label[parent], graph.label[node]), USAGE_FORMS
            if value is None:
                return None, None
        elif isinstance(rule, dict):
            found = Counter(graph.meta[child] for child in graph.descendants(node) if graph.meta[child] in rule)
            value = sum(found.values())
            if value == 0 and graph.meta[node] in rule:
                return None, None  # сама сущность (марка, свойство) не «содержит» себя
            forms = rule[max(rule, key=lambda meta: found.get(meta, 0))]
        else:
            return None, None
        return value, plural(value, level.get("count_label") or forms)

    # --- дерево пунктов ---

    def comments(self, resource_id: str, nodes: list[str]) -> dict[str, str]:
        result = {}
        for start in range(0, len(nodes), 400):
            chunk = nodes[start:start + 400]
            marks = ",".join("?" for _ in chunk)
            for node_id, payload in self.db.execute(
                    f"SELECT node_id, payload FROM nodes WHERE resource_id=? AND node_id IN ({marks})",
                    (resource_id, *chunk)):
                try:
                    comment = json.loads(payload).get("comment")
                except (TypeError, ValueError, AttributeError):
                    comment = None
                if isinstance(comment, str) and comment.strip():
                    result[node_id] = comment.strip().splitlines()[0].strip()
        return result

    def level(self, resource_id: str, graph: _Graph, parent: str, spec: dict, default_rule: Any, default_max: int):
        """Пункты одного уровня: (вершина, словарь пункта). Последний может быть «…и ещё N»."""
        metas = _as_set(spec.get("meta"))
        nodes = [node for node in graph.children.get(parent, ()) if metas is None or graph.meta[node] in metas]
        entries = []
        for node in nodes:
            value, label = self.count(resource_id, graph, parent, node, spec, default_rule)
            entries.append((node, value, label))
        order = spec.get("sort", "count")
        if order == "title":
            entries.sort(key=lambda entry: (_norm(graph.label[entry[0]]), graph.order[entry[0]]))
        elif order == "count":
            entries.sort(key=lambda entry: (-(entry[1] or 0), graph.order[entry[0]]))
        limit = max(1, int(spec.get("max") or default_max))
        hidden = len(entries) - limit
        if hidden > 0:
            entries = entries[:limit]
        details = _as_set(spec.get("detail_meta")) or set()
        titles = {}
        for node, _, _ in entries:
            title = graph.label[node] or "(без названия)"
            detail = next((graph.label[child] for child in graph.children.get(node, ()) if graph.meta[child] in details), None)
            titles[node] = f"{title} ({detail})" if detail else title
        same = Counter(titles.values())
        repeated = [node for node, _, _ in entries if same[titles[node]] > 1]
        if repeated:  # одинаковые соседи (например, три «Характеристики») различаем по комментарию
            for node, comment in self.comments(resource_id, repeated).items():
                titles[node] = f"{titles[node]} — {comment[:80]}"
        result = []
        for node, value, label in entries:
            item = {"title": titles[node]}
            if value is not None:
                item.update(count=value, count_label=label)
            result.append((node, item))
        if hidden > 0:
            result.append((None, {"title": f"…и ещё {format_number(hidden)}"}))
        return result

    def items(self, resource_id: str, graph: _Graph, kind: str, spec: dict) -> list[dict]:
        start = graph.find(list(spec.get("root") or []))
        if start is None:
            log.warning("Обзор данных: в ресурсе %s нет вершины %s, беру корень", resource_id, spec.get("root"))
            start = graph.root
        levels = spec.get("levels")
        if levels is None:
            levels = [{}, {}]
        levels = [level or {} for level in levels[:2]]
        if not levels:
            return []
        default_rule = "usage" if kind == "ontology" and self.described_by(resource_id) else "nodes"
        result = []
        for node, item in self.level(resource_id, graph, start, levels[0], default_rule, DEFAULT_MAX_ITEMS):
            if node is not None and len(levels) > 1:
                children = [child for _, child in self.level(resource_id, graph, node, levels[1],
                                                             default_rule, DEFAULT_MAX_CHILDREN)]
                if children:
                    item["children"] = children
            result.append(item)
        return result

    def examples(self, resource_id: str, graph: _Graph, wanted: list[str] | None, items: list[dict]) -> list[str]:
        if wanted is not None:
            # В обзор попадают только метки, которые действительно есть в данных.
            present = set(graph.label.values())
            return [label for label in dict.fromkeys(wanted) if label in present]
        result = []
        for item in items:
            for entry in item.get("children") or [item]:
                if not entry["title"].startswith("…") and entry["title"] not in result:
                    result.append(entry["title"])
        return result[:5]

    # --- разделы ---

    def default_description(self, resource_id: str, kind: str) -> str:
        metadata = self.rows[resource_id]["metadata"]
        parts = []
        if metadata.get("comment"):
            parts.append(str(metadata["comment"]).strip().rstrip(".") + ".")
        if kind == "ontology":
            described = self.described_by(resource_id)
            if described:
                titles = ", ".join(f"«{self.title(self.by_id(item))}»" for item in described)
                parts.append(f"По этой онтологии построены: {titles}.")
        else:
            name = self.ontology_name(resource_id)
            if name:
                missing = "" if self.resolve(name) in self.configured else " — её нет среди выданных ресурсов"
                parts.append(f"Описан по онтологии «{name}»{missing}.")
        parts.append("Подробного описания пока нет; ниже — верхние уровни дерева ресурса.")
        return " ".join(parts)

    def by_id(self, resource_id: str) -> dict:
        return next(item for item in self.definitions if item["id"] == resource_id)

    def section(self, definition: dict) -> dict:
        resource_id = definition["id"]
        note = self.note(resource_id)
        kind = self.kind(definition)
        row = self.rows.get(resource_id)
        if row is None:
            return {"id": resource_id, "title": self.title(definition), "kind": kind,
                    "description": note.get("description") or "Ресурс ещё не загружен в локальный индекс.",
                    "stats": [{"label": "Состояние", "value": "не загружен"}], "items": [], "examples": [],
                    "related": [item for item in note.get("related", []) if item in self.configured],
                    "source": {"path": definition.get("path"), "indexed_at": None, "platform_modified": None}}
        graph = self.graph(resource_id)
        items = self.items(resource_id, graph, kind, note.get("items") or {})
        specs = list(note.get("stats") or [])
        if not any(spec.get("source") == "nodes" for spec in specs):
            specs.append({"label": "Вершин", "source": "nodes"})
        if "stats" not in note:
            specs.append({"label": "Ссылки на ресурсы вне набора", "source": "external_refs", "hide_zero": True})
        stats = [stat for stat in (self.stat(spec, resource_id) for spec in specs) if stat]
        if row.get("last_error"):
            stats.append({"label": "Обновление", "value": "не удалось, показан прежний снимок"})
        related = note.get("related")
        related = ([item for item in related if item in self.configured and item != resource_id]
                   if related is not None else self.auto_related(resource_id, kind))
        return {"id": resource_id, "title": self.title(definition), "kind": kind,
                "description": note.get("description") or self.default_description(resource_id, kind),
                "stats": stats, "items": items,
                "examples": self.examples(resource_id, graph, note.get("examples"), items),
                "related": related,
                "source": {"path": definition.get("path") or row["metadata"].get("path"),
                           "indexed_at": row["indexed_at"],
                           "platform_modified": _short_date(row["metadata"].get("date"))}}

    def overview_stats(self) -> list[dict]:
        indexed = [item["id"] for item in self.definitions if item["id"] in self.rows]
        total = len(self.definitions)
        stats = [{"label": "Ресурсов", "value": format_number(total) if len(indexed) == total
                  else f"{format_number(len(indexed))} из {format_number(total)}"}]
        for spec in self.notes.get("stats") or []:
            stat = self.stat(spec, None)
            if stat:
                stats.append(stat)
        stats.append({"label": "Вершин", "value": format_number(sum(self.rows[item]["node_count"] or 0 for item in indexed))})
        snapshot = _local_date(min(self.rows[item]["indexed_at"] for item in indexed))
        if snapshot:
            stats.append({"label": "Снимок данных", "value": snapshot})
        return stats

    def questions(self) -> list[str]:
        result = []
        for entry in self.notes.get("suggested_questions") or []:
            if isinstance(entry, str):
                result.append(entry)
            elif isinstance(entry, dict) and entry.get("text") and all(
                    item in self.rows for item in entry.get("resources") or []):
                result.append(entry["text"])
        return result


def _indexed_rows(db: sqlite3.Connection, ids: list[str]) -> dict[str, dict]:
    marks = ",".join("?" for _ in ids)
    rows = {}
    for resource_id, metadata, indexed_at, node_count, edge_count, last_error in db.execute(
            f"SELECT resource_id, metadata, indexed_at, node_count, edge_count, last_error "
            f"FROM resources WHERE resource_id IN ({marks})", ids):
        try:
            parsed = json.loads(metadata) if metadata else {}
        except ValueError:
            parsed = {}
        rows[resource_id] = {"metadata": parsed if isinstance(parsed, dict) else {}, "indexed_at": indexed_at,
                             "node_count": node_count, "edge_count": edge_count, "last_error": last_error}
    return rows


def build_overview(db_path: Path, resources: list[dict], notes: dict | None = None) -> dict:
    """Описание данных для вкладки «Данные»: формат — раздел GET /api/data/overview в docs/http-api.md.

    db_path — локальный индекс (открывается только на чтение и закрывается);
    resources — список из config/resources.json; notes — config/data_notes.json.
    Нет файла, таблиц или ни одного загруженного ресурса → ``ready: false`` и пустые разделы.
    """
    notes = notes or {}
    definitions = [dict(item) for item in resources if item.get("id")]
    result = {"ready": False, "generated_at": _now(), "title": notes.get("title") or DEFAULT_TITLE,
              "intro": notes.get("intro") or DEFAULT_INTRO, "stats": [], "sections": [],
              "suggested_questions": []}
    if not definitions or not Path(db_path).is_file():
        return result
    try:
        with closing(_open_readonly(db_path)) as db:
            rows = _indexed_rows(db, [item["id"] for item in definitions])
            if not rows:
                return result
            builder = _Builder(db, definitions, rows, notes)
            sections = [builder.section(item) for item in definitions]
            result.update(ready=len(rows) == len(definitions), stats=builder.overview_stats(),
                          sections=sections, suggested_questions=builder.questions())
    except sqlite3.Error:
        # Пустой или повреждённый файл: интерфейс покажет предложение загрузить данные.
        log.warning("Обзор данных: индекс %s не прочитан", db_path, exc_info=True)
        return {**result, "stats": [], "sections": [], "suggested_questions": []}
    return result
