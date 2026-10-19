"""Database upkeep, health check, streaming Assist, integration state, structured vision, context tools."""

import asyncio
import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import pytest
from aiohttp.test_utils import TestClient, TestServer

from app.agent.loop import Agent
from app.config import Config
from app.ha.index import EntityInfo
from app.llm.ollama import LLMStats
from app.main import AUTH_FAILURE_LIMIT, create_app
from app.storage.db import BACKUP_NAME, DB_NAME, Database, write_backup
from app.tools.base import ToolRegistry
from app.tools.context import cheapest_window, energy_info, get_agenda
from app.watcher import vision
from app.watcher.baseline import MODE_GUEST, MODE_HOME, MODE_VACATION, house_mode

from .conftest import STATES
from .test_e2e import fake_home_assistant, fake_ollama


def test_prune_keeps_recent_rows_and_rules(tmp_path):
    db = Database(str(tmp_path))
    db.add_message("c", "user", "oud")
    db.add_message("c", "user", "nieuw")
    db.add_rule("Houd de woonkamer op 20 graden")
    old = time.time() - 400 * 86400
    db._conn.execute("UPDATE messages SET ts = ? WHERE content = 'oud'", (old,))
    incident = db.open_incident("test", "info", "Oud incident")
    db.add_incident_event(incident, "iets")
    db.update_incident(incident, status="resolved")
    db._conn.execute("UPDATE incidents SET updated = ? WHERE id = ?", (old, incident))
    db._conn.commit()
    removed = db.prune()
    assert removed["messages"] == 1 and removed["incidents"] == 1
    assert [m["content"] for m in db.recent_messages("c")] == ["nieuw"]
    assert db.rules() and db.get_incident(incident) is None
    assert db._conn.execute("SELECT COUNT(*) FROM incident_events").fetchone()[0] == 0
    assert db._conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_backup_copy_and_restore(tmp_path):
    db = Database(str(tmp_path))
    db.add_memory("De kat heet Mies")
    path = write_backup(str(tmp_path))
    assert path.name == BACKUP_NAME
    db.close()
    for name in (DB_NAME, DB_NAME + "-wal", DB_NAME + "-shm"):
        (tmp_path / name).unlink(missing_ok=True)  # what a restored backup looks like (live file excluded)
    restored = Database(str(tmp_path))
    assert [m["text"] for m in restored.memories()] == ["De kat heet Mies"]
    assert not (tmp_path / BACKUP_NAME).exists()


def test_llm_stats():
    stats = LLMStats()
    stats.record("qwen3:8b", 2.0, {"eval_count": 10, "prompt_eval_count": 100, "load_duration": 3e9})
    stats.record("qwen3:8b", 4.0, {}, error=True)
    stats.record_wait(1.0)
    summary = stats.summary(waiting=2)
    model = summary["models"]["qwen3:8b"]
    assert model["calls"] == 2 and model["errors"] == 1 and model["loads"] == 1
    assert model["output_tokens"] == 10 and summary["avg_seconds"] == 2.0
    assert summary["queue"] == 2 and summary["avg_wait_seconds"] == 1.0


def test_vision_parses_structured_and_plain_replies():
    assert vision.parse('{"categorie": "BEZOEK", "beschrijving": "Een bezorger bij de deur."}') == (
        vision.VISITOR, "Een bezorger bij de deur.")
    assert vision.parse("VERDACHT: iemand bij de schuur") == (vision.SUSPICIOUS, "iemand bij de schuur")
    assert vision.parse('{"categorie": "ONZIN", "beschrijving": "?"}')[0] == vision.UNKNOWN


async def test_vision_asks_for_json(ctx):
    seen = {}

    async def describe(image, question, priority=1, schema=None):
        seen["schema"] = schema
        return '{"categorie": "NIETS", "beschrijving": "Lege oprit"}'

    ctx.llm.describe_image = describe
    assert await vision.classify(ctx.llm, b"jpeg", "Oprit") == (vision.NOTHING, "Lege oprit")
    assert seen["schema"]["required"] == ["categorie", "beschrijving"]


def test_area_from_satellite_in_prompt(ctx):
    agent = Agent(ctx, ToolRegistry([]))
    assert agent.known_area("keuken") == "Keuken" and agent.known_area("zolder") is None
    assert "ruimte 'Keuken'" in agent.system_prompt("assist", "Keuken")


async def test_streaming_answer_only_after_checking(ctx):
    ctx.llm.responses = [
        {"role": "assistant", "content": "Uit mijn hoofd: aan."},  # sent back by check_first, never streamed
        {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "get_state", "arguments": {"entity_id": "light.keuken"}}}]},
        {"role": "assistant", "content": "De keukenlamp is aan."},
    ]
    from app.tools.home import build_registry

    pieces = []

    async def on_delta(piece):
        pieces.append(piece)

    result = await Agent(ctx, build_registry()).run("Is de keukenlamp aan?", "c1", source="assist",
                                                     on_delta=on_delta)
    assert result.answer == "De keukenlamp is aan."
    assert "".join(pieces).strip() == "De keukenlamp is aan."
    assert [r["streamed"] for r in ctx.llm.requests] == [False, True, True]


def test_calendar_sets_guest_and_vacation_mode(index, config):
    now = datetime(2026, 10, 19, 14, 0)
    assert house_mode(index, config, now) == MODE_HOME
    index.entities["calendar.gezin"] = EntityInfo("calendar.gezin", "Gezin", "calendar", "on",
                                                  {"message": "Gasten uit Utrecht"})
    assert house_mode(index, config, now) == MODE_GUEST
    config.calendar_context = False
    assert house_mode(index, config, now) == MODE_HOME
    config.calendar_context = True
    index.entities["calendar.gezin"].attributes["message"] = "Vakantie Frankrijk"
    for ent in index.entities.values():
        if ent.domain == "person":
            ent.state = "not_home"
            ent.last_changed = datetime.now(timezone.utc).isoformat()
    if any(e.domain == "person" for e in index.entities.values()):
        assert house_mode(index, config, now) == MODE_VACATION


async def test_agenda_tool(ctx):
    assert "error" in await get_agenda(ctx)
    ctx.index.entities["calendar.gezin"] = EntityInfo("calendar.gezin", "Gezin", "calendar", "off")

    async def calendar_events(entity_id, start, end):
        return [{"summary": "Tandarts", "start": {"dateTime": "2026-10-19T15:30:00+02:00"}},
                {"summary": "Vuilnis", "start": {"date": "2026-10-19"}}]

    ctx.ha.calendar_events = calendar_events
    ctx.timezone = "Europe/Amsterdam"
    events = (await get_agenda(ctx))["events"]
    assert events == ["19-10 Vuilnis (Gezin)", "19-10 15:30 Tandarts (Gezin)"]


def test_cheapest_window_and_energy_info(ctx):
    ctx.timezone = "UTC"
    start = datetime(2026, 10, 19, 0, 0, tzinfo=timezone.utc)
    prices = [0.30, 0.28, 0.25, 0.10, 0.08, 0.09, 0.27, 0.31]
    points = [(start + timedelta(hours=i), p) for i, p in enumerate(prices)]
    window = cheapest_window(points, 3, start + timedelta(minutes=30))
    assert window["start"] == start + timedelta(hours=3) and window["avg_price"] == 0.09
    assert cheapest_window(points, 3, start + timedelta(hours=7)) is None

    ctx.index.entities["sensor.stroomprijs"] = EntityInfo(
        "sensor.stroomprijs", "Stroomprijs", "sensor", "0.30", unit="EUR/kWh",
        attributes={"raw_today": [{"start": t.isoformat(), "end": (t + timedelta(hours=1)).isoformat(),
                                   "value": p} for t, p in points]})
    ctx.index.entities["sensor.energy_production_today"] = EntityInfo(
        "sensor.energy_production_today", "Estimated energy production - today", "sensor", "12.4",
        device_class="energy", unit="kWh")
    info = energy_info(ctx, 3, now=start + timedelta(minutes=30))
    assert info["price_now"].startswith("0.30 EUR/kWh")
    assert info["cheapest_3h"].startswith("19-10 03:00-06:00")
    assert info["solar_forecast"] == ["Estimated energy production - today: 12.4 kWh"]


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    ha_server = TestServer(fake_home_assistant([]))
    ollama_server = TestServer(fake_ollama([], [{"role": "assistant", "content": "De keukenlamp is aan."}]))
    await ha_server.start_server()
    await ollama_server.start_server()
    config = Config(ha_url=str(ha_server.make_url("")).rstrip("/"), ha_token="secret",
                    ollama_url=str(ollama_server.make_url("")).rstrip("/"), data_dir=str(tmp_path))
    client = TestClient(TestServer(create_app(config)))
    await client.start_server()
    state = client.app["state"]
    await state.ha.wait_connected(5)
    for _ in range(50):
        if len(state.index.entities) >= len(STATES):
            break
        await asyncio.sleep(0.05)
    yield client
    await client.close()
    await ha_server.close()
    await ollama_server.close()


async def test_health_reports_problems(client):
    state = client.app["state"]
    resp = await client.get("/api/health")
    assert resp.status == 200 and (await resp.json())["ok"]
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert "frame-ancestors 'self'" in resp.headers["Content-Security-Policy"]
    state.scheduler.loop_tick -= 3600
    resp = await client.get("/api/health")
    assert resp.status == 503 and (await resp.json())["problems"] == ["scheduler_stalled"]
    state.scheduler.loop_tick = time.monotonic()
    state.ha.disconnected_since = time.monotonic() - 600
    assert (await (await client.get("/api/health")).json())["problems"] == ["home_assistant_disconnected"]


async def test_conversation_stream(client):
    resp = await client.post("/api/conversation/stream", json={"text": "Is de keukenlamp aan?", "area": "keuken"})
    assert resp.headers["Content-Type"].startswith("application/x-ndjson")
    lines = [json.loads(line) for line in (await resp.text()).splitlines()]
    assert lines[-1]["done"] and lines[-1]["speech"] == "De keukenlamp is aan." and lines[-1]["streamed"]
    assert "".join(line.get("delta", "") for line in lines).strip() == "De keukenlamp is aan."


async def test_integration_state_replaces_legacy_entities(client):
    state = client.app["state"]
    calls = []

    async def delete_state(entity_id):
        calls.append(entity_id)

    state.ha.delete_state = delete_state
    data = await (await client.get("/api/integration/state")).json()
    assert data["status"] == "rustig" and data["health_problems"] == [] and "llm_avg_seconds" in data
    await state.publisher.publish("thuis")
    assert "sensor.ai_huis_status" in calls
    perf = await (await client.get("/api/llm/performance")).json()
    assert "models" in perf


async def test_failed_api_keys_are_rate_limited(tmp_path, monkeypatch):
    monkeypatch.setenv("SUPERVISOR_TOKEN", "x")  # supervised: only ingress or the API key gets in
    config = Config(ha_url="http://127.0.0.1:9", ha_token="t", data_dir=str(tmp_path), api_key="goed")
    client = TestClient(TestServer(create_app(config)))
    await client.start_server()
    try:
        for _ in range(AUTH_FAILURE_LIMIT):
            assert (await client.get("/api/status", headers={"Authorization": "Bearer fout"})).status == 401
        assert (await client.get("/api/status", headers={"Authorization": "Bearer fout"})).status == 429
        assert (await client.get("/api/health")).status in (200, 503)  # never needs a key
    finally:
        await client.close()
    assert (tmp_path / "api_key").exists() is False  # configured key: nothing written


def test_generated_api_key_is_private(tmp_path):
    from app.main import load_api_key

    load_api_key(Config(data_dir=str(tmp_path)))
    assert (tmp_path / "api_key").stat().st_mode & 0o077 == 0


def test_sqlite_backup_is_consistent(tmp_path):
    Database(str(tmp_path)).add_rule("x")
    copy = sqlite3.connect(write_backup(str(tmp_path)))
    assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
