"""Time based jobs: periodic rule checks, daily summary and nightly routine analysis."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime
from typing import Any

from ..agent.loop import DAYS_NL
from ..notify import Notifier
from ..tools.base import ToolContext
from ..i18n import text as text_for
from ..tools.context import agenda, energy_info
from ..scheduled import run_due
from .baseline import MODE_SLEEP, house_mode
from .routines import analyze_routines
from .rules import Watcher

_LOGGER = logging.getLogger(__name__)

TICK_SECONDS = 60
HEALTH_REPORT_WEEKDAY, HEALTH_REPORT_TIME = 0, "10:00"  # Monday morning
ROUTINE_ANALYSIS_TIME = "03:30"
LLM_CHECK_SECONDS = 300


def low_batteries(ctx: ToolContext, threshold: float = 20) -> list[str]:
    low = []
    for ent in ctx.index.entities.values():
        if ent.device_class != "battery":
            continue
        if ent.domain == "binary_sensor" and ent.state == "on":
            low.append(ent.name)
        elif ent.domain == "sensor":
            try:
                if float(ent.state) <= threshold:
                    low.append(f"{ent.name} ({ent.state}%)")
            except ValueError:
                pass
    return low


async def build_daily_summary(ctx: ToolContext, notifier: Notifier) -> str:
    now = notifier.now()
    data: dict[str, Any] = {
        "datum": f"{DAYS_NL[now.weekday()]} {now:%d-%m-%Y}",
        "huis": ctx.index.overview(),
        "lege_batterijen": low_batteries(ctx),
        "incidenten_afgelopen_24u": [f"{i['title']} ({i['status']})" for i in
                                     ctx.db.incidents(since=time.time() - 86400, limit=10)],
        "zelf_gedaan_afgelopen_24u": [f"{a['domain']}.{a['service']} {', '.join(a['entity_ids'])}"
                                      + (" (teruggedraaid)" if a["undone"] else "")
                                      for a in ctx.db.recent_actions(since=time.time() - 86400, limit=10)],
        "meldingen_afgelopen_24u": [f"{e['title']}: {e['message']}" for e in
                                    ctx.db.events(since=time.time() - 86400, limit=20)
                                    if e["kind"] not in ("summary", "confirmation", "confirmation_result")],
        "routine_suggesties": len(ctx.db.suggestions()),
    }
    if ctx.config.calendar_context:
        data["agenda_vandaag"] = await agenda(ctx)
    if ctx.config.energy_context:
        data["energie"] = energy_info(ctx)
    messages = [
        {"role": "system", "content": text_for(ctx.config, "summary_system")},
        {"role": "user", "content": text_for(ctx.config, "summary_prompt") + "\n\n"
            + json.dumps(data, ensure_ascii=False, default=str)},
    ]
    reply = await ctx.llm.chat(messages)
    return (reply.get("content") or "").strip()


class Scheduler:
    def __init__(self, ctx: ToolContext, notifier: Notifier, watcher: Watcher) -> None:
        self._ctx = ctx
        self._notifier = notifier
        self._watcher = watcher
        self._last_run: dict[str, str] = {}
        self._task: asyncio.Task | None = None
        self.loop_tick = time.monotonic()  # start of the latest loop iteration, for the health check
        # Optional collaborators of the autonomous agent, wired up by the app.
        self.agent = None
        self.baseline = None
        self.patrol = None
        self.incidents = None
        self.snapshots = None
        self.publisher = None
        self.comfort = None
        self.thresholds = None
        self._previous_mode: str | None = None
        self.appliances = None
        self.simulation = None
        self._agent_task: asyncio.Task | None = None
        self._llm_checked = -1e9
        self.llm_down_since: float | None = None
        self._llm_reported = False
        self.last_tick = 0.0
        self._last_digest = time.time()

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    def stop(self) -> None:
        if self._task:
            self._task.cancel()
        if self._agent_task:
            self._agent_task.cancel()

    async def wait_agent(self) -> None:
        """Wait for a heartbeat started in the background (used in tests)."""
        if self._agent_task:
            await asyncio.gather(self._agent_task, return_exceptions=True)

    def _start_agent(self) -> None:
        """The heartbeat runs on its own: a slow LLM must not stop the rule checks and the patrol."""
        async def run() -> None:
            try:
                await self.agent.run()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Autonomous heartbeat failed")

        self._agent_task = asyncio.create_task(run())

    def _due(self, job: str, at: str | None, now: datetime) -> bool:
        if not at:
            return False
        try:
            hour, minute = map(int, at.strip().split(":"))
        except ValueError:
            return False
        today = now.date().isoformat()
        if (now.hour, now.minute) >= (hour, minute) and self._last_run.get(job) != today:
            # Only run within an hour of the planned time (not when the add-on starts at night).
            if now.hour * 60 + now.minute - (hour * 60 + minute) <= 60:
                self._last_run[job] = today
                return True
            self._last_run[job] = today
        return False

    async def _loop(self) -> None:
        while True:
            self.loop_tick = time.monotonic()
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Scheduled job failed")
            await asyncio.sleep(TICK_SECONDS)

    async def _stage(self, name: str, coro: Any, job: str | None = None) -> bool:
        """One failing stage (a broken notify service, a bad rule) must not starve the others."""
        try:
            await coro
            return True
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Scheduled stage %s failed", name)
            if job is not None:
                self._last_run.pop(job, None)  # try the daily job again next tick
            return False

    async def run_once(self) -> None:
        if not self._ctx.ha.connected or not self._ctx.index.entities:
            return
        config = self._ctx.config
        await self._stage("watcher", self._watcher.tick())
        await self._stage("escalate", self._notifier.escalate())
        await self._stage("scheduled", run_due(self._ctx))
        now = self._notifier.now()
        await self._autonomous_round(now)
        await self._stage("llm", self._check_llm())
        if config.digest_time and self._due("digest", config.digest_time, now):
            if await self._stage("digest", self._notifier.send_digest(self._last_digest), "digest"):
                self._last_digest = time.time()
        self.last_tick = time.time()
        if config.proactive and self._due("summary", config.daily_summary_time, now):
            await self._stage("summary", self.daily_summary(), "summary")
        if config.routine_learning and self._due("routines", ROUTINE_ANALYSIS_TIME, now):
            await self._stage("routines", self.routines(), "routines")
        if config.health_report and now.weekday() == HEALTH_REPORT_WEEKDAY and \
                self._due("doctor", HEALTH_REPORT_TIME, now):
            from ..doctor import send_health_report

            await self._stage("doctor", send_health_report(self._ctx), "doctor")

    async def _autonomous_round(self, now: datetime) -> None:
        config = self._ctx.config
        settling = self._watcher.in_grace
        mode = house_mode(self._ctx.index, config, now)
        if self.agent is not None:
            self.agent.set_mode(mode)
        if mode == MODE_SLEEP and self._previous_mode not in (None, MODE_SLEEP) and not settling \
                and config.bedtime_check:
            await self._stage("bedtime", self._watcher.bedtime_round())
        self._previous_mode = mode
        if self.baseline is not None and not settling:
            await self._stage("baseline", self._anomalies(now))
        if self.agent is not None and not settling and self.agent.due() and \
                (self._agent_task is None or self._agent_task.done()):
            self._start_agent()
        if self.comfort is not None and not settling:
            await self._stage("comfort", self.comfort.tick(mode, now))
        if self.thresholds is not None and not settling:
            await self._stage("thresholds", self.thresholds.tick(mode))
        if self.appliances is not None and not settling:
            await self._stage("appliances", self.appliances.tick(mode))
        if self.simulation is not None and not settling:
            await self._stage("simulation", self.simulation.tick(mode, now))
        if self.patrol is not None and not settling and self.patrol.due(mode):
            await self._stage("patrol", self.patrol.run())
        if self.incidents is not None:
            try:
                self.incidents.resolve_stale()
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Resolving stale incidents failed")
        if self._due("cleanup", "04:00", now):
            try:
                if self.snapshots is not None:
                    self.snapshots.cleanup(config.snapshot_retention_days)
                _LOGGER.info("Database cleanup: %s", self._ctx.db.prune())
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Database cleanup failed")
        if self.publisher is not None:
            await self._stage("publish", self.publisher.publish(mode, self.agent.last_summary if self.agent else "",
                                                                self.health()))

    async def _anomalies(self, now: datetime) -> None:
        for anomaly in self.baseline.sample(now) + self.baseline.offline():
            await self._watcher.report_anomaly(anomaly)

    def health(self) -> dict[str, Any]:
        """For the dead man's switch: when we last ran and whether the AI server answers."""
        tz = self._notifier.now().tzinfo
        stamp = lambda ts: datetime.fromtimestamp(ts, tz).isoformat(timespec="minutes") if ts else None  # noqa: E731
        return {
            "last_tick": stamp(time.time()),
            "last_agent_run": stamp(self.agent.last_run_ts) if self.agent is not None else None,
            "ollama": "onbereikbaar" if self.llm_down_since else "ok",
        }

    async def _check_llm(self) -> None:
        """Tell the user once when the AI server has been unreachable for a while, and when it is back."""
        if time.monotonic() - self._llm_checked < LLM_CHECK_SECONDS:
            return
        self._llm_checked = time.monotonic()
        try:
            reachable = (await self._ctx.llm.status()).get("reachable", False)
        except Exception:  # noqa: BLE001
            reachable = False
        now = time.time()
        if reachable:
            if self._llm_reported:
                await self._notifier.send("✅ AI-server weer bereikbaar", "De autonome agent werkt weer normaal.",
                                          kind="health", respect_quiet_hours=True)
            self.llm_down_since, self._llm_reported = None, False
            return
        self.llm_down_since = self.llm_down_since or now
        down_minutes = (now - self.llm_down_since) / 60
        if not self._llm_reported and down_minutes >= self._ctx.config.llm_down_minutes:
            self._llm_reported = True
            since = datetime.fromtimestamp(self.llm_down_since, self._notifier.now().tzinfo)
            await self._notifier.send(
                "⚠️ AI-server niet bereikbaar",
                f"Ollama reageert niet sinds {since:%H:%M}. Rook-, lek- en inbraakmeldingen werken door, "
                "maar de agent kan niet nadenken en camerabeelden worden niet bekeken.",
                kind="health", respect_quiet_hours=False)

    async def daily_summary(self) -> str:
        text = await build_daily_summary(self._ctx, self._notifier)
        if text:
            await self._notifier.send(text_for(self._ctx.config, "summary_title"), text, kind="summary")
        return text

    async def routines(self) -> int:
        new = await analyze_routines(self._ctx)
        if new:
            await self._notifier.send(
                text_for(self._ctx.config, "routines_title"),
                text_for(self._ctx.config, "routines_message", count=new),
                kind="routine",
            )
        return new
