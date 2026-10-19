"""The add-on and the integration against a real Home Assistant (started by CI in a container).

Needs Home Assistant at REAL_HA_URL (fresh, not onboarded) with custom_components/ mounted, and
Home Assistant must reach the add-on at ADDON_URL (both on the host network in CI).
"""

import asyncio
import json
import os

import aiohttp
import pytest
from aiohttp import web

from app.config import Config
from app.main import create_app
from tests.test_e2e import fake_ollama

HA_URL = os.environ.get("REAL_HA_URL", "")
ADDON_PORT = int(os.environ.get("ADDON_PORT", "8099"))
ADDON_URL = os.environ.get("ADDON_URL", f"http://127.0.0.1:{ADDON_PORT}")
OLLAMA_PORT = int(os.environ.get("FAKE_OLLAMA_PORT", "11435"))
CLIENT_ID = f"{HA_URL}/"
ANSWER = "Alles is rustig thuis."

pytestmark = pytest.mark.skipif(not HA_URL, reason="REAL_HA_URL not set")


async def onboard(session: aiohttp.ClientSession) -> str:
    """Create the owner account and return an access token."""
    async with session.post(f"{HA_URL}/api/onboarding/users", json={
        "client_id": CLIENT_ID, "name": "Test", "username": "test", "password": "test-wachtwoord-123",
        "language": "nl"}) as resp:
        assert resp.status == 200, await resp.text()
        code = (await resp.json())["auth_code"]
    async with session.post(f"{HA_URL}/auth/token", data={
        "grant_type": "authorization_code", "code": code, "client_id": CLIENT_ID}) as resp:
        return (await resp.json())["access_token"]


async def wait_for(check, timeout: float = 60, what: str = ""):
    for _ in range(int(timeout * 4)):
        result = await check()
        if result:
            return result
        await asyncio.sleep(0.25)
    raise AssertionError(f"timed out waiting for {what}")


async def test_addon_and_integration_with_real_home_assistant(tmp_path, monkeypatch):
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    async with aiohttp.ClientSession() as session:
        token = await onboard(session)
        headers = {"Authorization": f"Bearer {token}"}

        ollama = web.AppRunner(fake_ollama([], [{"role": "assistant", "content": ANSWER}]))
        await ollama.setup()
        await web.TCPSite(ollama, "127.0.0.1", OLLAMA_PORT).start()
        config = Config(ha_url=HA_URL, ha_token=token, ollama_url=f"http://127.0.0.1:{OLLAMA_PORT}",
                        data_dir=str(tmp_path), port=ADDON_PORT, api_key="ci-sleutel")
        app = create_app(config)
        addon = web.AppRunner(app)
        await addon.setup()
        await web.TCPSite(addon, "0.0.0.0", ADDON_PORT).start()
        state = app["state"]
        try:
            await state.ha.wait_connected(30)
            await wait_for(lambda: asyncio.sleep(0, bool(state.index.entities)), what="entity index")
            async with session.get(f"{ADDON_URL}/api/health") as resp:
                assert resp.status == 200, await resp.text()

            # The kill switch is created in the real Home Assistant.
            async def kill_switch():
                async with session.get(f"{HA_URL}/api/states/input_boolean.ai_autonoom", headers=headers) as r:
                    return r.status == 200 and (await r.json())["state"] == "on"
            await wait_for(kill_switch, what="kill switch")

            # "How often was someone at the front door?" is counted from the real recorder history.
            for value, wait in (("off", 1.5), ("on", 2), ("off", 65), ("on", 2), ("off", 1.5)):
                async with session.post(f"{HA_URL}/api/states/binary_sensor.voordeur_persoon", headers=headers,
                                        json={"state": value, "attributes": {"friendly_name": "Voordeur Persoon"}}):
                    pass
                await asyncio.sleep(wait)  # two visits, more than a minute apart

            async def counted():
                result = json.loads(await state.registry.run(state.ctx, "get_detections",
                                                             {"what": "persoon", "where": "voordeur", "hours": 1}))
                return result if result.get("total") == 2 else None
            detections = await wait_for(counted, timeout=30, what="detections in the recorder history")
            assert detections["places"][0]["sensors"] == {"binary_sensor.voordeur_persoon": 2}

            # Set up the integration through the real config flow.
            async with session.post(f"{HA_URL}/api/config/config_entries/flow", headers=headers,
                                    json={"handler": "ai_ha_control"}) as resp:
                flow = await resp.json()
                assert resp.status == 200, flow
            async with session.post(f"{HA_URL}/api/config/config_entries/flow/{flow['flow_id']}", headers=headers,
                                    json={"url": ADDON_URL, "api_key": "ci-sleutel"}) as resp:
                result = await resp.json()
                assert result.get("type") == "create_entry", result

            async def entities():
                async with session.get(f"{HA_URL}/api/states", headers=headers) as r:
                    return {s["entity_id"]: s for s in await r.json() if "ai_ha_control" in s["entity_id"]}
            found = await wait_for(lambda: entities_with(entities), what="integration entities")
            status = next(s for e, s in found.items() if e.startswith("sensor.") and s["state"] == "rustig")
            assert status
            switch = next(e for e in found if e.startswith("switch."))
            assert found[switch]["state"] == "on"
            conversation = next(e for e in found if e.startswith("conversation."))

            # Assist through Home Assistant: streamed from the add-on into the chat log.
            async with session.post(f"{HA_URL}/api/conversation/process", headers=headers, json={
                    "text": "Hoe gaat het thuis?", "language": "nl", "agent_id": conversation}) as resp:
                body = await resp.json()
            assert body["response"]["speech"]["plain"]["speech"].strip() == ANSWER, body

            # The switch in Home Assistant turns the add-on's kill switch off.
            async with session.post(f"{HA_URL}/api/services/switch/turn_off", headers=headers,
                                    json={"entity_id": switch}) as resp:
                assert resp.status == 200
            async def switched_off():
                async with session.get(f"{HA_URL}/api/states/input_boolean.ai_autonoom", headers=headers) as r:
                    return (await r.json())["state"] == "off"
            await wait_for(switched_off, what="kill switch off")

            # With the integration active the old state-only sensors are removed.
            await state.publisher.publish("thuis")
            async with session.get(f"{HA_URL}/api/states/sensor.ai_huis_status", headers=headers) as resp:
                assert resp.status == 404
        finally:
            await addon.cleanup()
            await ollama.cleanup()


async def entities_with(fetch):
    found = await fetch()
    kinds = {e.split(".")[0] for e in found}
    return found if {"sensor", "binary_sensor", "switch", "conversation"} <= kinds else None
