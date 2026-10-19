"""End-to-end: real web app + HA client against a fake HA websocket and a fake Ollama."""

import asyncio

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
            if data["type"] == "call_service":
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


def fake_ollama(requests: list) -> web.Application:
    replies = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "control_light", "arguments": {"entity_ids": ["light.keuken"], "action": "off"}}}]},
        {"role": "assistant", "content": "De keukenlamp is uit."},
    ]

    async def chat(request):
        requests.append(await request.json())
        return web.json_response({"message": replies.pop(0)})

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
        assert llm_requests[0]["options"]["num_ctx"] == config.context_size
        assert "Thuis" in llm_requests[0]["messages"][0]["content"]

        audit = await (await client.get("/api/audit")).json()
        assert audit[0]["decision"] == "executed"
        assert (await client.get("/")).status == 200
    finally:
        await client.close()
        await ha_server.close()
        await ollama_server.close()
