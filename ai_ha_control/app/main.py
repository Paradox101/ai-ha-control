"""Entry point: web server (Ingress UI + API) and Home Assistant connection."""

from __future__ import annotations

import hmac
import ipaddress
import logging
import os
import secrets
import uuid
from pathlib import Path

import asyncio
import json

import aiohttp
from aiohttp import web

from .agent.autonomous import AutonomousAgent
from .agent.loop import Agent
from .autonomy import Autonomy, undo_action
from .config import Config, container_env, supervisor_token
from .ha.client import HAClient
from .ha.index import EntityIndex
from .ha.status import StatusPublisher, ensure_kill_switch
from .llm.ollama import LLMError, OllamaClient
from .notify import Notifier
from .policy import Policy
from .storage.db import Database
from .tools.actions import ConfirmationStore, resolve_confirmation
from .tools.base import ToolContext
from .tools.home import build_autonomous_registry, build_registry
from . import settings as settings_module
from .watcher.baseline import Baseline
from .watcher.incidents import IncidentManager, SnapshotStore
from .watcher.patrol import Patrol
from .watcher.routines import accept_suggestion
from .watcher.rules import Watcher, apply_feedback
from .watcher.scheduler import Scheduler

_LOGGER = logging.getLogger("ai_ha_control")
STATIC_DIR = Path(__file__).parent / "web" / "static"
INGRESS_PROXY = ipaddress.ip_address("172.30.32.2")
DISCOVERY_SERVICE = "ai_ha_control"
REGISTRY_EVENTS = ("entity_registry_updated", "device_registry_updated", "area_registry_updated")
REFRESH_DELAY = 5  # seconds; registry changes often come in bursts


def query_int(request: web.Request, name: str, default: int, maximum: int) -> int:
    try:
        return max(1, min(int(request.query.get(name, default)), maximum))
    except ValueError:
        raise web.HTTPBadRequest(text=f"invalid {name}") from None


async def json_body(request: web.Request) -> dict:
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise web.HTTPBadRequest(text="invalid JSON") from None
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(text="expected a JSON object")
    return body


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
        self.guardian: AutonomousAgent | None = None
        self._refresh_task: asyncio.Task | None = None

    async def startup(self, _app: web.Application) -> None:
        self.session = aiohttp.ClientSession()
        self.ha = HAClient(self.config, self.session)
        self.llm = OllamaClient(self.config, self.session)
        self.notifier = Notifier(self.config, self.ha, self.db)
        self.autonomy = Autonomy(self.config, self.index, self.db)
        self.incidents = IncidentManager(self.db, self.notifier)
        self.snapshots = SnapshotStore(self.config.data_dir)
        self.ctx = ToolContext(
            config=self.config,
            ha=self.ha,
            index=self.index,
            llm=self.llm,
            policy=Policy(self.config),
            db=self.db,
            confirmations=self.confirmations,
            notifier=self.notifier,
            autonomy=self.autonomy,
            incidents=self.incidents,
        )
        self.agent = Agent(self.ctx, self.registry)
        self.watcher = Watcher(self.ctx, self.notifier)
        self.scheduler = Scheduler(self.ctx, self.notifier, self.watcher)
        self.guardian = AutonomousAgent(self.ctx, build_autonomous_registry(), self.incidents)
        self.baseline = Baseline(self.db, self.index)
        self.patrol = Patrol(self.ctx, self.incidents, self.snapshots)
        self.patrol.on_observation = lambda text: self.guardian.observe(text, urgent=True)
        self.publisher = StatusPublisher(self.ha, self.db, self.autonomy)
        self.watcher.agent, self.watcher.incidents = self.guardian, self.incidents
        self.watcher.baseline, self.watcher.snapshots = self.baseline, self.snapshots
        s = self.scheduler
        s.agent, s.baseline, s.patrol = self.guardian, self.baseline, self.patrol
        s.incidents, s.snapshots, s.publisher = self.incidents, self.snapshots, self.publisher
        self.ha.on_connect(self.refresh_index)
        self.ha.on_connect(self.watcher.on_connected)
        self.ha.on_event("homeassistant_started", self.watcher.on_ha_started)
        # Order matters: the index must be updated before the watcher looks at it.
        self.ha.on_state_changed(self.index.handle_state_changed)
        self.ha.on_state_changed(self.watcher.handle_state_changed)
        self.ha.on_event("mobile_app_notification_action", self.watcher.handle_notification_action)
        self.ha.on_event("automation_triggered", self.guardian.note_automation)
        for event_type in REGISTRY_EVENTS:
            self.ha.on_event(event_type, self.schedule_refresh)
        await self.ha.start()
        self.scheduler.start()
        await self.announce_discovery()

    async def shutdown(self, _app: web.Application) -> None:
        if self.scheduler:
            self.scheduler.stop()
        if self._refresh_task:
            self._refresh_task.cancel()
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
        self.guardian.set_timezone(self.ctx.timezone)
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
        if self.config.autonomous and self.config.kill_switch:
            await ensure_kill_switch(self.ha, self.index, self.config.kill_switch)
        self.publisher.reset()

    def schedule_refresh(self, _data: dict) -> None:
        """Reload the index shortly after entities, devices or areas change."""
        if self._refresh_task and not self._refresh_task.done():
            self._refresh_task.cancel()
        self._refresh_task = asyncio.create_task(self._delayed_refresh())

    async def _delayed_refresh(self) -> None:
        await asyncio.sleep(REFRESH_DELAY)
        try:
            await self.refresh_index()
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Index refresh failed: %s", err)

    async def announce_discovery(self) -> None:
        """Let the Assist integration find this add-on automatically (Supervisor discovery)."""
        token = supervisor_token()
        host = container_env("HOSTNAME")
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
                "mode": self.guardian.mode if self.guardian else None,
                "autonomous": self.config.autonomous,
                "acting": self.autonomy.enabled,
                "active_incidents": len(self.db.incidents(status="active")),
                "proactive": self.config.proactive,
                "language": self.config.language,
            }
        )

    async def chat(self, request: web.Request) -> web.Response:
        assert self.agent
        body = await json_body(request)
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
        body = await json_body(request)
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
        return web.json_response(self.db.audit_log(query_int(request, "limit", 100, 500)))

    async def events(self, request: web.Request) -> web.Response:
        return web.json_response(self.db.events(limit=query_int(request, "limit", 100, 500)))

    async def memories(self, request: web.Request) -> web.Response:
        if request.method == "POST":
            text = ((await json_body(request)).get("text") or "").strip()
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

    # --- Autonomous agent ---------------------------------------------------------

    async def agent_status(self, _request: web.Request) -> web.Response:
        assert self.guardian
        switch = self.index.get(self.config.kill_switch) if self.config.kill_switch else None
        return web.json_response({
            "enabled": self.config.autonomous,
            "acting": self.autonomy.enabled,
            "kill_switch": {"entity_id": self.config.kill_switch, "state": switch.state if switch else None},
            "mode": self.guardian.mode,
            "running": self.guardian.running,
            "last_summary": self.guardian.last_summary,
            "runs": self.db.agent_runs(30),
            "actions": self.db.recent_actions(limit=30),
            "followups": self.db.pending_followups(),
            "mutes": self.db.mutes(),
        })

    async def agent_run(self, _request: web.Request) -> web.Response:
        assert self.guardian
        if self.guardian.running:
            return web.json_response({"error": "de agent is al bezig"}, status=409)
        self.guardian.observe("Handmatige controle gevraagd vanuit de web-UI")
        try:
            summary = await self.guardian.run("handmatig")
        except LLMError as err:
            return web.json_response({"error": str(err)}, status=502)
        return web.json_response({"summary": summary or "Niets bijzonders"})

    async def kill_switch(self, request: web.Request) -> web.Response:
        assert self.ha
        if not self.config.kill_switch:
            return web.json_response({"error": "geen noodknop ingesteld"}, status=400)
        on = bool((await json_body(request)).get("on"))
        await self.ha.call_service("input_boolean", "turn_on" if on else "turn_off",
                                   target={"entity_id": [self.config.kill_switch]})
        self.db.audit("kill_switch", {"on": on}, "executed", "ok", "web")
        return web.json_response({"on": on})

    async def undo(self, request: web.Request) -> web.Response:
        assert self.ctx
        result = await undo_action(self.ctx, int(request.match_info["action_id"]))
        return web.json_response(result, status=400 if result.get("status") == "error" else 200)

    async def unmute(self, request: web.Request) -> web.Response:
        self.db.unmute(request.match_info["key"])
        return web.json_response({"status": "ok"})

    async def feedback(self, request: web.Request) -> web.Response:
        value = 1 if (await json_body(request)).get("value", 0) > 0 else -1
        apply_feedback(self.db, int(request.match_info["event_id"]), value)
        return web.json_response({"status": "ok"})

    async def incidents_list(self, request: web.Request) -> web.Response:
        if request.query.get("status") == "active":
            return web.json_response(self.db.incidents(status="active"))
        return web.json_response(self.db.incidents(limit=query_int(request, "limit", 50, 200)))

    async def incident_detail(self, request: web.Request) -> web.Response:
        incident = self.db.get_incident(int(request.match_info["incident_id"]))
        if incident is None:
            raise web.HTTPNotFound()
        return web.json_response(incident)

    async def incident_resolve(self, request: web.Request) -> web.Response:
        incident_id = int(request.match_info["incident_id"])
        if self.db.get_incident(incident_id) is None:
            raise web.HTTPNotFound()
        self.incidents.resolve(incident_id, "Afgesloten door de bewoner")
        return web.json_response({"status": "ok"})

    async def snapshot(self, request: web.Request) -> web.StreamResponse:
        path = self.snapshots.path(request.match_info["name"])
        if path is None:
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={"Cache-Control": "max-age=86400"})

    async def rules(self, request: web.Request) -> web.Response:
        if request.method == "POST":
            text = ((await json_body(request)).get("text") or "").strip()
            if not text:
                return web.json_response({"error": "lege regel"}, status=400)
            self.db.add_rule(text[:300])
        elif request.method == "DELETE":
            self.db.delete_rule(int(request.match_info["rule_id"]))
        return web.json_response(self.db.rules())

    async def rule_toggle(self, request: web.Request) -> web.Response:
        enabled = bool((await json_body(request)).get("enabled"))
        self.db.set_rule_enabled(int(request.match_info["rule_id"]), enabled)
        return web.json_response(self.db.rules())

    # --- Settings --------------------------------------------------------------------

    async def settings_get(self, _request: web.Request) -> web.Response:
        assert self.llm
        return web.json_response({
            "values": settings_module.current_values(self.config),
            "sections": settings_module.SECTIONS,
            "choices": await settings_module.choices(self.index, self.llm),
        })

    async def settings_save(self, request: web.Request) -> web.Response:
        assert self.session
        changes = (await json_body(request)).get("values") or {}
        try:
            clean = settings_module.validate(self.config, changes)
        except settings_module.SettingsError as err:
            return web.json_response({"error": "Controleer de gemarkeerde velden", "fields": err.errors}, status=400)
        settings_module.apply(self.config, clean)
        warning = await settings_module.persist(self.session, self.config)
        self.db.audit("settings", {"changed": sorted(clean)}, "executed", warning or "ok", "web")
        return web.json_response({"values": settings_module.current_values(self.config), "warning": warning})

    async def test_ollama(self, request: web.Request) -> web.Response:
        assert self.llm
        url = ((await json_body(request)).get("url") or "").strip()
        return web.json_response(await self.llm.status(url or None))

    async def health(self, _request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    async def camera(self, request: web.Request) -> web.Response:
        assert self.ha
        entity_id = request.match_info["entity_id"]
        ent = self.index.get(entity_id)
        if ent is None or ent.domain != "camera":
            raise web.HTTPNotFound()
        try:
            image = await self.ha.camera_snapshot(entity_id)
        except Exception as err:  # noqa: BLE001
            return web.json_response({"error": str(err)}, status=502)
        return web.Response(body=image, content_type="image/jpeg", headers={"Cache-Control": "no-store"})

    async def integration(self, _request: web.Request) -> web.Response:
        host = container_env("HOSTNAME") or "localhost"
        return web.json_response({"url": f"http://{host}:{self.config.port}", "api_key": self.api_key})

    async def entities(self, request: web.Request) -> web.Response:
        results = self.index.search(
            request.query.get("q", ""),
            area=request.query.get("area"),
            domain=request.query.get("domain"),
            limit=query_int(request, "limit", 50, 500),
        )
        return web.json_response([e.compact(with_attributes=True) for e in results])


def access_middleware(api_key: str, supervised: bool):
    """Ingress requests are trusted; everything else (the Assist integration) needs the API key."""

    @web.middleware
    async def middleware(request: web.Request, handler):
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer ") and hmac.compare_digest(auth[7:].encode(), api_key.encode()):
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
    supervised = bool(supervisor_token())
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
            web.get("/api/agent", state.agent_status),
            web.post("/api/agent/run", state.agent_run),
            web.post("/api/agent/kill-switch", state.kill_switch),
            web.post(r"/api/actions/{action_id:\d+}/undo", state.undo),
            web.delete("/api/mutes/{key}", state.unmute),
            web.post(r"/api/events/{event_id:\d+}/feedback", state.feedback),
            web.get("/api/incidents", state.incidents_list),
            web.get(r"/api/incidents/{incident_id:\d+}", state.incident_detail),
            web.post(r"/api/incidents/{incident_id:\d+}/resolve", state.incident_resolve),
            web.get("/api/snapshots/{name}", state.snapshot),
            web.get("/api/rules", state.rules),
            web.post("/api/rules", state.rules),
            web.delete(r"/api/rules/{rule_id:\d+}", state.rules),
            web.post(r"/api/rules/{rule_id:\d+}/toggle", state.rule_toggle),
            web.get("/api/settings", state.settings_get),
            web.post("/api/settings", state.settings_save),
            web.post("/api/settings/test-ollama", state.test_ollama),
            web.get("/api/health", state.health),
            web.get(r"/api/camera/{entity_id:camera\.[a-z0-9_]+}", state.camera),
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
    else:
        _LOGGER.info("Home Assistant token found (%s)", "HA_TOKEN" if os.environ.get("HA_TOKEN") else "Supervisor")
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
