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
