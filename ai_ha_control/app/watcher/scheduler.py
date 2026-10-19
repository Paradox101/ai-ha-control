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
from .baseline import house_mode
from .routines import analyze_routines
from .rules import Watcher

_LOGGER = logging.getLogger(__name__)

TICK_SECONDS = 60
ROUTINE_ANALYSIS_TIME = "03:30"


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
    messages = [
        {"role": "system", "content": "Je bent de AI-assistent van het huis. Schrijf in het Nederlands."},
        {"role": "user", "content":
            "Schrijf een korte, vriendelijke ochtendsamenvatting (maximaal 6 zinnen, geen opmaak) voor de "
            "bewoners op basis van deze gegevens. Noem het weer, bijzonderheden van de nacht (incidenten), wat je "
            "zelf hebt gedaan, open ramen/sloten en lege batterijen alleen als ze er zijn.\n\n"
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
        # Optional collaborators of the autonomous agent, wired up by the app.
        self.agent = None
        self.baseline = None
        self.patrol = None
        self.incidents = None
        self.snapshots = None
        self.publisher = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    def stop(self) -> None:
        if self._task:
            self._task.cancel()

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
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Scheduled job failed")
            await asyncio.sleep(TICK_SECONDS)

    async def run_once(self) -> None:
        if not self._ctx.ha.connected or not self._ctx.index.entities:
            return
        config = self._ctx.config
        await self._watcher.tick()
        now = self._notifier.now()
        await self._autonomous_round(now)
        if config.proactive and self._due("summary", config.daily_summary_time, now):
            await self.daily_summary()
        if config.routine_learning and self._due("routines", ROUTINE_ANALYSIS_TIME, now):
            await self.routines()

    async def _autonomous_round(self, now: datetime) -> None:
        config = self._ctx.config
        settling = self._watcher.in_grace
        mode = house_mode(self._ctx.index, config, now)
        if self.agent is not None:
            self.agent.set_mode(mode)
        if self.baseline is not None and not settling:
            for anomaly in self.baseline.sample(now) + self.baseline.offline():
                await self._watcher.report_anomaly(anomaly)
        if self.agent is not None and not settling and self.agent.due():
            await self.agent.run()
        if self.patrol is not None and not settling and self.patrol.due(mode):
            await self.patrol.run()
        if self.incidents is not None:
            self.incidents.resolve_stale()
        if self.snapshots is not None and self._due("cleanup", "04:00", now):
            self.snapshots.cleanup(config.snapshot_retention_days)
        if self.publisher is not None:
            await self.publisher.publish(mode, self.agent.last_summary if self.agent else "")

    async def daily_summary(self) -> str:
        text = await build_daily_summary(self._ctx, self._notifier)
        if text:
            await self._notifier.send("☀️ Goedemorgen", text, kind="summary")
        return text

    async def routines(self) -> int:
        new = await analyze_routines(self._ctx)
        if new:
            await self._notifier.send(
                "💡 Nieuwe routines gevonden",
                f"Ik heb {new} terugkerende handeling(en) gevonden die ik kan automatiseren. "
                "Bekijk ze in AI Huis → Routines.",
                kind="routine",
            )
        return new
