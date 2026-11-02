"""Онтологии проекта как схема записи: проверка дерева «понятие + имя или значение» и сборка universal JSON.

Онтологии лежат в локальном индексе целиком. Правила взяты из языка ИРУО и проверены на платформе:
- понятие-ссылка без своих потомков берёт потомков понятия, на которое ссылается (по link_id);
- отношение со спецификатором «пропуск» прозрачно: его потомки стоят прямо под родителем;
- ограничители ref, clone, ref-clone требуют ссылки на вершину или клона вершины другого ресурса —
  такие вершины сервер не создаёт (платформа отклоняет собственное значение по ним).
"""
import json
from dataclasses import dataclass

from .fund_paths import segment_key

ONTOLOGIES = ("materials_ontology", "properties_ontology", "characteristics_ontology")
VALUE_TYPES = ("REAL", "INTEGER", "STRING")
REFERENCE_ONLY = ("ref", "clone", "ref-clone")
NAMED_SPECIFIERS = ("множеств", "спис", "последовательн")  # элементы множеств и списков различаются именем
MAX_ITEMS = 500


class OntologyError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


@dataclass(frozen=True)
class Concept:
    resource_id: str
    node_id: str
    name: str
    type: str
    specifier: str
    restrictor: str
    valtype: str
    alternative: bool
    link_id: str

    @property
    def needs_name(self) -> bool:
        return self.type == "НЕТЕРМИНАЛ" and any(part in self.specifier for part in NAMED_SPECIFIERS)


class Ontology:
    def __init__(self, resource_id: str, title: str, path: str, root: Concept,
                 concepts: dict[tuple[str, str], Concept], edges: dict[tuple[str, str], list[str]]):
        self.resource_id, self.title, self.path = resource_id, title, path
        self.root, self._concepts, self._edges = root, concepts, edges

    @classmethod
    def load(cls, store, definitions, resource_id: str) -> "Ontology":
        if resource_id not in ONTOLOGIES:
            raise OntologyError("INVALID_ARGUMENTS", "Онтология: " + ", ".join(ONTOLOGIES) + ".")
        concepts, edges, roots = {}, {}, {}
        with store.connect() as db:
            for row in db.execute(f"SELECT resource_id, node_id, label, payload FROM nodes WHERE resource_id IN "
                                  f"({','.join('?' for _ in ONTOLOGIES)})", ONTOLOGIES):
                payload = json.loads(row["payload"] or "{}")
                concept = Concept(row["resource_id"], row["node_id"], row["label"], payload.get("type", ""),
                                  payload.get("specifier") or "", payload.get("restrictor") or "",
                                  payload.get("valtype") or "", payload.get("meta") == "описать вариант альтернативы",
                                  str(payload.get("link_id") or ""))
                concepts[(concept.resource_id, concept.node_id)] = concept
                if concept.type == "КОРЕНЬ":
                    roots[concept.resource_id] = concept
            for row in db.execute(f"SELECT resource_id, parent_id, child_id FROM edges WHERE resource_id IN "
                                  f"({','.join('?' for _ in ONTOLOGIES)}) ORDER BY parent_id, CAST(child_id AS INTEGER)", ONTOLOGIES):
                edges.setdefault((row["resource_id"], row["parent_id"]), []).append(row["child_id"])
        indexed = store.resource(resource_id)
        if indexed is None or resource_id not in roots:
            raise OntologyError("INDEX_NOT_READY", "Онтология ещё не загружена в индекс. Обновите индекс из IACPaaS.")
        title = next((item.title for item in definitions if item.id == resource_id), indexed["metadata"].get("title", ""))
        return cls(resource_id, indexed["metadata"].get("title") or title, indexed["metadata"].get("path", ""),
                   roots[resource_id], concepts, edges)

    # ---------- Структура ----------

    def _target(self, concept: Concept) -> Concept | None:
        """Понятие, на которое ссылается понятие-ссылка, в одной из трёх онтологий проекта."""
        for resource_id in (concept.resource_id, *ONTOLOGIES):
            target = self._concepts.get((resource_id, concept.link_id))
            if target and self._edges.get((resource_id, target.node_id)):
                return target
        return None

    def _children(self, concept: Concept, seen: frozenset = frozenset()) -> list[Concept]:
        key = (concept.resource_id, concept.node_id)
        if key in seen:
            return []
        source = concept
        if not self._edges.get(key) and concept.link_id:
            source = self._target(concept) or concept
        result = []
        for child_id in self._edges.get((source.resource_id, source.node_id), []):
            child = self._concepts.get((source.resource_id, child_id))
            if child is None:
                continue
            if child.specifier == "пропуск":
                result.extend(self._children(child, seen | {key}))
            else:
                result.append(child)
        return result

    def options(self, group: list[Concept]) -> dict[str, list[Concept]]:
        """Допустимые понятия под вершиной: по имени (у одноимённых понятий потомки объединяются)."""
        result: dict[str, list[Concept]] = {}
        for concept in group:
            for child in self._children(concept):
                result.setdefault(segment_key(child.name), []).append(child)
        return result

    def unsupported(self, concept: Concept) -> str | None:
        if concept.restrictor in REFERENCE_ONLY:
            return "нужна ссылка на вершину или клон вершины другого ресурса"
        if concept.type == "ТЕРМИНАЛ-ЗНАЧЕНИЕ":
            return "фиксированное значение онтологии"
        if concept.type == "ТЕРМИНАЛ-СОРТ" and concept.valtype not in VALUE_TYPES:
            return f"значения типа {concept.valtype or 'без типа'}"
        if concept.type == "НЕТЕРМИНАЛ" and concept.link_id and not self._edges.get(
                (concept.resource_id, concept.node_id)) and self._target(concept) is None:
            return "понятие из онтологии вне трёх онтологий проекта"
        return None

    @staticmethod
    def kind(concept: Concept) -> str:
        if concept.type == "ТЕРМИНАЛ-СОРТ":
            kind = f"значение {concept.valtype}"
        elif concept.type == "ТЕРМИНАЛ-ЗНАЧЕНИЕ":
            kind = "фиксированное значение"
        else:
            kind = "вершина, нужно имя" if concept.needs_name else "вершина"
        return ("вариант альтернативы, " if concept.alternative else "") + kind

    def structure(self, max_depth: int = 10) -> list[str]:
        """Дерево понятий строками с отступом: «понятие [вид]»; неподдерживаемые не раскрываются."""
        lines, expanded = [], set()

        def walk(group: list[Concept], depth: int):
            for children in self.options(group).values():
                concept = children[0]
                reason = self.unsupported(concept)
                key = tuple(sorted((item.resource_id, item.node_id) for item in children))
                again = not reason and key in expanded and bool(self.options(children))
                lines.append("  " * depth + f"{concept.name} [{self.kind(concept)}"
                             + (f"; не поддерживается: {reason}]" if reason else "]")
                             + (" — состав как выше" if again else ""))
                if not reason and not again and depth + 1 < max_depth:
                    expanded.add(key)
                    walk(children, depth + 1)
        walk([self.root], 0)
        return lines

    # ---------- Сборка ресурса ----------

    def build(self, name: str, comment: str, items: list[dict]) -> dict:
        """universal JSON нового ресурса; при нарушении онтологии — OntologyError с подсказкой."""
        name = (name or "").strip()
        if not name:
            raise OntologyError("INVALID_ARGUMENTS", "Укажите имя ресурса (name).")
        counter = [0]
        graph = {"title": name, "name": name, "json_type": "universal", "type": "КОРЕНЬ",
                 "ontology": self.path, "meta": self.title,
                 "successors": self._nodes(items or [], [self.root], "корень ресурса", counter)}
        if comment:
            graph["comment"] = comment
        return graph

    def _nodes(self, items, group: list[Concept], parent_label: str, counter: list[int]) -> list[dict]:
        if not isinstance(items, list):
            raise OntologyError("INVALID_ARGUMENTS", f"children у «{parent_label}» должен быть списком.")
        allowed = self.options(group)
        result = []
        for item in items:
            counter[0] += 1
            if counter[0] > MAX_ITEMS:
                raise OntologyError("INVALID_ARGUMENTS", f"Не больше {MAX_ITEMS} вершин в одном ресурсе.")
            if not isinstance(item, dict) or not str(item.get("concept") or "").strip():
                raise OntologyError("INVALID_ARGUMENTS", f"У каждой вершины под «{parent_label}» нужно поле concept.")
            concept_name = str(item["concept"]).strip()
            candidates = allowed.get(segment_key(concept_name))
            if not candidates:
                names = ", ".join(f"«{children[0].name}»" for children in allowed.values())
                raise OntologyError("CONCEPT_NOT_ALLOWED", f"В «{parent_label}» нельзя добавить «{concept_name}»; "
                                    + (f"допустимы: {names}." if names else "вложенных понятий у него нет."))
            result.append(self._node(item, candidates, counter))
        return result

    def _node(self, item: dict, candidates: list[Concept], counter: list[int]) -> dict:
        concept = candidates[0]
        reason = self.unsupported(concept)
        if reason:
            raise OntologyError("UNSUPPORTED_ONTOLOGY_FEATURE", f"«{concept.name}» не поддерживается: {reason}.")
        comment = str(item.get("comment") or "").strip()
        name = str(item.get("name") or "").strip()
        if concept.type == "ТЕРМИНАЛ-СОРТ":
            raw = item.get("value", name or None)
            if item.get("children"):
                raise OntologyError("INVALID_ARGUMENTS", f"У значения «{concept.name}» не бывает вложенных вершин.")
            node = {"type": "ТЕРМИНАЛ-ЗНАЧЕНИЕ", "value": self.value(concept, raw),
                    "valtype": concept.valtype, "meta": concept.name}
        else:
            if item.get("value") is not None:
                raise OntologyError("INVALID_ARGUMENTS", f"«{concept.name}» — вершина: укажите name, а не value.")
            if not name and concept.needs_name:
                raise OntologyError("INVALID_ARGUMENTS", f"У «{concept.name}» нужно имя (name): это элемент множества.")
            if name and concept.specifier == "копия" and segment_key(name) != segment_key(concept.name):
                raise OntologyError("INVALID_ARGUMENTS",
                                    f"Вершина «{concept.name}» называется как понятие; name не указывайте.")
            node = {"type": "НЕТЕРМИНАЛ", "name": name or concept.name, "meta": concept.name}
            children = self._nodes(item.get("children") or [], candidates, concept.name, counter)
            if children:
                node["successors"] = children
        if comment:
            node["comment"] = comment
        return node

    @staticmethod
    def value(concept: Concept, raw):
        """Значение по типу понятия: REAL — число (строка-число допустима), INTEGER — целое, STRING — строка."""
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            raise OntologyError("INVALID_VALUE", f"У «{concept.name}» нужно значение (value, {concept.valtype}).")
        if concept.valtype == "STRING":
            if not isinstance(raw, str):
                raise OntologyError("INVALID_VALUE", f"«{concept.name}»: ожидается строка (STRING).")
            return raw.strip()
        try:
            if isinstance(raw, bool):
                raise ValueError
            number = float(raw.strip().replace(",", ".")) if isinstance(raw, str) else float(raw)
        except (TypeError, ValueError):
            raise OntologyError("INVALID_VALUE", f"«{concept.name}»: ожидается число ({concept.valtype}), "
                                f"получено «{raw}».") from None
        if concept.valtype == "INTEGER":
            if not number.is_integer():
                raise OntologyError("INVALID_VALUE", f"«{concept.name}»: ожидается целое число (INTEGER).")
            return int(number)
        return number


def node_label(node: dict) -> str:
    """«Понятие «имя»» вершины universal JSON; имя, совпадающее с понятием, не повторяется."""
    meta = str(node.get("meta", ""))
    text = str(node["value"]) if "value" in node else str(node.get("name", ""))
    return meta if not text or text == meta else f"{meta} «{text}»"


def describe_graph(graph: dict, limit: int = 1200) -> str:
    """Все цепочки от корня до листьев: «Класс «А» → Свойство «Б» → синоним «В»; …»."""
    chains = []

    def walk(node: dict, prefix: list[str]):
        path = prefix + [node_label(node)]
        children = node.get("successors") or []
        if not children:
            chains.append(" → ".join(path))
        for child in children:
            walk(child, path)
    for top in graph.get("successors") or []:
        walk(top, [])
    text = "; ".join(chains) or "без вершин"
    return text if len(text) <= limit else text[:limit].rsplit(";", 1)[0] + f"; … (всего цепочек: {len(chains)})"
