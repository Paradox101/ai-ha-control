import asyncio
import json
import re
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from app import settings as settings_module
from app.config import Config
from app.main import create_app

from .conftest import STATES
from .test_e2e import fake_home_assistant, fake_ollama


def test_validate_coerces_and_reports_per_field():
    config = Config()
    clean = settings_module.validate(config, {
        "dry_run": False, "heartbeat_minutes": "5", "max_climate_change": 1.5, "quiet_hours": " 22:00 - 06:30 ",
        "daily_summary_time": "", "autonomy": ["light=act", "lock=observe"], "unknown_key": 1,
    })
    assert clean == {"dry_run": False, "heartbeat_minutes": 5, "max_climate_change": 1.5, "quiet_hours": "22:00-06:30",
                     "daily_summary_time": "", "autonomy": ["light=act", "lock=observe"]}
    with pytest.raises(settings_module.SettingsError) as err:
        settings_module.validate(config, {"heartbeat_minutes": 0, "ollama_url": "ftp://x", "sleep_hours": "laat",
                                          "autonomy": ["light=always"], "camera_patrol": "soms", "model": ""})
    assert set(err.value.errors) == {"heartbeat_minutes", "ollama_url", "sleep_hours", "autonomy", "camera_patrol", "model"}


def test_every_field_maps_to_an_option():
    keys = set(settings_module.option_keys())
    assert set(settings_module.FIELD_INDEX) <= keys
    assert "api_key" not in settings_module.FIELD_INDEX and "ha_token" not in keys


def test_every_field_type_has_a_ui_builder():
    html = (Path(settings_module.__file__).parent / "web" / "static" / "index.html").read_text()
    block = html[html.index("const builders = {"):html.index("function renderSettings")]
    builders = set(re.findall(r"^    (\w+)\(spec, v, set", block, re.M))
    types = {f["type"] for f in settings_module.FIELD_INDEX.values()}
    assert types <= builders, f"geen UI-builder voor: {sorted(types - builders)}"


async def test_persist_without_supervisor_writes_options(tmp_path, monkeypatch):
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    monkeypatch.setenv("OPTIONS_FILE", str(tmp_path / "options.json"))
    config = Config(data_dir=str(tmp_path))
    settings_module.apply(config, {"dry_run": False, "heartbeat_minutes": 7})
    assert await settings_module.persist(None, config) is None
    saved = json.loads((tmp_path / "options.json").read_text())
    assert saved["dry_run"] is False and saved["heartbeat_minutes"] == 7


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    monkeypatch.setenv("OPTIONS_FILE", str(tmp_path / "options.json"))
    ha_server = TestServer(fake_home_assistant([]))
    ollama_server = TestServer(fake_ollama([], [{"role": "assistant", "content": "Alles rustig"}]))
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


async def test_settings_api_roundtrip(client):
    data = await (await client.get("/api/settings")).json()
    assert data["values"]["dry_run"] is True
    assert any(s["id"] == "autonomy" for s in data["sections"])
    assert data["choices"]["models"][0]["name"] == "qwen3:8b"
    assert "notify.mobile_app_telefoon" in [c["value"] for c in data["choices"]["notify"]]

    bad = await client.post("/api/settings", json={"values": {"heartbeat_minutes": 0}})
    assert bad.status == 400 and "heartbeat_minutes" in (await bad.json())["fields"]

    ok = await client.post("/api/settings", json={"values": {"dry_run": False, "camera_patrol": "always"}})
    body = await ok.json()
    assert ok.status == 200 and body["warning"] is None
    assert body["values"]["dry_run"] is False
    assert client.app["state"].config.camera_patrol == "always"  # applied immediately

    bad = await client.post("/api/settings", json={"values": {"camera_profiles": ["camera.x=sometimes"]}})
    assert bad.status == 400
    ok = await client.post("/api/settings", json={"values": {"camera_profiles": ["camera.tuin=private", "camera.y=auto"]}})
    assert (await ok.json())["values"]["camera_profiles"] == ["camera.tuin=private"]

    test = await (await client.post("/api/settings/test-ollama", json={"url": "http://127.0.0.1:1"})).json()
    assert test["reachable"] is False


async def test_agent_incident_and_rule_endpoints(client):
    state = client.app["state"]
    agent = await (await client.get("/api/agent")).json()
    assert agent["enabled"] is True and agent["mode"] == "thuis"

    run = await (await client.post("/api/agent/run")).json()
    assert run["summary"] == "Alles rustig"

    incident_id = await state.incidents.report("presence", "Detectie", "Persoon bij de poort")
    active = await (await client.get("/api/incidents?status=active")).json()
    assert active[0]["id"] == incident_id
    detail = await (await client.get(f"/api/incidents/{incident_id}")).json()
    assert detail["events"][0]["text"] == "Persoon bij de poort"
    assert (await client.post(f"/api/incidents/{incident_id}/resolve")).status == 200
    assert await (await client.get("/api/incidents?status=active")).json() == []

    rules = await (await client.post("/api/rules", json={"text": "Waarschuw bij vorst"})).json()
    rules = await (await client.post(f"/api/rules/{rules[0]['id']}/toggle", json={"enabled": False})).json()
    assert rules[0]["enabled"] == 0

    assert (await client.get("/api/snapshots/..%2F..%2Fetc%2Fpasswd")).status == 404
    status = await (await client.get("/api/status")).json()
    assert status["mode"] == "thuis" and "active_incidents" in status
