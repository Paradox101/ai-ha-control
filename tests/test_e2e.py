"""End-to-end: real web app + HA client against a fake HA websocket and a fake Ollama."""

import asyncio
import io
import zipfile
import json
import time

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app.config import Config
from app.main import create_app

from .conftest import AREAS, DEVICES, ENTITIES, SERVICES, STATES


def fake_home_assistant(service_calls: list) -> web.Application:
    async def websocket(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({"type": "auth_required"})
        auth = await ws.receive_json()
        if auth.get("access_token") != "secret":
            await ws.send_json({"type": "auth_invalid", "message": "bad token"})
            return ws
        await ws.send_json({"type": "auth_ok", "ha_version": "2026.10.0"})
        results = {
            "get_states": STATES,
            "get_config": {"time_zone": "Europe/Amsterdam", "location_name": "Thuis"},
            "config/area_registry/list": AREAS,
            "config/device_registry/list": DEVICES,
            "config/entity_registry/list": ENTITIES,
            "get_services": SERVICES,
            "subscribe_events": None,
        }
        async for msg in ws:
            data = msg.json()
            if data["type"] == "config/entity_registry/get_entries":
                await ws.send_json({"id": data["id"], "type": "result", "success": True, "result": {
                    "light.keuken": {"aliases": ["kooklamp"]}}})
            elif data["type"] == "call_service":
                service_calls.append(data)
                await ws.send_json({"id": data["id"], "type": "result", "success": True, "result": {}})
                await ws.send_json({"id": 1, "type": "event", "event": {"event_type": "state_changed", "data": {
                    "entity_id": "light.keuken",
                    "new_state": {"entity_id": "light.keuken", "state": "off",
                                  "attributes": {"friendly_name": "Keukenlamp"}}}}})
            elif data["type"] in results:
                await ws.send_json({"id": data["id"], "type": "result", "success": True,
                                    "result": results[data["type"]]})
            else:
                await ws.send_json({"id": data["id"], "type": "result", "success": False,
                                    "error": {"code": "unknown_command", "message": data["type"]}})
        return ws

    app = web.Application()
    app.router.add_get("/api/websocket", websocket)
    return app


def fake_ollama(requests: list, replies: list | None = None) -> web.Application:
    replies = replies if replies is not None else [
        {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "control_light", "arguments": {"entity_ids": ["light.keuken"], "action": "off"}}}]},
        {"role": "assistant", "content": "De keukenlamp is uit."},
    ]

    async def chat(request):
        body = await request.json()
        requests.append(body)
        reply = replies.pop(0) if len(replies) > 1 else replies[0]  # the last reply repeats
        if not body.get("stream"):
            return web.json_response({"message": reply, "eval_count": 5, "prompt_eval_count": 50})
        resp = web.StreamResponse()
        await resp.prepare(request)
        if reply.get("tool_calls"):
            await resp.write(json.dumps({"message": reply, "done": False}).encode() + b"\n")
        else:
            for word in (reply.get("content") or "").split(" "):
                await resp.write(json.dumps({"message": {"role": "assistant", "content": word + " "},
                                             "done": False}).encode() + b"\n")
        await resp.write(json.dumps({"message": {"role": "assistant", "content": ""}, "done": True,
                                     "eval_count": 5, "prompt_eval_count": 50}).encode() + b"\n")
        return resp

    async def tags(_request):
        return web.json_response({"models": [{"name": "qwen3:8b"}]})

    app = web.Application()
    app.router.add_post("/api/chat", chat)
    app.router.add_get("/api/tags", tags)
    return app


async def test_end_to_end(tmp_path):
    service_calls, llm_requests = [], []
    ha_server = TestServer(fake_home_assistant(service_calls))
    ollama_server = TestServer(fake_ollama(llm_requests))
    await ha_server.start_server()
    await ollama_server.start_server()

    config = Config(
        ha_url=str(ha_server.make_url("")).rstrip("/"),
        ha_token="secret",
        ollama_url=str(ollama_server.make_url("")).rstrip("/"),
        data_dir=str(tmp_path),
        dry_run=False,
    )
    client = TestClient(TestServer(create_app(config)))
    await client.start_server()
    try:
        state = client.app["state"]
        await state.ha.wait_connected(5)
        for _ in range(50):
            if state.index.entities:
                break
            await asyncio.sleep(0.05)

        status = await (await client.get("/api/status")).json()
        assert status["home_assistant"]["connected"]
        assert status["home_assistant"]["entities"] == len(STATES)
        assert status["ollama"]["model_installed"]

        resp = await client.post("/api/chat", json={"message": "Doe de keukenlamp uit"})
        body = await resp.json()
        assert resp.status == 200, body
        assert body["answer"] == "De keukenlamp is uit."
        assert service_calls[0]["domain"] == "light" and service_calls[0]["service"] == "turn_off"
        assert service_calls[0]["target"] == {"entity_id": ["light.keuken"]}
        assert state.index.get("light.keuken").state == "off"
        assert state.index.search("kooklamp")[0].entity_id == "light.keuken"  # alias from extended registry
        assert (await client.get("/api/audit?limit=abc")).status == 400
        assert (await client.post("/api/chat", data="geen json")).status == 400
        assert llm_requests[0]["options"]["num_ctx"] == config.context_size
        assert "Thuis" in llm_requests[0]["messages"][0]["content"]

        assert (await client.get("/api/health")).status == 200
        assert (await client.get("/api/camera/light.keuken")).status == 404
        audit = await (await client.get("/api/audit")).json()
        assert audit[0]["decision"] == "executed"
        assert (await client.get("/")).status == 200

        export = await client.get("/api/export/entities.xlsx")
        assert export.status == 200 and "attachment" in export.headers["Content-Disposition"]
        with zipfile.ZipFile(io.BytesIO(await export.read())) as xlsx:
            sheet = xlsx.read("xl/worksheets/sheet1.xml").decode()
        assert "light.keuken" in sheet and "Keukenlamp" in sheet
    finally:
        await client.close()
        await ha_server.close()
        await ollama_server.close()


async def test_assist_endpoint_requires_api_key(tmp_path, monkeypatch):
    monkeypatch.setenv("SUPERVISOR_TOKEN", "x")
    monkeypatch.delenv("HOSTNAME", raising=False)
    service_calls, llm_requests = [], []
    ha_server = TestServer(fake_home_assistant(service_calls))
    ollama_server = TestServer(fake_ollama(llm_requests, [{"role": "assistant", "content": "Het is 21,4 °C."}]))
    await ha_server.start_server()
    await ollama_server.start_server()
    config = Config(ha_url=str(ha_server.make_url("")).rstrip("/"), ha_token="secret",
                    ollama_url=str(ollama_server.make_url("")).rstrip("/"), data_dir=str(tmp_path),
                    api_key="sleutel123")
    client = TestClient(TestServer(create_app(config)))
    await client.start_server()
    try:
        await client.app["state"].ha.wait_connected(5)
        assert (await client.post("/api/conversation", json={"text": "Hoe warm is het?"})).status == 401
        assert (await client.get("/api/status", headers={"Authorization": "Bearer fout"})).status == 401
        resp = await client.post("/api/conversation", json={"text": "Hoe warm is het?", "conversation_id": "c1"},
                                 headers={"Authorization": "Bearer sleutel123"})
        assert resp.status == 200
        assert await resp.json() == {"conversation_id": "c1", "speech": "Het is 21,4 °C."}
        assert "uitgesproken" in llm_requests[0]["messages"][0]["content"]  # voice-friendly prompt
    finally:
        await client.close()
        await ha_server.close()
        await ollama_server.close()


async def test_event_callback_can_call_home_assistant(tmp_path):
    """A callback that sends a command must not block the websocket reader (deadlock regression)."""
    from app.ha.client import HAClient

    service_calls = []
    ha_server = TestServer(fake_home_assistant(service_calls))
    await ha_server.start_server()
    config = Config(ha_url=str(ha_server.make_url("")).rstrip("/"), ha_token="secret", data_dir=str(tmp_path))
    import aiohttp
    async with aiohttp.ClientSession() as session:
        client = HAClient(config, session)
        done = asyncio.Event()

        async def on_change(data):
            await client.get_config()  # needs the reader to receive the reply
            done.set()

        client.on_state_changed(on_change)
        await client.start()
        try:
            await client.wait_connected(5)
            start = time.monotonic()
            await client.call_service("light", "turn_off", target={"entity_id": ["light.keuken"]})
            await asyncio.wait_for(done.wait(), 5)
            assert time.monotonic() - start < 2
        finally:
            await client.stop()
    await ha_server.close()


async def test_registry_update_triggers_refresh(tmp_path, monkeypatch):
    import app.main as main_module
    monkeypatch.setattr(main_module, "REFRESH_DELAY", 0)
    service_calls, llm_requests = [], []
    ha_server = TestServer(fake_home_assistant(service_calls))
    await ha_server.start_server()
    config = Config(ha_url=str(ha_server.make_url("")).rstrip("/"), ha_token="secret", data_dir=str(tmp_path))
    client = TestClient(TestServer(create_app(config)))
    await client.start_server()
    try:
        state = client.app["state"]
        await state.ha.wait_connected(5)
        for _ in range(50):
            if state.index.entities:
                break
            await asyncio.sleep(0.05)
        state.index.entities.clear()
        state.schedule_refresh({})
        await state._refresh_task
        assert len(state.index.entities) == len(STATES)
    finally:
        await client.close()
        await ha_server.close()
