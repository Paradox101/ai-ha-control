"""Entry point: web server (Ingress UI + API) and Home Assistant connection."""

from __future__ import annotations

import ipaddress
import logging
import os
import uuid
from dataclasses import replace
from pathlib import Path

import aiohttp
from aiohttp import web

from .agent.loop import Agent
from .config import Config
from .ha.client import HAClient
from .ha.index import EntityIndex
from .llm.ollama import LLMError, OllamaClient
from .policy import Policy
from .storage.db import Database
from .tools.actions import ConfirmationStore, execute
from .tools.base import ToolContext
from .tools.home import build_registry

_LOGGER = logging.getLogger("ai_ha_control")
STATIC_DIR = Path(__file__).parent / "web" / "static"
INGRESS_PROXY = ipaddress.ip_address("172.30.32.2")


class App:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.index = EntityIndex()
        self.db = Database(config.data_dir)
        self.confirmations = ConfirmationStore()
        self.registry = build_registry()
        self.session: aiohttp.ClientSession | None = None
        self.ha: HAClient | None = None
        self.llm: OllamaClient | None = None
        self.ctx: ToolContext | None = None
        self.agent: Agent | None = None

    async def startup(self, _app: web.Application) -> None:
        self.session = aiohttp.ClientSession()
        self.ha = HAClient(self.config, self.session)
        self.llm = OllamaClient(self.config, self.session)
        self.ctx = ToolContext(
            config=self.config,
            ha=self.ha,
            index=self.index,
            llm=self.llm,
            policy=Policy(self.config),
            db=self.db,
            confirmations=self.confirmations,
        )
        self.agent = Agent(self.ctx, self.registry)
        self.ha.on_connect(self.refresh_index)
        self.ha.on_state_changed(self.index.handle_state_changed)
        await self.ha.start()

    async def shutdown(self, _app: web.Application) -> None:
        if self.ha:
            await self.ha.stop()
        if self.session:
            await self.session.close()
        self.db.close()

    async def refresh_index(self) -> None:
        assert self.ha and self.ctx
        ha_config = await self.ha.get_config()
        self.ctx.timezone = ha_config.get("time_zone")
        self.index.location_name = ha_config.get("location_name")
        allowed = await self.ha.exposed_entities() if self.config.only_exposed_entities else None
        self.index.load(
            states=await self.ha.get_states(),
            areas=await self.ha.area_registry(),
            devices=await self.ha.device_registry(),
            entities=await self.ha.entity_registry(),
            services=await self.ha.get_services(),
            allowed=allowed,
        )

    # --- HTTP handlers ---------------------------------------------------------

    async def index_page(self, _request: web.Request) -> web.FileResponse:
        return web.FileResponse(STATIC_DIR / "index.html")

    async def status(self, _request: web.Request) -> web.Response:
        assert self.ha and self.llm
        return web.json_response(
            {
                "home_assistant": {
                    "connected": self.ha.connected,
                    "version": self.ha.ha_version,
                    "entities": len(self.index.entities),
                    "areas": len(self.index.areas),
                },
                "ollama": await self.llm.status(),
                "dry_run": self.config.dry_run,
                "language": self.config.language,
            }
        )

    async def chat(self, request: web.Request) -> web.Response:
        assert self.agent
        body = await request.json()
        message = (body.get("message") or "").strip()
        if not message:
            return web.json_response({"error": "empty message"}, status=400)
        conversation_id = body.get("conversation_id") or uuid.uuid4().hex
        try:
            result = await self.agent.run(message, conversation_id)
        except LLMError as err:
            return web.json_response({"error": str(err)}, status=502)
        return web.json_response(
            {
                "conversation_id": conversation_id,
                "answer": result.answer,
                "actions": result.actions,
                "pending": result.pending,
            }
        )

    async def pending(self, _request: web.Request) -> web.Response:
        return web.json_response([p.as_dict() for p in self.confirmations.list()])

    async def confirm(self, request: web.Request) -> web.Response:
        assert self.ctx
        action = self.confirmations.pop(request.match_info["action_id"])
        if action is None:
            return web.json_response({"error": "onbekende of verlopen bevestiging"}, status=404)
        if request.match_info["decision"] == "reject":
            self.db.audit(
                "call_service",
                {"domain": action.domain, "service": action.service, "entity_ids": action.entity_ids,
                 "data": action.data},
                "rejected",
                None,
                action.conversation_id,
            )
            return web.json_response({"status": "rejected"})
        ctx = replace(self.ctx, conversation_id=action.conversation_id)
        result = await execute(ctx, action.domain, action.service, action.entity_ids, action.data, confirmed=True)
        return web.json_response(result)

    async def audit(self, request: web.Request) -> web.Response:
        limit = min(int(request.query.get("limit", 100)), 500)
        return web.json_response(self.db.audit_log(limit))

    async def entities(self, request: web.Request) -> web.Response:
        results = self.index.search(
            request.query.get("q", ""),
            area=request.query.get("area"),
            domain=request.query.get("domain"),
            limit=min(int(request.query.get("limit", 50)), 500),
        )
        return web.json_response([e.compact(with_attributes=True) for e in results])


@web.middleware
async def ingress_only(request: web.Request, handler):
    """Under the Supervisor, only accept requests that come through Ingress."""
    remote = request.remote or ""
    try:
        addr = ipaddress.ip_address(remote)
    except ValueError:
        raise web.HTTPForbidden()
    if addr != INGRESS_PROXY and not addr.is_loopback:
        raise web.HTTPForbidden()
    return await handler(request)


def create_app(config: Config) -> web.Application:
    state = App(config)
    middlewares = [ingress_only] if os.environ.get("SUPERVISOR_TOKEN") else []
    app = web.Application(middlewares=middlewares)
    app["state"] = state
    app.on_startup.append(state.startup)
    app.on_cleanup.append(state.shutdown)
    app.add_routes(
        [
            web.get("/", state.index_page),
            web.get("/api/status", state.status),
            web.post("/api/chat", state.chat),
            web.get("/api/pending", state.pending),
            web.post(r"/api/pending/{action_id}/{decision:confirm|reject}", state.confirm),
            web.get("/api/audit", state.audit),
            web.get("/api/entities", state.entities),
        ]
    )
    return app


def main() -> None:
    config = Config.load()
    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not config.ha_token:
        _LOGGER.error("No Home Assistant token (SUPERVISOR_TOKEN / HA_TOKEN) available")
    _LOGGER.info(
        "Starting AI HA Control: model=%s vision=%s ollama=%s dry_run=%s",
        config.model,
        config.vision_model,
        config.ollama_url,
        config.dry_run,
    )
    web.run_app(create_app(config), host="0.0.0.0", port=config.port, access_log=None)


if __name__ == "__main__":
    main()
