"""SQLite storage for conversations and the action audit log."""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger(__name__)

DB_NAME = "ai_ha_control.db"
# Written by backup_pre (a consistent copy for Home Assistant backups); the live file is excluded.
BACKUP_NAME = "ai_ha_control.backup.db"

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

CREATE TABLE IF NOT EXISTS overrides (
    entity_id TEXT PRIMARY KEY,
    ts REAL NOT NULL,
    until REAL NOT NULL,
    note TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS manual_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    entity_id TEXT NOT NULL,
    mode TEXT NOT NULL,
    value REAL
);
CREATE INDEX IF NOT EXISTS idx_manual_changes ON manual_changes(entity_id, ts);

CREATE TABLE IF NOT EXISTS confirmations (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    description TEXT NOT NULL,
    reason TEXT NOT NULL,
    payload TEXT NOT NULL,
    conversation_id TEXT,
    created REAL NOT NULL,
    sensitive INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS arrivals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    weekday INTEGER NOT NULL,
    minute INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS comfort_profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    area TEXT NOT NULL,
    climate_entity TEXT NOT NULL DEFAULT '',
    targets TEXT NOT NULL DEFAULT '{}',
    preheat_minutes INTEGER NOT NULL DEFAULT 0,
    lights TEXT NOT NULL DEFAULT '[]',
    ventilation_entity TEXT NOT NULL DEFAULT '',
    shade_covers TEXT NOT NULL DEFAULT '[]',
    enabled INTEGER NOT NULL DEFAULT 1,
    backup_entity TEXT NOT NULL DEFAULT '',
    backup TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS agent_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    trigger TEXT NOT NULL,
    summary TEXT NOT NULL,
    tools TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS watch_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    entity_id TEXT NOT NULL,
    op TEXT NOT NULL,
    value TEXT NOT NULL,
    minutes INTEGER NOT NULL DEFAULT 0,
    modes TEXT NOT NULL DEFAULT '[]',
    severity TEXT NOT NULL DEFAULT 'warning',
    label TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    since REAL,
    alerted INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS scheduled (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    due REAL NOT NULL,
    kind TEXT NOT NULL,
    text TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    conversation_id TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
);

CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(ts);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit(ts);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_actions_ts ON actions(ts);
CREATE INDEX IF NOT EXISTS idx_agent_runs_ts ON agent_runs(ts);
CREATE INDEX IF NOT EXISTS idx_incidents_updated ON incidents(updated);
"""

# Columns added after the first release; ALTER TABLE for existing databases.
MIGRATIONS = {
    "events": {"ref": "TEXT", "feedback": "INTEGER", "incident_id": "INTEGER",
               "escalate_at": "REAL", "escalations": "INTEGER NOT NULL DEFAULT 0", "acked": "REAL"},
    "actions": {"shadow": "INTEGER NOT NULL DEFAULT 0", "feedback": "INTEGER", "verified": "TEXT"},
    "agent_runs": {"report": "TEXT", "feedback": "INTEGER"},
    "comfort_profiles": {"backup_entity": "TEXT NOT NULL DEFAULT ''", "backup": "TEXT NOT NULL DEFAULT '{}'",
                         "temperature_sensor": "TEXT NOT NULL DEFAULT ''", "pause_entity": "TEXT NOT NULL DEFAULT ''"},
}
PROFILE_JSON = ("targets", "lights", "shade_covers", "backup")
BASELINE_MEMORY = 200  # samples per bucket before older ones start to fade


# Days to keep rows in the growing tables (memories, rules, suggestions and baselines are kept).
RETENTION_DAYS = {
    "messages": 30,
    "agent_runs": 90,
    "events": 180,
    "audit": 365,
    "actions": 365,
}


def restore_backup(path: Path) -> None:
    """After restoring a Home Assistant backup only the backup copy exists: make it the live database."""
    live, backup = path / DB_NAME, path / BACKUP_NAME
    if not live.exists() and backup.exists():
        _LOGGER.warning("Database missing, restoring it from %s", backup.name)
        backup.replace(live)


def write_backup(data_dir: str) -> Path:
    """Consistent copy of the live database (safe while the add-on keeps writing)."""
    path = Path(data_dir)
    target = path / BACKUP_NAME
    tmp = target.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    src = sqlite3.connect(path / DB_NAME, timeout=30)
    try:
        dst = sqlite3.connect(tmp)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    tmp.replace(target)
    return target


class Database:
    def __init__(self, data_dir: str) -> None:
        path = Path(data_dir)
        path.mkdir(parents=True, exist_ok=True)
        restore_backup(path)
        self._conn = sqlite3.connect(path / DB_NAME, check_same_thread=False, timeout=10)
        self._conn.row_factory = sqlite3.Row
        # WAL: readers never block, commits are cheap and a crash cannot leave a half-written page.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
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

    # --- Maintenance --------------------------------------------------------------

    def prune(self, now: float | None = None) -> dict[str, int]:
        """Delete rows past their retention; returns the number removed per table."""
        now = now or time.time()
        removed = {}
        for table, days in RETENTION_DAYS.items():
            cur = self._conn.execute(f"DELETE FROM {table} WHERE ts < ?", (now - days * 86400,))
            removed[table] = cur.rowcount
        limit = now - 365 * 86400
        cur = self._conn.execute(
            "DELETE FROM incidents WHERE status = 'resolved' AND updated < ?", (limit,))
        removed["incidents"] = cur.rowcount
        self._conn.execute("DELETE FROM incident_events WHERE incident_id NOT IN (SELECT id FROM incidents)")
        self._conn.execute("DELETE FROM followups WHERE done = 1 AND due < ?", (now - 30 * 86400,))
        self._conn.execute("DELETE FROM scheduled WHERE status != 'pending' AND due < ?", (now - 30 * 86400,))
        self._conn.commit()
        self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        if sum(removed.values()) > 1000:
            self._conn.execute("VACUUM")
        return removed

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

    def set_escalation(self, event_id: int, at: float | None, level: int = 0) -> None:
        self._conn.execute("UPDATE events SET escalate_at = ?, escalations = ? WHERE id = ?", (at, level, event_id))
        self._conn.commit()

    def due_escalations(self, now: float | None = None) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM events WHERE escalate_at IS NOT NULL AND escalate_at <= ? AND acked IS NULL",
                          (now or time.time(),))

    def ack_events(self, event_id: int | None = None, incident_id: int | None = None) -> int:
        """Mark a critical notification (or all of an incident) as seen; returns how many were still escalating."""
        column, value = ("incident_id", incident_id) if incident_id is not None else ("id", event_id)
        pending = self._conn.execute(
            f"SELECT COUNT(*) FROM events WHERE {column} = ? AND escalate_at IS NOT NULL AND acked IS NULL",
            (value,)).fetchone()[0]
        self._conn.execute(f"UPDATE events SET acked = COALESCE(acked, ?), escalate_at = NULL WHERE {column} = ?",
                           (time.time(), value))
        self._conn.commit()
        return pending

    def negative_feedback(self, limit: int = 10) -> list[dict[str, Any]]:
        return self._rows("SELECT title, message FROM events WHERE feedback < 0 ORDER BY id DESC LIMIT ?", (limit,))

    def events(self, since: float | None = None, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM events WHERE ts >= ? ORDER BY id DESC LIMIT ?", (since or 0, limit)
        ).fetchall()
        return [dict(r) for r in rows]

    def unpushed_events(self, kinds: set[str], since: float) -> list[dict[str, Any]]:
        """Events of these kinds that were not pushed yet, newest first (no limit: a digest must not drop any)."""
        return self._rows(f"SELECT * FROM events WHERE ts >= ? AND pushed = 0 AND kind IN "
                          f"({','.join('?' * len(kinds))}) ORDER BY id DESC", (since, *sorted(kinds)))

    def events_between(self, since: float, until: float, kind: str | None = None,
                       entity_ids: list[str] | None = None) -> list[dict[str, Any]]:
        sql, params = "SELECT * FROM events WHERE ts >= ? AND ts < ?", [since, until]
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        if entity_ids is not None:
            sql += f" AND entity_id IN ({','.join('?' * len(entity_ids))})"
            params += entity_ids
        return self._rows(sql + " ORDER BY ts", tuple(params))

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
        # unseen: critical notifications of the incident nobody marked as seen yet (still escalating)
        columns = ("*, (SELECT COUNT(*) FROM events e WHERE e.incident_id = incidents.id "
                   "AND e.escalate_at IS NOT NULL AND e.acked IS NULL) AS unseen")
        if status == "active":
            return self._rows(f"SELECT {columns} FROM incidents WHERE status != 'resolved' "
                              "ORDER BY updated DESC LIMIT ?", (limit,))
        return self._rows(f"SELECT {columns} FROM incidents WHERE updated >= ? ORDER BY updated DESC LIMIT ?",
                          (since, limit))

    # --- Follow-ups -------------------------------------------------------------

    def add_followup(self, due: float, note: str, incident_id: int | None = None) -> int:
        return self._insert("INSERT INTO followups (created, due, note, incident_id) VALUES (?, ?, ?, ?)",
                            (time.time(), due, note, incident_id))

    def due_followups(self, now: float | None = None) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM followups WHERE done = 0 AND due <= ? ORDER BY due",
                          (now or time.time(),))

    def pending_followups(self) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM followups WHERE done = 0 ORDER BY due")

    def postpone_followups(self, ids: list[int], seconds: float) -> None:
        if ids:
            self._conn.execute(f"UPDATE followups SET due = ? WHERE id IN ({','.join('?' * len(ids))})",
                               (time.time() + seconds, *ids))
            self._conn.commit()

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
                   previous: dict[str, Any], reason: str = "", shadow: bool = False) -> int:
        return self._insert(
            "INSERT INTO actions (ts, domain, service, entity_ids, data, previous, reason, shadow) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (time.time(), domain, service, json.dumps(entity_ids), json.dumps(data, ensure_ascii=False),
             json.dumps(previous, ensure_ascii=False), reason, int(shadow)),
        )

    def set_action_feedback(self, action_id: int, value: int) -> None:
        self._conn.execute("UPDATE actions SET feedback = ? WHERE id = ?", (value, action_id))
        self._conn.commit()

    def set_action_verified(self, action_id: int, result: str) -> None:
        self._conn.execute("UPDATE actions SET verified = ? WHERE id = ?", (result, action_id))
        self._conn.commit()

    def shadow_scores(self) -> list[dict[str, Any]]:
        """Per domain: how many shadow actions were recorded and how the resident judged them."""
        return self._rows(
            "SELECT domain, COUNT(*) AS total, SUM(feedback > 0) AS good, SUM(feedback < 0) AS bad "
            "FROM actions WHERE shadow = 1 GROUP BY domain ORDER BY domain")

    def last_action_on(self, entity_id: str, since: float) -> dict[str, Any] | None:
        """The latest real (not shadow) action of the agent on this entity."""
        rows = self._rows("SELECT id FROM actions WHERE shadow = 0 AND undone = 0 AND ts >= ? AND entity_ids LIKE ? "
                          "ORDER BY id DESC LIMIT 1", (since, f'%"{entity_id}"%'))
        return self.get_action(rows[0]["id"]) if rows else None

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
        return self._conn.execute("SELECT COUNT(*) FROM actions WHERE ts >= ? AND shadow = 0",
                                  (since,)).fetchone()[0]

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
        if n >= BASELINE_MEMORY:
            # Slowly forget: old samples weigh less, so the baseline follows the seasons.
            m2 = m2 * (BASELINE_MEMORY - 2) / (n - 1)
            n = BASELINE_MEMORY - 1
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

    def activity_by_hour(self, entity_ids: list[str]) -> dict[int, int]:
        """Learned activations per hour of the day, summed over the given sensors."""
        if not entity_ids:
            return {}
        marks = ",".join("?" * len(entity_ids))
        rows = self._rows(f"SELECT bucket, SUM(count) AS n FROM activity WHERE entity_id IN ({marks}) "
                          "GROUP BY bucket", tuple(entity_ids))
        return {int(r["bucket"]): int(r["n"]) for r in rows}

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

    def add_agent_run(self, trigger: str, summary: str, tools: list[str], report: str | None = None) -> int:
        return self._insert("INSERT INTO agent_runs (ts, trigger, summary, tools, report) VALUES (?, ?, ?, ?, ?)",
                            (time.time(), trigger, summary, json.dumps(tools), report))

    def set_run_feedback(self, run_id: int, value: int) -> None:
        self._conn.execute("UPDATE agent_runs SET feedback = ? WHERE id = ?", (value, run_id))
        self._conn.commit()

    def runs_with_reports(self, limit: int = 200) -> list[dict[str, Any]]:
        return [{**r, "tools": json.loads(r["tools"])} for r in self._rows(
            "SELECT * FROM agent_runs WHERE report IS NOT NULL ORDER BY id DESC LIMIT ?", (limit,))]

    # --- Manual overrides -------------------------------------------------------------

    def set_override(self, entity_id: str, until: float, note: str = "") -> None:
        self._conn.execute("INSERT OR REPLACE INTO overrides (entity_id, ts, until, note) VALUES (?, ?, ?, ?)",
                           (entity_id, time.time(), until, note))
        self._conn.commit()

    def active_overrides(self, now: float | None = None) -> dict[str, dict[str, Any]]:
        return {r["entity_id"]: r for r in self._rows("SELECT * FROM overrides WHERE until > ?",
                                                       (now or time.time(),))}

    def clear_override(self, entity_id: str) -> None:
        self._conn.execute("DELETE FROM overrides WHERE entity_id = ?", (entity_id,))
        self._conn.commit()

    def add_manual_change(self, entity_id: str, mode: str, value: float | None) -> None:
        self._insert("INSERT INTO manual_changes (ts, entity_id, mode, value) VALUES (?, ?, ?, ?)",
                     (time.time(), entity_id, mode, value))

    def manual_changes(self, since: float, entity_id: str | None = None) -> list[dict[str, Any]]:
        if entity_id:
            return self._rows("SELECT * FROM manual_changes WHERE ts >= ? AND entity_id = ? ORDER BY id",
                              (since, entity_id))
        return self._rows("SELECT * FROM manual_changes WHERE ts >= ? ORDER BY id", (since,))

    # --- Pending confirmations ----------------------------------------------------------

    def save_confirmation(self, item: dict[str, Any]) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO confirmations (id, kind, description, reason, payload, conversation_id, created, "
            "sensitive) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (item["id"], item["kind"], item["description"], item["reason"],
             json.dumps(item["payload"], ensure_ascii=False), item.get("conversation_id"), item["created"],
             int(item.get("sensitive", False))))
        self._conn.commit()

    def delete_confirmation(self, confirmation_id: str) -> None:
        self._conn.execute("DELETE FROM confirmations WHERE id = ?", (confirmation_id,))
        self._conn.commit()

    def pending_confirmations(self) -> list[dict[str, Any]]:
        return [{**r, "payload": json.loads(r["payload"]), "sensitive": bool(r["sensitive"])}
                for r in self._rows("SELECT * FROM confirmations ORDER BY created")]

    # --- Arrivals (for preheating) ------------------------------------------------------

    def add_arrival(self, weekday: int, minute: int) -> None:
        self._insert("INSERT INTO arrivals (ts, weekday, minute) VALUES (?, ?, ?)", (time.time(), weekday, minute))

    def arrivals(self, weekday: int, since: float) -> list[int]:
        return [r["minute"] for r in self._rows(
            "SELECT minute FROM arrivals WHERE weekday = ? AND ts >= ? ORDER BY minute", (weekday, since))]

    # --- Comfort profiles ---------------------------------------------------------------

    def _profile(self, row: dict[str, Any]) -> dict[str, Any]:
        return {**row, **{k: json.loads(row[k]) for k in PROFILE_JSON}, "enabled": bool(row["enabled"])}

    def profiles(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM comfort_profiles" + (" WHERE enabled = 1" if enabled_only else "") + " ORDER BY area"
        return [self._profile(r) for r in self._rows(sql)]

    def get_profile(self, profile_id: int) -> dict[str, Any] | None:
        rows = self._rows("SELECT * FROM comfort_profiles WHERE id = ?", (profile_id,))
        return self._profile(rows[0]) if rows else None

    def save_profile(self, profile: dict[str, Any]) -> int:
        values = (profile["area"], profile.get("climate_entity") or "",
                  json.dumps(profile.get("targets") or {}), int(profile.get("preheat_minutes") or 0),
                  json.dumps(profile.get("lights") or []), profile.get("ventilation_entity") or "",
                  json.dumps(profile.get("shade_covers") or []), int(profile.get("enabled", True)),
                  profile.get("backup_entity") or "", json.dumps(profile.get("backup") or {}),
                  profile.get("temperature_sensor") or "", profile.get("pause_entity") or "")
        if profile.get("id"):
            self._conn.execute(
                "UPDATE comfort_profiles SET area = ?, climate_entity = ?, targets = ?, preheat_minutes = ?, "
                "lights = ?, ventilation_entity = ?, shade_covers = ?, enabled = ?, backup_entity = ?, backup = ?, "
                "temperature_sensor = ?, pause_entity = ? "
                "WHERE id = ?",
                (*values, int(profile["id"])))
            self._conn.commit()
            return int(profile["id"])
        return self._insert(
            "INSERT INTO comfort_profiles (area, climate_entity, targets, preheat_minutes, lights, "
            "ventilation_entity, shade_covers, enabled, backup_entity, backup, temperature_sensor, pause_entity) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            values)

    def delete_profile(self, profile_id: int) -> bool:
        cur = self._conn.execute("DELETE FROM comfort_profiles WHERE id = ?", (profile_id,))
        self._conn.commit()
        return cur.rowcount > 0

    def agent_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        return [{**r, "tools": json.loads(r["tools"])}
                for r in self._rows("SELECT * FROM agent_runs ORDER BY id DESC LIMIT ?", (limit,))]

    # --- Watch rules (thresholds checked without the LLM) ----------------------------

    def add_watch_rule(self, rule: dict[str, Any]) -> int:
        return self._insert(
            "INSERT INTO watch_rules (ts, entity_id, op, value, minutes, modes, severity, label) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (time.time(), rule["entity_id"], rule["op"], str(rule["value"]), int(rule.get("minutes") or 0),
             json.dumps(rule.get("modes") or []), rule.get("severity") or "warning", rule.get("label") or ""))

    def watch_rules(self, enabled_only: bool = False, entity_id: str | None = None) -> list[dict[str, Any]]:
        sql, params = "SELECT * FROM watch_rules WHERE 1 = 1", []
        if enabled_only:
            sql += " AND enabled = 1"
        if entity_id is not None:
            sql += " AND entity_id = ?"
            params.append(entity_id)
        return [{**r, "modes": json.loads(r["modes"]), "enabled": bool(r["enabled"]), "alerted": bool(r["alerted"])}
                for r in self._rows(sql + " ORDER BY id", tuple(params))]

    def set_watch_state(self, rule_id: int, since: float | None, alerted: bool) -> None:
        self._conn.execute("UPDATE watch_rules SET since = ?, alerted = ? WHERE id = ?", (since, int(alerted), rule_id))
        self._conn.commit()

    def set_watch_enabled(self, rule_id: int, enabled: bool) -> None:
        self._conn.execute("UPDATE watch_rules SET enabled = ?, since = NULL, alerted = 0 WHERE id = ?",
                           (int(enabled), rule_id))
        self._conn.commit()

    def delete_watch_rule(self, rule_id: int) -> bool:
        cur = self._conn.execute("DELETE FROM watch_rules WHERE id = ?", (rule_id,))
        self._conn.commit()
        return cur.rowcount > 0

    # --- Reminders and planned actions -----------------------------------------------

    def add_scheduled(self, due: float, kind: str, text: str, payload: dict[str, Any],
                      conversation_id: str | None = None) -> int:
        return self._insert(
            "INSERT INTO scheduled (ts, due, kind, text, payload, conversation_id) VALUES (?, ?, ?, ?, ?, ?)",
            (time.time(), due, kind, text, json.dumps(payload), conversation_id))

    def scheduled(self) -> list[dict[str, Any]]:
        return [{**r, "payload": json.loads(r["payload"])}
                for r in self._rows("SELECT * FROM scheduled WHERE status = 'pending' ORDER BY due")]

    def due_scheduled(self, now: float | None = None) -> list[dict[str, Any]]:
        return [{**r, "payload": json.loads(r["payload"])} for r in self._rows(
            "SELECT * FROM scheduled WHERE status = 'pending' AND due <= ? ORDER BY due", (now or time.time(),))]

    def finish_scheduled(self, item_id: int) -> None:
        self._conn.execute("UPDATE scheduled SET status = 'done' WHERE id = ?", (item_id,))
        self._conn.commit()

    def cancel_scheduled(self, item_id: int) -> bool:
        cur = self._conn.execute("UPDATE scheduled SET status = 'cancelled' WHERE id = ? AND status = 'pending'",
                                 (item_id,))
        self._conn.commit()
        return cur.rowcount > 0
