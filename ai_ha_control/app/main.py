"""Entry point: web server (Ingress UI + API) and Home Assistant connection."""

from __future__ import annotations

import hmac
import ipaddress
import logging
import os
import secrets
import uuid
from pathlib import Path

import aiohttp
from aiohttp import web

from .agent.loop import Agent
from .config import Config
from .ha.client import HAClient
from .ha.index import EntityIndex
from .llm.ollama import LLMError, OllamaClient
from .notify import Notifier
from .policy import Policy
from .storage.db import Database
from .tools.actions import ConfirmationStore, resolve_confirmation
from .tools.base import ToolContext
from .tools.home import build_registry
from .watcher.routines import accept_suggestion
from .watcher.rules import Watcher
from .watcher.scheduler import Scheduler

_LOGGER = logging.getLogger("ai_ha_control")
STATIC_DIR = Path(__file__).parent / "web" / "static"
INGRESS_PROXY = ipaddress.ip_address("172.30.32.2")
DISCOVERY_SERVICE = "ai_ha_control"


def load_api_key(config: Config) -> str:
    if config.api_key:
        return config.api_key
    path = Path(config.data_dir) / "api_key"
    if path.exists():
        return path.read_text().strip()
    key = secrets.token_urlsafe(24)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(key)
    return key


class App:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.api_key = load_api_key(config)
        self.index = EntityIndex()
        self.db = Database(config.data_dir)
        self.confirmations = ConfirmationStore()
        self.registry = build_registry()
        self.session: aiohttp.ClientSession | None = None
        self.ha: HAClient | None = None
        self.llm: OllamaClient | None = None
        self.ctx: ToolContext | None = None
        self.agent: Agent | None = None
        self.notifier: Notifier | None = None
        self.watcher: Watcher | None = None
        self.scheduler: Scheduler | None = None

    async def startup(self, _app: web.Application) -> None:
        self.session = aiohttp.ClientSession()
        self.ha = HAClient(self.config, self.session)
        self.llm = OllamaClient(self.config, self.session)
        self.notifier = Notifier(self.config, self.ha, self.db)
        self.ctx = ToolContext(
            config=self.config,
            ha=self.ha,
            index=self.index,
            llm=self.llm,
            policy=Policy(self.config),
            db=self.db,
            confirmations=self.confirmations,
            notifier=self.notifier,
        )
        self.agent = Agent(self.ctx, self.registry)
        self.watcher = Watcher(self.ctx, self.notifier)
        self.scheduler = Scheduler(self.ctx, self.notifier, self.watcher)
        self.ha.on_connect(self.refresh_index)
        # Order matters: the index must be updated before the watcher looks at it.
        self.ha.on_state_changed(self.index.handle_state_changed)
        self.ha.on_state_changed(self.watcher.handle_state_changed)
        self.ha.on_event("mobile_app_notification_action", self.watcher.handle_notification_action)
        await self.ha.start()
        self.scheduler.start()
        await self.announce_discovery()

    async def shutdown(self, _app: web.Application) -> None:
        if self.scheduler:
            self.scheduler.stop()
        if self.ha:
            await self.ha.stop()
        if self.session:
            await self.session.close()
        self.db.close()

    async def refresh_index(self) -> None:
        assert self.ha and self.ctx and self.notifier and self.watcher
        ha_config = await self.ha.get_config()
        self.ctx.timezone = ha_config.get("time_zone")
        self.notifier.timezone = self.ctx.timezone
        self.watcher.set_timezone(self.ctx.timezone)
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

    async def announce_discovery(self) -> None:
        """Let the Assist integration find this add-on automatically (Supervisor discovery)."""
        token = os.environ.get("SUPERVISOR_TOKEN")
        host = os.environ.get("HOSTNAME")
        if not token or not host or self.session is None:
            return
        payload = {"service": DISCOVERY_SERVICE,
                   "config": {"host": host, "port": self.config.port, "api_key": self.api_key}}
        try:
            async with self.session.post(
                "http://supervisor/discovery",
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status >= 400:
                    _LOGGER.warning("Discovery announcement failed (%s): %s", resp.status, await resp.text())
                    return
        except aiohttp.ClientError as err:
            _LOGGER.warning("Discovery announcement failed: %s", err)
            return
        _LOGGER.info("Assist integration can connect to http://%s:%s", host, self.config.port)

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
                "proactive": self.config.proactive,
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

    async def conversation(self, request: web.Request) -> web.Response:
        """Endpoint for the Assist conversation agent integration."""
        assert self.agent
        body = await request.json()
        text = (body.get("text") or "").strip()
        conversation_id = body.get("conversation_id") or uuid.uuid4().hex
        if not text:
            return web.json_response({"error": "empty text"}, status=400)
        try:
            result = await self.agent.run(text, conversation_id, source="assist")
            speech = result.answer
        except LLMError as err:
            _LOGGER.warning("Assist request failed: %s", err)
            speech = ("Sorry, ik kan de AI-server nu niet bereiken." if self.config.language == "nl"
                      else "Sorry, I cannot reach the AI server right now.")
        return web.json_response({"conversation_id": conversation_id, "speech": speech})

    async def pending(self, _request: web.Request) -> web.Response:
        return web.json_response([p.as_dict() for p in self.confirmations.list()])

    async def confirm(self, request: web.Request) -> web.Response:
        assert self.ctx
        approve = request.match_info["decision"] == "confirm"
        result = await resolve_confirmation(self.ctx, request.match_info["action_id"], approve)
        return web.json_response(result, status=404 if result.get("status") == "error" else 200)

    async def audit(self, request: web.Request) -> web.Response:
        limit = min(int(request.query.get("limit", 100)), 500)
        return web.json_response(self.db.audit_log(limit))

    async def events(self, request: web.Request) -> web.Response:
        limit = min(int(request.query.get("limit", 100)), 500)
        return web.json_response(self.db.events(limit=limit))

    async def memories(self, request: web.Request) -> web.Response:
        if request.method == "POST":
            text = ((await request.json()).get("text") or "").strip()
            if not text:
                return web.json_response({"error": "empty"}, status=400)
            self.db.add_memory(text[:500])
        elif request.method == "DELETE":
            self.db.delete_memory(int(request.match_info["memory_id"]))
        return web.json_response(self.db.memories(200))

    async def suggestions(self, _request: web.Request) -> web.Response:
        return web.json_response(self.db.suggestions())

    async def suggestion_decision(self, request: web.Request) -> web.Response:
        assert self.ctx
        suggestion_id = int(request.match_info["suggestion_id"])
        if request.match_info["decision"] == "dismiss":
            self.db.set_suggestion_status(suggestion_id, "dismissed")
            return web.json_response({"status": "dismissed"})
        return web.json_response(await accept_suggestion(self.ctx, suggestion_id))

    async def run_job(self, request: web.Request) -> web.Response:
        assert self.scheduler
        job = request.match_info["job"]
        try:
            if job == "summary":
                return web.json_response({"text": await self.scheduler.daily_summary()})
            return web.json_response({"new": await self.scheduler.routines()})
        except LLMError as err:
            return web.json_response({"error": str(err)}, status=502)

    async def integration(self, _request: web.Request) -> web.Response:
        host = os.environ.get("HOSTNAME", "localhost")
        return web.json_response({"url": f"http://{host}:{self.config.port}", "api_key": self.api_key})

    async def entities(self, request: web.Request) -> web.Response:
        results = self.index.search(
            request.query.get("q", ""),
            area=request.query.get("area"),
            domain=request.query.get("domain"),
            limit=min(int(request.query.get("limit", 50)), 500),
        )
        return web.json_response([e.compact(with_attributes=True) for e in results])


def access_middleware(api_key: str, supervised: bool):
    """Ingress requests are trusted; everything else (the Assist integration) needs the API key."""

    @web.middleware
    async def middleware(request: web.Request, handler):
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer ") and hmac.compare_digest(auth[7:], api_key):
            return await handler(request)
        if not supervised:
            return await handler(request)
        try:
            addr = ipaddress.ip_address(request.remote or "")
        except ValueError:
            raise web.HTTPUnauthorized()
        if addr == INGRESS_PROXY:
            return await handler(request)
        raise web.HTTPUnauthorized()

    return middleware


def create_app(config: Config) -> web.Application:
    state = App(config)
    supervised = bool(os.environ.get("SUPERVISOR_TOKEN"))
    app = web.Application(middlewares=[access_middleware(state.api_key, supervised)])
    app["state"] = state
    app.on_startup.append(state.startup)
    app.on_cleanup.append(state.shutdown)
    app.add_routes(
        [
            web.get("/", state.index_page),
            web.get("/api/status", state.status),
            web.post("/api/chat", state.chat),
            web.post("/api/conversation", state.conversation),
            web.get("/api/pending", state.pending),
            web.post(r"/api/pending/{action_id}/{decision:confirm|reject}", state.confirm),
            web.get("/api/audit", state.audit),
            web.get("/api/events", state.events),
            web.get("/api/memories", state.memories),
            web.post("/api/memories", state.memories),
            web.delete(r"/api/memories/{memory_id:\d+}", state.memories),
            web.get("/api/suggestions", state.suggestions),
            web.post(r"/api/suggestions/{suggestion_id:\d+}/{decision:accept|dismiss}", state.suggestion_decision),
            web.post(r"/api/jobs/{job:summary|routines}", state.run_job),
            web.get("/api/entities", state.entities),
            web.get("/api/integration", state.integration),
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
        "Starting AI HA Control: model=%s vision=%s ollama=%s dry_run=%s proactive=%s",
        config.model,
        config.vision_model,
        config.ollama_url,
        config.dry_run,
        config.proactive,
    )
    web.run_app(create_app(config), host="0.0.0.0", port=config.port, access_log=None)


if __name__ == "__main__":
    main()
