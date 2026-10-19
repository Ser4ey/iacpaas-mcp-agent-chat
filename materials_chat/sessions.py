"""A small SQLite history store. A complete turn is written in one transaction."""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .prompts import SYSTEM_PROMPT


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def empty_usage() -> dict:
    return {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
            "known": True, "requests": 0, "unknown_requests": 0}


def add_usage(left: dict, right: dict) -> dict:
    result = empty_usage()
    for key in ("input_tokens", "output_tokens", "total_tokens", "requests", "unknown_requests"):
        result[key] = int(left.get(key, 0) or 0) + int(right.get(key, 0) or 0)
    result["known"] = bool(left.get("known", True) and right.get("known", True))
    return result


class SessionStore:
    def __init__(self, path: str | Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY, title TEXT NOT NULL, updated_at TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS messages (
            id TEXT PRIMARY KEY, session_id TEXT NOT NULL, role TEXT NOT NULL,
            content TEXT NOT NULL, created_at TEXT NOT NULL, trace TEXT NOT NULL,
            usage TEXT NOT NULL, model_messages TEXT NOT NULL);
        """)
        self.db.commit()

    def create(self) -> dict:
        session_id = uuid.uuid4().hex
        with self._lock, self.db:
            self.db.execute("INSERT INTO sessions VALUES (?,?,?)", (session_id, "Новый диалог", now()))
        return self.get(session_id)

    def get(self, session_id: str) -> dict | None:
        with self._lock:
            row = self.db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            if row is None:
                return None
            rows = self.db.execute("SELECT * FROM messages WHERE session_id=? ORDER BY rowid", (session_id,)).fetchall()
        usage, messages = empty_usage(), []
        for item in rows:
            message = dict(item)
            for key in ("trace", "usage", "model_messages"):
                message[key] = json.loads(message[key])
            usage = add_usage(usage, message["usage"])
            message.pop("session_id")
            message.pop("model_messages")
            messages.append(message)
        return {**dict(row), "messages": messages, "usage": usage,
                "context_tokens": self.estimate_tokens([{"role": "system", "content": SYSTEM_PROMPT},
                                                        *self.history(session_id)]), "context_estimated": True}

    def list(self) -> list[dict]:
        with self._lock:
            ids = self.db.execute("SELECT id FROM sessions ORDER BY updated_at DESC").fetchall()
        return [{key: value for key, value in self.get(row["id"]).items()
                 if key in ("id", "title", "updated_at", "usage")} for row in ids]

    def history(self, session_id: str) -> list[dict]:
        with self._lock:
            rows = self.db.execute("SELECT role,content,model_messages FROM messages WHERE session_id=? ORDER BY rowid", (session_id,)).fetchall()
        result = []
        for row in rows:
            stored = json.loads(row["model_messages"])
            result.extend(stored or [{"role": row["role"], "content": row["content"]}])
        return result

    def save_turn(self, session_id: str, user: str, answer: str, trace: list,
                  usage: dict, model_messages: list | None = None) -> str:
        assistant_id = uuid.uuid4().hex
        timestamp = now()
        with self._lock, self.db:
            # The user and assistant appear together; interrupted tool exchanges
            # never leave an invalid tool_call without a corresponding result.
            self.db.execute("INSERT INTO messages VALUES (?,?,?,?,?,?,?,?)",
                            (uuid.uuid4().hex, session_id, "user", user, timestamp, "[]", "{}", "[]"))
            self.db.execute("INSERT INTO messages VALUES (?,?,?,?,?,?,?,?)",
                            (assistant_id, session_id, "assistant", answer, timestamp,
                             json.dumps(trace, ensure_ascii=False), json.dumps(usage),
                             json.dumps(model_messages or [{"role": "assistant", "content": answer}], ensure_ascii=False)))
            self.db.execute("UPDATE sessions SET title=CASE WHEN title='Новый диалог' THEN ? ELSE title END,updated_at=? WHERE id=?",
                            (user[:70], timestamp, session_id))
        return assistant_id

    def delete(self, session_id: str) -> None:
        with self._lock, self.db:
            self.db.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
            self.db.execute("DELETE FROM sessions WHERE id=?", (session_id,))

    @staticmethod
    def estimate_tokens(messages: Any) -> int:
        # This is a visible context-size estimate, never provider billing usage.
        return (len(json.dumps(messages, ensure_ascii=False)) + 2) // 3

    def close(self) -> None:
        self.db.close()
