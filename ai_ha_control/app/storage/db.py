"""SQLite storage for conversations and the action audit log."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    ts REAL NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id, id);

CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    conversation_id TEXT,
    action TEXT NOT NULL,
    arguments TEXT NOT NULL,
    decision TEXT NOT NULL,
    result TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    message TEXT NOT NULL,
    entity_id TEXT,
    pushed INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    text TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS suggestions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    entity_id TEXT NOT NULL,
    state TEXT NOT NULL,
    time TEXT NOT NULL,
    weekdays_only INTEGER NOT NULL,
    days INTEGER NOT NULL,
    description TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'new'
);
"""


class Database:
    def __init__(self, data_dir: str) -> None:
        path = Path(data_dir)
        path.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path / "ai_ha_control.db", check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    # --- Conversations ------------------------------------------------------

    def add_message(self, conversation_id: str, role: str, content: str) -> None:
        self._conn.execute(
            "INSERT INTO messages (conversation_id, ts, role, content) VALUES (?, ?, ?, ?)",
            (conversation_id, time.time(), role, content),
        )
        self._conn.commit()

    def recent_messages(self, conversation_id: str, limit: int = 10) -> list[dict[str, str]]:
        rows = self._conn.execute(
            "SELECT role, content FROM messages WHERE conversation_id = ? ORDER BY id DESC LIMIT ?",
            (conversation_id, limit),
        ).fetchall()
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

    # --- Audit log ------------------------------------------------------------

    def audit(
        self,
        action: str,
        arguments: dict[str, Any],
        decision: str,
        result: str | None = None,
        conversation_id: str | None = None,
    ) -> int:
        cur = self._conn.execute(
            "INSERT INTO audit (ts, conversation_id, action, arguments, decision, result) VALUES (?, ?, ?, ?, ?, ?)",
            (time.time(), conversation_id, action, json.dumps(arguments, ensure_ascii=False), decision, result),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def audit_log(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [{**dict(r), "arguments": json.loads(r["arguments"])} for r in rows]

    # --- Events (proactive notifications) --------------------------------------

    def add_event(self, kind: str, title: str, message: str, entity_id: str | None = None,
                  pushed: bool = False) -> int:
        cur = self._conn.execute(
            "INSERT INTO events (ts, kind, title, message, entity_id, pushed) VALUES (?, ?, ?, ?, ?, ?)",
            (time.time(), kind, title, message, entity_id, int(pushed)),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def events(self, since: float | None = None, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM events WHERE ts >= ? ORDER BY id DESC LIMIT ?", (since or 0, limit)
        ).fetchall()
        return [dict(r) for r in rows]

    # --- Memories -------------------------------------------------------------

    def add_memory(self, text: str) -> int:
        cur = self._conn.execute("INSERT INTO memories (ts, text) VALUES (?, ?)", (time.time(), text))
        self._conn.commit()
        return int(cur.lastrowid)

    def memories(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM memories ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in reversed(rows)]

    def delete_memory(self, memory_id: int) -> bool:
        cur = self._conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
        self._conn.commit()
        return cur.rowcount > 0

    # --- Routine suggestions ------------------------------------------------------

    def add_suggestion(self, entity_id: str, state: str, at: str, weekdays_only: bool, days: int,
                       description: str) -> int | None:
        """Store a suggestion unless a similar one (same entity/state, ±30 min) already exists."""
        hour, minute = map(int, at.split(":"))
        minutes = hour * 60 + minute
        for row in self._conn.execute(
            "SELECT time FROM suggestions WHERE entity_id = ? AND state = ?", (entity_id, state)
        ).fetchall():
            h, m = map(int, row["time"].split(":"))
            if abs(h * 60 + m - minutes) <= 30:
                return None
        cur = self._conn.execute(
            "INSERT INTO suggestions (ts, entity_id, state, time, weekdays_only, days, description) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (time.time(), entity_id, state, at, int(weekdays_only), days, description),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def suggestions(self, status: str | None = "new") -> list[dict[str, Any]]:
        if status:
            rows = self._conn.execute("SELECT * FROM suggestions WHERE status = ? ORDER BY id", (status,))
        else:
            rows = self._conn.execute("SELECT * FROM suggestions ORDER BY id")
        return [{**dict(r), "weekdays_only": bool(r["weekdays_only"])} for r in rows.fetchall()]

    def get_suggestion(self, suggestion_id: int) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM suggestions WHERE id = ?", (suggestion_id,)).fetchone()
        return {**dict(row), "weekdays_only": bool(row["weekdays_only"])} if row else None

    def set_suggestion_status(self, suggestion_id: int, status: str) -> None:
        self._conn.execute("UPDATE suggestions SET status = ? WHERE id = ?", (status, suggestion_id))
        self._conn.commit()
