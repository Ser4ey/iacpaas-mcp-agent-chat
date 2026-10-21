"""Локальный граф и FTS5-поиск. В модель попадают только ограниченные выборки."""
import json
import re
import sqlite3
from collections import deque
from contextlib import contextmanager
from pathlib import Path

from .platform import utc_now


def normalized(text: str) -> str:
    return str(text).lower().replace("ё", "е")


def search_terms(text: str) -> list[str]:
    # Простая нормализация окончаний помогает «стали» найти «сталь».
    # Это лексический поиск; семантические совпадения или полноту он не обещает.
    endings = ("иями", "ами", "ями", "ого", "ему", "ому", "ыми", "ими", "ая", "яя", "ое", "ее", "ые", "ие",
               "ой", "ей", "ый", "ий", "ую", "юю", "ов", "ев", "ам", "ям", "ах", "ях", "ия", "ию",
               "и", "а", "я", "ы", "у", "ю", "е", "о", "ь", "й")
    result = []
    for word in re.findall(r"[^\W_]+", normalized(text), re.UNICODE):
        if len(word) >= 4 and re.fullmatch(r"[а-я]+", word):
            for suffix in endings:
                if word.endswith(suffix) and len(word) - len(suffix) >= 3:
                    word = word[:-len(suffix)]
                    break
        result.append(word)
    return result


def node_from_raw(resource_id: str, raw: dict, path: str) -> dict:
    """Вершина universal JSON в том же виде, в каком её отдаёт индекс."""
    return {"resource_id": resource_id, "node_id": str(raw.get("id")),
            "label": str(raw.get("name", raw.get("value", ""))), "meta": str(raw.get("meta", "")),
            "path": path, "type": raw.get("type"), "value": raw.get("value"),
            "comment": raw.get("comment", ""),
            "links": [raw[key] for key in ("link", "original") if raw.get(key)]}


class GraphStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS resources (
                    resource_id TEXT PRIMARY KEY, metadata TEXT NOT NULL,
                    indexed_at TEXT NOT NULL, node_count INTEGER, edge_count INTEGER,
                    last_error TEXT
                );
                CREATE TABLE IF NOT EXISTS nodes (
                    resource_id TEXT NOT NULL, node_id TEXT NOT NULL,
                    label TEXT, meta TEXT, path TEXT, payload TEXT,
                    PRIMARY KEY(resource_id,node_id)
                );
                CREATE TABLE IF NOT EXISTS edges (
                    resource_id TEXT NOT NULL, parent_id TEXT NOT NULL, child_id TEXT NOT NULL,
                    PRIMARY KEY(resource_id,parent_id,child_id)
                );
                CREATE INDEX IF NOT EXISTS edge_parent ON edges(resource_id,parent_id);
                -- Обратный индекс нужен, чтобы быстро подниматься от вершины к корню.
                CREATE INDEX IF NOT EXISTS edge_child ON edges(resource_id,child_id);
                CREATE VIRTUAL TABLE IF NOT EXISTS search_index USING fts5(
                    resource_id UNINDEXED, node_id UNINDEXED, label, body,
                    tokenize='unicode61 remove_diacritics 2'
                );
            """)

    @contextmanager
    def connect(self):
        """Соединение на одну операцию: фиксирует транзакцию и всегда закрывается."""
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def resource(self, resource_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM resources WHERE resource_id=?", (resource_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["metadata"] = json.loads(result["metadata"])
        return result

    def import_graph(self, resource_id: str, graph: dict) -> dict:
        """Атомарная замена одного ресурса: читатели не видят половину индекса."""
        nodes, edges, fts = {}, set(), []
        stack = [(graph, None, [], 0)]
        while stack:
            raw, parent, ancestors, ordinal = stack.pop()
            label = str(raw.get("name", raw.get("value", "")))
            node_id = str(raw.get("id") or "path:" + "/".join(ancestors + [label]) + ":" + str(ordinal))
            if parent:
                edges.add((resource_id, parent, node_id))
            # При повторном появлении вершины оставляем полное первичное описание.
            if node_id in nodes:
                continue
            path = "/".join(ancestors + [label])
            payload = {key: value for key, value in raw.items() if key not in ("successors", "context", "ontology")}
            nodes[node_id] = (resource_id, node_id, label, str(raw.get("meta", "")), path,
                              json.dumps(payload, ensure_ascii=False))
            if not raw.get("link"):
                body = " ".join([str(raw.get("meta", "")), str(raw.get("comment", "")), " ".join(ancestors[-3:])])
                fts.append((resource_id, node_id, " ".join(search_terms(label)), " ".join(search_terms(body))))
            for index, child in reversed(list(enumerate(raw.get("successors", [])))):
                if isinstance(child, dict):
                    stack.append((child, node_id, ancestors + [label], index))
        metadata = {key: graph[key] for key in ("title", "path", "date", "creation", "ontology", "context",
                                                "json_type", "id", "comment") if key in graph}
        indexed_at = utc_now()
        with self.connect() as db:
            for table in ("nodes", "edges", "search_index"):
                db.execute(f"DELETE FROM {table} WHERE resource_id=?", (resource_id,))
            db.executemany("INSERT INTO nodes VALUES (?,?,?,?,?,?)", nodes.values())
            db.executemany("INSERT OR IGNORE INTO edges VALUES (?,?,?)", edges)
            db.executemany("INSERT INTO search_index VALUES (?,?,?,?)", fts)
            db.execute("INSERT OR REPLACE INTO resources VALUES (?,?,?,?,?,NULL)",
                       (resource_id, json.dumps(metadata, ensure_ascii=False), indexed_at, len(nodes), len(edges)))
        return {"resource_id": resource_id, "nodes": len(nodes), "edges": len(edges), "indexed_at": indexed_at}

    def record_error(self, resource_id: str, message: str):
        with self.connect() as db:
            db.execute("UPDATE resources SET last_error=? WHERE resource_id=?", (message, resource_id))

    def search(self, query: str, resource_ids: list[str], limit: int, offset: int) -> dict:
        terms = list(dict.fromkeys(search_terms(query)))[:8]
        if not terms:
            return {"hits": [], "total": 0, "match_mode": "empty_query"}
        slots = ",".join("?" for _ in resource_ids)
        with self.connect() as db:
            def total_for(expression):
                return db.execute(f"SELECT count(*) FROM search_index WHERE search_index MATCH ? "
                                  f"AND resource_id IN ({slots})", [expression, *resource_ids]).fetchone()[0]
            # Сначала ищем все слова сразу; если так ничего нет — любое из слов.
            mode = "all_terms_prefix"
            expression = " AND ".join('"' + term + '"*' for term in terms)
            total = total_for(expression)
            if total == 0 and len(terms) > 1:
                mode = "any_term_prefix"
                expression = " OR ".join('"' + term + '"*' for term in terms)
                total = total_for(expression)
            rows = db.execute(f"""SELECT n.*, bm25(search_index,0,0,8,1) AS rank
                FROM search_index JOIN nodes n ON n.resource_id=search_index.resource_id AND n.node_id=search_index.node_id
                WHERE search_index MATCH ? AND search_index.resource_id IN ({slots})
                ORDER BY rank, n.node_id LIMIT ? OFFSET ?""", [expression, *resource_ids, limit, offset]).fetchall()
        return {"hits": [self.node_dict(row) for row in rows], "total": total, "match_mode": mode}

    @staticmethod
    def node_dict(row) -> dict:
        payload = json.loads(row["payload"])
        return {"resource_id": row["resource_id"], "node_id": row["node_id"], "label": row["label"],
                "meta": row["meta"], "path": row["path"], "type": payload.get("type"),
                "value": payload.get("value"), "comment": payload.get("comment", ""),
                "links": [payload[key] for key in ("link", "original") if payload.get(key)]}

    def node(self, resource_id: str, node_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM nodes WHERE resource_id=? AND node_id=?", (resource_id, node_id)).fetchone()
        return self.node_dict(row) if row else None

    def concept_labels(self, resource_id: str, node_id: str) -> list[str] | None:
        """Имена вершин от корня (без него) до node_id — так IACPaaS адресует вершину.

        None, если вершины нет.
        """
        labels = []
        with self.connect() as db:
            current = node_id
            for _ in range(200):  # защита от циклов в повреждённых данных
                row = db.execute("SELECT label, path FROM nodes WHERE resource_id=? AND node_id=?",
                                 (resource_id, current)).fetchone()
                if row is None:
                    return None
                parents = db.execute("SELECT n.node_id, n.path FROM edges e JOIN nodes n "
                                     "ON n.resource_id=e.resource_id AND n.node_id=e.parent_id "
                                     "WHERE e.resource_id=? AND e.child_id=? ORDER BY n.node_id",
                                     (resource_id, current)).fetchall()
                if not parents:  # дошли до корня; сам корень в путь не входит
                    return list(reversed(labels))
                # На вершину могут вести ссылки из других мест; идём по основному пути,
                # тому самому, что сохранён в индексе при первом появлении вершины.
                primary = next((parent for parent in parents if parent["path"] + "/" + row["label"] == row["path"]), parents[0])
                labels.append(row["label"])
                current = primary["node_id"]
        return None

    def subgraph(self, resource_id: str, root: str, depth: int, limit: int, offset: int) -> dict:
        if self.node(resource_id, root) is None:
            raise KeyError(root)
        selected, selected_edges, seen = [], [], set()
        queue = deque([(root, 0)])
        # Обход ограничен глубиной, но может посетить много вершин. Верхний предел
        # защищает локальный сервер даже при запросе от корня большого справочника.
        traversal_cap = 100000
        with self.connect() as db:
            while queue and len(seen) < traversal_cap:
                current, distance = queue.popleft()
                if current in seen:
                    continue
                seen.add(current)
                selected.append(current)
                if distance >= depth:
                    continue
                for edge in db.execute("SELECT child_id FROM edges WHERE resource_id=? AND parent_id=? ORDER BY child_id",
                                       (resource_id, current)):
                    child = edge["child_id"]
                    selected_edges.append({"from": current, "to": child})
                    if child not in seen:
                        queue.append((child, distance + 1))
            page = selected[offset:offset + limit]
            nodes = [self.node_dict(db.execute("SELECT * FROM nodes WHERE resource_id=? AND node_id=?",
                                               (resource_id, node)).fetchone()) for node in page]
        return page_of_subgraph(root, nodes, selected_edges, len(selected), offset, limit, capped=bool(queue))

    def counts(self, resource_id: str, root: str | None = None) -> dict:
        if root is None:
            item = self.resource(resource_id)
            return {"nodes": item["node_count"], "edges": item["edge_count"], "scope": "whole_indexed_resource"}
        if self.node(resource_id, root) is None:
            raise KeyError(root)
        # Все дуги ресурса читаются одним запросом, обход — в памяти. Это линейно по
        # размеру графа: ~0,2 с даже для справочника из 94 тысяч вершин.
        children: dict[str, list[str]] = {}
        with self.connect() as db:
            for parent, child in db.execute("SELECT parent_id, child_id FROM edges WHERE resource_id=?", (resource_id,)):
                children.setdefault(parent, []).append(child)
        reachable, queue, edges = {root}, deque([root]), 0
        while queue:
            for child in children.get(queue.popleft(), []):
                edges += 1  # дуги уникальны (первичный ключ), и все потомки достижимы
                if child not in reachable:
                    reachable.add(child)
                    queue.append(child)
        return {"nodes": len(reachable), "edges": edges, "scope": "reachable_subgraph", "root": root}


def page_of_subgraph(root: str, nodes: list[dict], edges: list[dict], total: int,
                     offset: int, limit: int, capped: bool = False) -> dict:
    """Общая форма страницы подграфа для индекса и живого чтения с платформы."""
    page_ids = {node["node_id"] for node in nodes}
    more = offset + limit < total
    return {"root": root, "nodes": nodes,
            "edges": [edge for edge in edges if edge["from"] in page_ids and edge["to"] in page_ids],
            "total_within_depth": total, "has_more": more, "next_offset": offset + limit if more else None,
            "truncated": capped or more, "traversal_capped": capped}


def subgraph_from_export(resource_id: str, target: dict, base_path: str, depth: int,
                         limit: int, offset: int) -> dict:
    """Страница подграфа из ответа платформы, в том же порядке обхода, что и в индексе."""
    root = str(target.get("id"))
    selected, edges, seen = [], [], set()
    queue = deque([(target, base_path, 0)])
    while queue:
        raw, path, distance = queue.popleft()
        node_id = str(raw.get("id"))
        if node_id in seen:
            continue
        seen.add(node_id)
        selected.append(node_from_raw(resource_id, raw, path))
        if distance >= depth:
            continue
        successors = [child for child in raw.get("successors", []) if isinstance(child, dict) and child.get("id") is not None]
        for child in sorted(successors, key=lambda item: str(item["id"])):
            label = str(child.get("name", child.get("value", "")))
            edges.append({"from": node_id, "to": str(child["id"])})
            if str(child["id"]) not in seen:
                queue.append((child, path + "/" + label, distance + 1))
    return page_of_subgraph(root, selected[offset:offset + limit], edges, len(selected), offset, limit)
