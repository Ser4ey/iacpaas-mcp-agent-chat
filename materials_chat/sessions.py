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
from .usage import add_usage, empty_usage, with_cost

COMPACTED_NOTE = "…[сокращено: полный результат был получен в одном из прошлых ходов]"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def compact_turn(messages: list[dict], tool_chars: int) -> list[dict]:
    """Старый ход для модели: результаты инструментов укорачиваются, рассуждения убираются.

    Пары tool_call → tool сохраняются целиком, иначе провайдер отклонит историю.
    """
    result = []
    for message in messages:
        message = {key: value for key, value in message.items() if key != "reasoning_details"}
        content = message.get("content")
        if message.get("role") == "tool" and isinstance(content, str) and len(content) > tool_chars:
            message["content"] = content[:tool_chars] + COMPACTED_NOTE
        result.append(message)
    return result


class SessionStore:
    def __init__(self, path: str | Path, full_turns: int = 2, tool_chars: int = 1500):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.full_turns, self.tool_chars = full_turns, tool_chars
        self.model_info: dict | None = None  # цены модели для оценки старых сообщений
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
        # Миграция: признак «ответ опирается на данные инструментов», ошибка хода
        # (отдельно от текста ответа) и сверка чисел появились позже.
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(messages)")}
        if "grounded" not in columns:
            self.db.execute("ALTER TABLE messages ADD COLUMN grounded INTEGER")
        if "error" not in columns:
            self.db.execute("ALTER TABLE messages ADD COLUMN error TEXT")
        if "number_check" not in columns:
            self.db.execute("ALTER TABLE messages ADD COLUMN number_check TEXT")
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
            message["usage"] = with_cost(message["usage"], self.model_info)
            message["grounded"] = None if message["grounded"] is None else bool(message["grounded"])
            message["error"] = json.loads(message["error"]) if message["error"] else None
            message["number_check"] = json.loads(message["number_check"]) if message["number_check"] else None
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
        """Сообщения для модели. Последние full_turns ходов — полностью, более старые — сжато."""
        full_turns, tool_chars = self.full_turns, self.tool_chars
        with self._lock:
            rows = self.db.execute("SELECT role,content,model_messages FROM messages WHERE session_id=? ORDER BY rowid",
                                   (session_id,)).fetchall()
        turns = sum(1 for row in rows if row["role"] == "assistant")
        result, turn = [], 0
        for row in rows:
            stored = json.loads(row["model_messages"]) or [{"role": row["role"], "content": row["content"]}]
            if row["role"] == "assistant":
                turn += 1
                if turn <= turns - full_turns:
                    stored = compact_turn(stored, tool_chars)
            result.extend(stored)
        return result

    def traces(self, session_id: str) -> list[dict]:
        """Записи журнала инструментов всех ответов диалога по порядку: данные для сверки чисел."""
        with self._lock:
            rows = self.db.execute("SELECT trace FROM messages WHERE session_id=? AND role='assistant' ORDER BY rowid",
                                   (session_id,)).fetchall()
        return [entry for row in rows for entry in json.loads(row["trace"])]

    def save_turn(self, session_id: str, user: str, answer: str, trace: list, usage: dict,
                  model_messages: list | None = None, grounded: bool | None = None,
                  error: dict | None = None, number_check: dict | None = None) -> str:
        """error — {"message", "code"}: остановка или сбой хода, хранится отдельно от текста ответа.

        number_check — результат сверки чисел ответа с данными инструментов (answer_check.check_numbers).
        """
        assistant_id = uuid.uuid4().hex
        timestamp = now()
        insert = ("INSERT INTO messages (id, session_id, role, content, created_at, trace, usage, "
                  "model_messages, grounded, error, number_check) VALUES (?,?,?,?,?,?,?,?,?,?,?)")
        with self._lock, self.db:
            # The user and assistant appear together; interrupted tool exchanges
            # never leave an invalid tool_call without a corresponding result.
            self.db.execute(insert, (uuid.uuid4().hex, session_id, "user", user, timestamp, "[]", "{}", "[]", None, None, None))
            self.db.execute(insert, (assistant_id, session_id, "assistant", answer, timestamp,
                                     json.dumps(trace, ensure_ascii=False), json.dumps(usage),
                                     json.dumps(model_messages or [{"role": "assistant", "content": answer}], ensure_ascii=False),
                                     None if grounded is None else int(grounded),
                                     json.dumps(error, ensure_ascii=False) if error else None,
                                     json.dumps(number_check, ensure_ascii=False) if number_check is not None else None))
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
