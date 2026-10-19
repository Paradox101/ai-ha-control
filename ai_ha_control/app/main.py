"""Entry point: web server (Ingress UI + API) and Home Assistant connection."""

from __future__ import annotations

import hmac
import ipaddress
import logging
import os
import secrets
import time
import uuid
from pathlib import Path

import asyncio
import json

import aiohttp
from aiohttp import web

from .agent.autonomous import AutonomousAgent
from .comfort import ComfortController, profile_suggestions, save_profile
from .agent.loop import Agent
from .autonomy import Autonomy, undo_action
from .config import Config, container_env, supervisor_token
from .export import entities_xlsx, export_filename
from .ha.client import HAClient
from .ha.context import ContextTracker
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
from .watcher.thresholds import ThresholdMonitor, describe, normalize, save_watch_rule
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
# What the Assist integration may reach with its API key; everything else only through Ingress (the HA panel).
INTEGRATION_PATHS = {"/api/conversation", "/api/conversation/stream", "/api/status", "/api/integration/state"}
# The Supervisor watchdog restarts the add-on when /api/health fails.
HEALTH_HA_DISCONNECTED = 300  # seconds without a Home Assistant connection
HEALTH_SCHEDULER_STALE = 45 * 60  # a heartbeat may take long (tool loop), but not this long
PUBLIC_PATHS = {"/api/health"}
MAX_TEXT_LENGTH = 4000  # characters per chat/Assist message
AUTH_FAILURE_LIMIT = 10  # failed API-key attempts per address per minute before answering 429
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    # The UI runs in Home Assistant's ingress iframe: only same-origin framing, no external resources.
    "Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                               "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
                               "connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'self'",
}


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
    path.chmod(0o600)
    return key


class App:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.api_key = load_api_key(config)
        self.index = EntityIndex()
        self.db = Database(config.data_dir)
        self.confirmations = ConfirmationStore(self.db)
        self.contexts = ContextTracker()
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
            policy=Policy(self.config, self.index),
            db=self.db,
            confirmations=self.confirmations,
            notifier=self.notifier,
            autonomy=self.autonomy,
            incidents=self.incidents,
            contexts=self.contexts,
        )
        self.notifier.index = self.index
        self.agent = Agent(self.ctx, self.registry)
        self.watcher = Watcher(self.ctx, self.notifier)
        self.scheduler = Scheduler(self.ctx, self.notifier, self.watcher)
        self.guardian = AutonomousAgent(self.ctx, build_autonomous_registry(), self.incidents)
        self.baseline = Baseline(self.db, self.index)
        self.patrol = Patrol(self.ctx, self.incidents, self.snapshots)
        self.patrol.on_observation = lambda text: self.guardian.observe(text, urgent=True)
        self.publisher = StatusPublisher(self.ha, self.db, self.autonomy)
        self.comfort = ComfortController(self.ctx)
        self.thresholds = ThresholdMonitor(self.ctx, self.incidents)
        self.incidents.on_event = lambda data: self.ha.fire_event("ai_ha_control_incident", data)
        self.watcher.agent, self.watcher.incidents = self.guardian, self.incidents
        self.watcher.baseline, self.watcher.snapshots = self.baseline, self.snapshots
        self.watcher.thresholds = self.thresholds
        s = self.scheduler
        s.agent, s.baseline, s.patrol = self.guardian, self.baseline, self.patrol
        s.incidents, s.snapshots, s.publisher = self.incidents, self.snapshots, self.publisher
        s.comfort, s.thresholds = self.comfort, self.thresholds
        self.ha.on_connect(self.refresh_index)
        self.ha.on_connect(self.watcher.on_connected)
        self.ha.on_event("homeassistant_started", self.watcher.on_ha_started)
        # Order matters: the index must be updated before the watcher looks at it.
        self.ha.on_state_changed(self.index.handle_state_changed)
        self.ha.on_state_changed(self.watcher.handle_state_changed)
        self.ha.on_event("mobile_app_notification_action", self.watcher.handle_notification_action)
        self.ha.on_event("automation_triggered", self.contexts.note_automation)
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
        self.comfort.set_timezone(self.ctx.timezone)
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
        # Always revalidate: a cached page from an older version misses builders for new settings fields.
        return web.FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

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
        message = (body.get("message") or "").strip()[:MAX_TEXT_LENGTH]
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
        text = (body.get("text") or "").strip()[:MAX_TEXT_LENGTH]
        conversation_id = body.get("conversation_id") or uuid.uuid4().hex
        if not text:
            return web.json_response({"error": "empty text"}, status=400)
        # Who spoke (None for a voice satellite): kept in the audit log; doors and the alarm are never
        # confirmed by voice anyway (see resolve_confirmation).
        self.db.audit("assist", {"user_id": body.get("user_id"), "text": text[:200]}, "received", None,
                      conversation_id)
        try:
            result = await self.agent.run(text, conversation_id, source="assist", area=body.get("area"))
            speech = result.answer
        except LLMError as err:
            _LOGGER.warning("Assist request failed: %s", err)
            speech = self._unreachable()
        return web.json_response({"conversation_id": conversation_id, "speech": speech})

    def _unreachable(self) -> str:
        return ("Sorry, ik kan de AI-server nu niet bereiken." if self.config.language == "nl"
                else "Sorry, I cannot reach the AI server right now.")

    async def conversation_stream(self, request: web.Request) -> web.StreamResponse:
        """Like /api/conversation, but streams the answer as NDJSON: {"delta": ...} lines, then {"done": true}."""
        assert self.agent
        body = await json_body(request)
        text = (body.get("text") or "").strip()[:MAX_TEXT_LENGTH]
        conversation_id = body.get("conversation_id") or uuid.uuid4().hex
        if not text:
            return web.json_response({"error": "empty text"}, status=400)
        resp = web.StreamResponse(headers={"Content-Type": "application/x-ndjson", "Cache-Control": "no-store"})
        await resp.prepare(request)

        async def send(data: dict) -> None:
            await resp.write((json.dumps(data, ensure_ascii=False) + "\n").encode())

        streamed = False

        async def on_delta(piece: str) -> None:
            nonlocal streamed
            streamed = True
            await send({"delta": piece})

        try:
            result = await self.agent.run(text, conversation_id, source="assist", area=body.get("area"),
                                          on_delta=on_delta)
            speech = result.answer
        except LLMError as err:
            _LOGGER.warning("Assist request failed: %s", err)
            speech = self._unreachable()
        await send({"done": True, "conversation_id": conversation_id, "speech": speech, "streamed": streamed})
        await resp.write_eof()
        return resp

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
            "shadow": self.db.shadow_scores(),
            "overrides": [{"entity_id": e, "name": self.index.get(e).name if self.index.get(e) else e, **o}
                          for e, o in self.db.active_overrides().items()],
            "health": self.scheduler.health() if self.scheduler else {},
        })

    async def action_feedback(self, request: web.Request) -> web.Response:
        value = 1 if (await json_body(request)).get("value", 0) > 0 else -1
        self.db.set_action_feedback(int(request.match_info["action_id"]), value)
        return web.json_response({"status": "ok", "shadow": self.db.shadow_scores()})

    async def run_feedback(self, request: web.Request) -> web.Response:
        value = 1 if (await json_body(request)).get("value", 0) > 0 else -1
        self.db.set_run_feedback(int(request.match_info["run_id"]), value)
        return web.json_response({"status": "ok"})

    async def clear_override(self, request: web.Request) -> web.Response:
        self.db.clear_override(request.match_info["entity_id"])
        return web.json_response({"status": "ok"})

    # --- Comfort profiles -------------------------------------------------------------

    async def profiles(self, request: web.Request) -> web.Response:
        if request.method == "POST":
            result = save_profile(self.db, await json_body(request))
            if result["status"] != "ok":
                return web.json_response(result, status=400)
        elif request.method == "DELETE":
            self.db.delete_profile(int(request.match_info["profile_id"]))
        return web.json_response({"profiles": self.db.profiles(), "suggestions": profile_suggestions(self.db),
                                  "areas": sorted(self.index.areas.values()),
                                  "heating": {str(k): v for k, v in self.comfort.heating.items()},
                                  "enabled": self.config.comfort})

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
        self.notifier.acknowledge_incident(incident_id)
        return web.json_response({"status": "ok"})

    async def incident_ack(self, request: web.Request) -> web.Response:
        incident_id = int(request.match_info["incident_id"])
        if self.db.get_incident(incident_id) is None:
            raise web.HTTPNotFound()
        return web.json_response({"status": "ok", "stopped": self.notifier.acknowledge_incident(incident_id)})

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

    async def scheduled(self, request: web.Request) -> web.Response:
        if request.method == "DELETE":
            self.db.cancel_scheduled(int(request.match_info["item_id"]))
        return web.json_response(self.db.scheduled())

    def _watch_rules(self) -> list[dict]:
        return [{**r, "text": describe(r, self.index.get(r["entity_id"]))} for r in self.db.watch_rules()]

    async def watch_rules(self, request: web.Request) -> web.Response:
        if request.method == "POST":
            body = await json_body(request)
            try:
                result = save_watch_rule(self.ctx, normalize(**{k: body[k] for k in (
                    "entity_id", "operator", "value", "minutes", "modes", "severity", "label") if k in body}))
            except (TypeError, ValueError) as err:
                result = {"status": "error", "error": str(err)}
            if result.get("status") == "error":
                return web.json_response(result, status=400)
        elif request.method == "DELETE":
            self.db.delete_watch_rule(int(request.match_info["rule_id"]))
        return web.json_response(self._watch_rules())

    async def watch_rule_toggle(self, request: web.Request) -> web.Response:
        self.db.set_watch_enabled(int(request.match_info["rule_id"]), bool((await json_body(request)).get("enabled")))
        return web.json_response(self._watch_rules())

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

    async def integration_state(self, _request: web.Request) -> web.Response:
        """Polled by the Home Assistant integration, which turns it into real entities."""
        assert self.guardian and self.llm
        self.publisher.integration_polled()
        switch = self.index.get(self.config.kill_switch) if self.config.kill_switch else None
        perf = self.llm.performance()
        return web.json_response({
            **self.publisher.state(self.guardian.mode, self.guardian.last_summary),
            "kill_switch": switch.state == "on" if switch else None,
            "kill_switch_entity": switch.entity_id if switch else None,
            "llm_avg_seconds": perf["avg_seconds"],
            "llm_queue": perf["queue"],
            "pending_confirmations": len(self.confirmations.list()),
            "health_problems": self.health_problems(),
        })

    def health_problems(self) -> list[str]:
        now = time.monotonic()
        problems = []
        if self.ha is not None and self.ha.disconnected_since is not None \
                and now - self.ha.disconnected_since > HEALTH_HA_DISCONNECTED:
            problems.append("home_assistant_disconnected")
        if self.scheduler is not None and now - self.scheduler.loop_tick > HEALTH_SCHEDULER_STALE:
            problems.append("scheduler_stalled")
        return problems

    async def llm_performance(self, _request: web.Request) -> web.Response:
        assert self.llm
        return web.json_response(self.llm.performance())

    async def health(self, _request: web.Request) -> web.Response:
        problems = self.health_problems()
        return web.json_response({"ok": not problems, "problems": problems}, status=503 if problems else 200)

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
        if request.query.get("format") == "pick":
            return web.json_response([{"id": e.entity_id, "name": e.name, "area": e.area or ""} for e in results])
        return web.json_response([e.compact(with_attributes=True) for e in results])

    async def export_entities(self, request: web.Request) -> web.Response:
        return web.Response(body=entities_xlsx(self.index), headers={
            "Content-Type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "Content-Disposition": f'attachment; filename="{export_filename()}"'})


def access_middleware(api_key: str, supervised: bool):
    """Ingress requests are trusted; everything else (the Assist integration) needs the API key."""
    failures: dict[str, list[float]] = {}

    def blocked(remote: str) -> bool:
        now = time.monotonic()
        recent = [t for t in failures.get(remote, []) if now - t < 60]
        failures[remote] = recent
        if not recent:
            failures.pop(remote, None)
        return len(recent) >= AUTH_FAILURE_LIMIT

    @web.middleware
    async def middleware(request: web.Request, handler):
        if request.path in PUBLIC_PATHS:
            return await handler(request)
        remote = request.remote or ""
        auth = request.headers.get("Authorization", "")
        valid_key = False
        if auth.startswith("Bearer "):
            if blocked(remote):
                raise web.HTTPTooManyRequests()
            valid_key = hmac.compare_digest(auth[7:].encode(), api_key.encode())
            if not valid_key:
                failures.setdefault(remote, []).append(time.monotonic())
        if valid_key and request.path in INTEGRATION_PATHS:
            return await handler(request)
        if not supervised:
            return await handler(request)
        try:
            addr = ipaddress.ip_address(remote)
        except ValueError:
            raise web.HTTPUnauthorized()
        if addr == INGRESS_PROXY:
            return await handler(request)
        if valid_key:
            # The key is for the Assist integration only; settings, kill switch etc. need the HA panel.
            raise web.HTTPForbidden(text="de API-sleutel geeft alleen toegang tot het gesprek")
        raise web.HTTPUnauthorized()

    return middleware


async def security_headers(_request: web.Request, resp: web.StreamResponse) -> None:
    for name, value in SECURITY_HEADERS.items():
        resp.headers.setdefault(name, value)


def create_app(config: Config) -> web.Application:
    state = App(config)
    supervised = bool(supervisor_token())
    app = web.Application(middlewares=[access_middleware(state.api_key, supervised)], client_max_size=256 * 1024)
    app.on_response_prepare.append(security_headers)
    app["state"] = state
    app.on_startup.append(state.startup)
    app.on_cleanup.append(state.shutdown)
    app.add_routes(
        [
            web.get("/", state.index_page),
            web.get("/api/status", state.status),
            web.post("/api/chat", state.chat),
            web.post("/api/conversation", state.conversation),
            web.post("/api/conversation/stream", state.conversation_stream),
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
            web.get("/api/export/entities.xlsx", state.export_entities),
            web.get("/api/integration", state.integration),
            web.get("/api/integration/state", state.integration_state),
            web.get("/api/llm/performance", state.llm_performance),
            web.get("/api/agent", state.agent_status),
            web.post("/api/agent/run", state.agent_run),
            web.post("/api/agent/kill-switch", state.kill_switch),
            web.post(r"/api/actions/{action_id:\d+}/undo", state.undo),
            web.post(r"/api/actions/{action_id:\d+}/feedback", state.action_feedback),
            web.post(r"/api/runs/{run_id:\d+}/feedback", state.run_feedback),
            web.delete("/api/overrides/{entity_id}", state.clear_override),
            web.get("/api/profiles", state.profiles),
            web.post("/api/profiles", state.profiles),
            web.delete(r"/api/profiles/{profile_id:\d+}", state.profiles),
            web.delete("/api/mutes/{key}", state.unmute),
            web.post(r"/api/events/{event_id:\d+}/feedback", state.feedback),
            web.get("/api/incidents", state.incidents_list),
            web.get(r"/api/incidents/{incident_id:\d+}", state.incident_detail),
            web.post(r"/api/incidents/{incident_id:\d+}/resolve", state.incident_resolve),
            web.post(r"/api/incidents/{incident_id:\d+}/ack", state.incident_ack),
            web.get("/api/snapshots/{name}", state.snapshot),
            web.get("/api/rules", state.rules),
            web.post("/api/rules", state.rules),
            web.delete(r"/api/rules/{rule_id:\d+}", state.rules),
            web.post(r"/api/rules/{rule_id:\d+}/toggle", state.rule_toggle),
            web.get("/api/scheduled", state.scheduled),
            web.delete(r"/api/scheduled/{item_id:\d+}", state.scheduled),
            web.get("/api/watch-rules", state.watch_rules),
            web.post("/api/watch-rules", state.watch_rules),
            web.delete(r"/api/watch-rules/{rule_id:\d+}", state.watch_rules),
            web.post(r"/api/watch-rules/{rule_id:\d+}/toggle", state.watch_rule_toggle),
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
