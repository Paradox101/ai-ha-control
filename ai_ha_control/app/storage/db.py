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

CREATE TABLE IF NOT EXISTS incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    opened REAL NOT NULL,
    updated REAL NOT NULL,
    kind TEXT NOT NULL,
    severity TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    title TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS incident_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id INTEGER NOT NULL,
    ts REAL NOT NULL,
    text TEXT NOT NULL,
    entity_id TEXT,
    snapshot TEXT
);
CREATE INDEX IF NOT EXISTS idx_incident_events ON incident_events(incident_id, id);

CREATE TABLE IF NOT EXISTS followups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created REAL NOT NULL,
    due REAL NOT NULL,
    note TEXT NOT NULL,
    incident_id INTEGER,
    done INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS house_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    text TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    domain TEXT NOT NULL,
    service TEXT NOT NULL,
    entity_ids TEXT NOT NULL,
    data TEXT NOT NULL,
    previous TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    undone INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS baseline (
    entity_id TEXT NOT NULL,
    bucket INTEGER NOT NULL,
    n INTEGER NOT NULL,
    mean REAL NOT NULL,
    m2 REAL NOT NULL,
    PRIMARY KEY (entity_id, bucket)
);

CREATE TABLE IF NOT EXISTS activity (
    entity_id TEXT NOT NULL,
    bucket INTEGER NOT NULL,
    count INTEGER NOT NULL,
    PRIMARY KEY (entity_id, bucket)
);

CREATE TABLE IF NOT EXISTS observed (
    entity_id TEXT PRIMARY KEY,
    first_seen REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS mutes (
    key TEXT PRIMARY KEY,
    ts REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    trigger TEXT NOT NULL,
    summary TEXT NOT NULL,
    tools TEXT NOT NULL
);
"""

# Columns added after the first release; ALTER TABLE for existing databases.
MIGRATIONS = {
    "events": {"ref": "TEXT", "feedback": "INTEGER", "incident_id": "INTEGER"},
}


class Database:
    def __init__(self, data_dir: str) -> None:
        path = Path(data_dir)
        path.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path / "ai_ha_control.db", check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        for table, columns in MIGRATIONS.items():
            existing = {row["name"] for row in self._conn.execute(f"PRAGMA table_info({table})")}
            for name, sql_type in columns.items():
                if name not in existing:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")
        self._conn.commit()

    def _insert(self, sql: str, params: tuple) -> int:
        cur = self._conn.execute(sql, params)
        self._conn.commit()
        return int(cur.lastrowid)

    def _rows(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

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
                  pushed: bool = False, ref: str | None = None, incident_id: int | None = None) -> int:
        return self._insert(
            "INSERT INTO events (ts, kind, title, message, entity_id, pushed, ref, incident_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (time.time(), kind, title, message, entity_id, int(pushed), ref, incident_id),
        )

    def count_events(self, kind: str, since: float, incident_id: int | None = None) -> int:
        sql, params = "SELECT COUNT(*) FROM events WHERE kind = ? AND ts >= ?", [kind, since]
        if incident_id is not None:
            sql += " AND incident_id = ?"
            params.append(incident_id)
        return self._conn.execute(sql, params).fetchone()[0]

    def set_event_pushed(self, event_id: int) -> None:
        self._conn.execute("UPDATE events SET pushed = 1 WHERE id = ?", (event_id,))
        self._conn.commit()

    def get_event(self, event_id: int) -> dict[str, Any] | None:
        rows = self._rows("SELECT * FROM events WHERE id = ?", (event_id,))
        return rows[0] if rows else None

    def set_event_feedback(self, event_id: int, value: int) -> None:
        self._conn.execute("UPDATE events SET feedback = ? WHERE id = ?", (value, event_id))
        self._conn.commit()

    def negative_feedback(self, limit: int = 10) -> list[dict[str, Any]]:
        return self._rows("SELECT title, message FROM events WHERE feedback < 0 ORDER BY id DESC LIMIT ?", (limit,))

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

    # --- Incidents --------------------------------------------------------------

    def open_incident(self, kind: str, severity: str, title: str, summary: str = "") -> int:
        now = time.time()
        return self._insert(
            "INSERT INTO incidents (opened, updated, kind, severity, title, summary) VALUES (?, ?, ?, ?, ?, ?)",
            (now, now, kind, severity, title, summary),
        )

    def update_incident(self, incident_id: int, **fields: Any) -> None:
        allowed = {k: v for k, v in fields.items() if k in ("severity", "status", "title", "summary") and v}
        allowed["updated"] = time.time()
        sets = ", ".join(f"{k} = ?" for k in allowed)
        self._conn.execute(f"UPDATE incidents SET {sets} WHERE id = ?", (*allowed.values(), incident_id))
        self._conn.commit()

    def add_incident_event(self, incident_id: int, text: str, entity_id: str | None = None,
                           snapshot: str | None = None) -> int:
        event_id = self._insert(
            "INSERT INTO incident_events (incident_id, ts, text, entity_id, snapshot) VALUES (?, ?, ?, ?, ?)",
            (incident_id, time.time(), text, entity_id, snapshot),
        )
        self.update_incident(incident_id)
        return event_id

    def get_incident(self, incident_id: int) -> dict[str, Any] | None:
        rows = self._rows("SELECT * FROM incidents WHERE id = ?", (incident_id,))
        if not rows:
            return None
        return {**rows[0], "events": self._rows(
            "SELECT * FROM incident_events WHERE incident_id = ? ORDER BY id", (incident_id,))}

    def incidents(self, status: str | None = None, since: float = 0, limit: int = 50) -> list[dict[str, Any]]:
        if status == "active":
            return self._rows("SELECT * FROM incidents WHERE status != 'resolved' ORDER BY updated DESC LIMIT ?",
                              (limit,))
        return self._rows("SELECT * FROM incidents WHERE updated >= ? ORDER BY updated DESC LIMIT ?", (since, limit))

    # --- Follow-ups -------------------------------------------------------------

    def add_followup(self, due: float, note: str, incident_id: int | None = None) -> int:
        return self._insert("INSERT INTO followups (created, due, note, incident_id) VALUES (?, ?, ?, ?)",
                            (time.time(), due, note, incident_id))

    def due_followups(self, now: float | None = None) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM followups WHERE done = 0 AND due <= ? ORDER BY due",
                          (now or time.time(),))

    def pending_followups(self) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM followups WHERE done = 0 ORDER BY due")

    def complete_followups(self, ids: list[int]) -> None:
        self._conn.executemany("UPDATE followups SET done = 1 WHERE id = ?", [(i,) for i in ids])
        self._conn.commit()

    # --- House rules ------------------------------------------------------------

    def add_rule(self, text: str) -> int:
        return self._insert("INSERT INTO house_rules (ts, text) VALUES (?, ?)", (time.time(), text))

    def rules(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        where = "WHERE enabled = 1" if enabled_only else ""
        return self._rows(f"SELECT * FROM house_rules {where} ORDER BY id")

    def set_rule_enabled(self, rule_id: int, enabled: bool) -> None:
        self._conn.execute("UPDATE house_rules SET enabled = ? WHERE id = ?", (int(enabled), rule_id))
        self._conn.commit()

    def delete_rule(self, rule_id: int) -> bool:
        cur = self._conn.execute("DELETE FROM house_rules WHERE id = ?", (rule_id,))
        self._conn.commit()
        return cur.rowcount > 0

    # --- Autonomous actions (for undo) -------------------------------------------

    def add_action(self, domain: str, service: str, entity_ids: list[str], data: dict[str, Any],
                   previous: dict[str, Any], reason: str = "") -> int:
        return self._insert(
            "INSERT INTO actions (ts, domain, service, entity_ids, data, previous, reason) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (time.time(), domain, service, json.dumps(entity_ids), json.dumps(data, ensure_ascii=False),
             json.dumps(previous, ensure_ascii=False), reason),
        )

    def get_action(self, action_id: int) -> dict[str, Any] | None:
        rows = self._rows("SELECT * FROM actions WHERE id = ?", (action_id,))
        if not rows:
            return None
        row = rows[0]
        return {**row, "entity_ids": json.loads(row["entity_ids"]), "data": json.loads(row["data"]),
                "previous": json.loads(row["previous"])}

    def recent_actions(self, since: float = 0, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._rows("SELECT id FROM actions WHERE ts >= ? ORDER BY id DESC LIMIT ?", (since, limit))
        return [self.get_action(r["id"]) for r in rows]

    def count_actions(self, since: float) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM actions WHERE ts >= ?", (since,)).fetchone()[0]

    def mark_undone(self, action_id: int) -> None:
        self._conn.execute("UPDATE actions SET undone = 1 WHERE id = ?", (action_id,))
        self._conn.commit()

    # --- Baselines ----------------------------------------------------------------

    def baseline_update(self, entity_id: str, bucket: int, value: float) -> tuple[int, float, float]:
        """Welford update; returns (n, mean, variance) before adding the value."""
        row = self._conn.execute("SELECT n, mean, m2 FROM baseline WHERE entity_id = ? AND bucket = ?",
                                 (entity_id, bucket)).fetchone()
        n, mean, m2 = (row["n"], row["mean"], row["m2"]) if row else (0, 0.0, 0.0)
        before = (n, mean, m2 / (n - 1) if n > 1 else 0.0)
        n += 1
        delta = value - mean
        mean += delta / n
        m2 += delta * (value - mean)
        self._conn.execute("INSERT OR REPLACE INTO baseline (entity_id, bucket, n, mean, m2) VALUES (?, ?, ?, ?, ?)",
                           (entity_id, bucket, n, mean, m2))
        self._conn.commit()
        return before

    def activity_increment(self, entity_id: str, bucket: int) -> int:
        """Count an activation; returns the count before this one."""
        row = self._conn.execute("SELECT count FROM activity WHERE entity_id = ? AND bucket = ?",
                                 (entity_id, bucket)).fetchone()
        count = row["count"] if row else 0
        self._conn.execute("INSERT OR REPLACE INTO activity (entity_id, bucket, count) VALUES (?, ?, ?)",
                           (entity_id, bucket, count + 1))
        self._conn.commit()
        return count

    def first_seen(self, entity_id: str) -> float:
        now = time.time()
        self._conn.execute("INSERT OR IGNORE INTO observed (entity_id, first_seen) VALUES (?, ?)", (entity_id, now))
        self._conn.commit()
        return self._conn.execute("SELECT first_seen FROM observed WHERE entity_id = ?", (entity_id,)).fetchone()[0]

    def mute(self, key: str) -> None:
        self._conn.execute("INSERT OR REPLACE INTO mutes (key, ts) VALUES (?, ?)", (key, time.time()))
        self._conn.commit()

    def unmute(self, key: str) -> None:
        self._conn.execute("DELETE FROM mutes WHERE key = ?", (key,))
        self._conn.commit()

    def is_muted(self, key: str) -> bool:
        return self._conn.execute("SELECT 1 FROM mutes WHERE key = ?", (key,)).fetchone() is not None

    def mutes(self) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM mutes ORDER BY ts DESC")

    # --- Agent log --------------------------------------------------------------------

    def add_agent_run(self, trigger: str, summary: str, tools: list[str]) -> int:
        return self._insert("INSERT INTO agent_runs (ts, trigger, summary, tools) VALUES (?, ?, ?, ?)",
                            (time.time(), trigger, summary, json.dumps(tools)))

    def agent_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        return [{**r, "tools": json.loads(r["tools"])}
                for r in self._rows("SELECT * FROM agent_runs ORDER BY id DESC LIMIT ?", (limit,))]
