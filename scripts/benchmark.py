"""Compare Ollama models on Dutch home-control tasks against a simulated house.

    python scripts/benchmark.py --ollama http://192.168.1.10:11434 --models qwen3:8b qwen3:4b

Nothing is sent to Home Assistant: service calls are recorded by a fake client.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "ai_ha_control"), str(ROOT)]

from app.agent.loop import Agent  # noqa: E402
from app.config import Config  # noqa: E402
from app.ha.index import EntityIndex  # noqa: E402
from app.llm.ollama import OllamaClient  # noqa: E402
from app.policy import Policy  # noqa: E402
from app.storage.db import Database  # noqa: E402
from app.tools import actions  # noqa: E402
from app.tools.actions import ConfirmationStore  # noqa: E402
from app.tools.base import ToolContext  # noqa: E402
from app.tools.home import build_registry  # noqa: E402
from tests.conftest import AREAS, DEVICES, ENTITIES, SERVICES, STATES, FakeHA  # noqa: E402


@dataclass
class Case:
    prompt: str
    check: Callable[[ToolContext, str], bool]


def called(domain: str, service: str, entity: str, **data) -> Callable[[ToolContext, str], bool]:
    def check(ctx: ToolContext, _answer: str) -> bool:
        for d, s, payload, target in ctx.ha.calls:
            if (d, s) == (domain, service) and entity in (target or {}).get("entity_id", []):
                return all(float((payload or {}).get(k, -1)) == float(v) for k, v in data.items())
        return False
    return check


def answer_contains(*words: str) -> Callable[[ToolContext, str], bool]:
    return lambda ctx, answer: not ctx.ha.calls and all(w.lower() in answer.lower() for w in words)


def pending_for(entity: str) -> Callable[[ToolContext, str], bool]:
    return lambda ctx, _a: not ctx.ha.calls and any(
        entity in p.description for p in ctx.confirmations.list())


def remembered(word: str) -> Callable[[ToolContext, str], bool]:
    return lambda ctx, _a: any(word in m["text"] for m in ctx.db.memories())


CASES = [
    Case("Doe de lamp in de keuken uit", called("light", "turn_off", "light.keuken")),
    Case("Zet de plafondlamp in de woonkamer op 30 procent", called("light", "turn_on", "light.woonkamer_plafond",
                                                                    brightness_pct=30)),
    Case("Zet de thermostaat op 21 graden", called("climate", "set_temperature", "climate.thermostaat",
                                                   temperature=21)),
    Case("Zet het koffiezetapparaat aan", called("switch", "turn_on", "switch.koffiezetapparaat")),
    Case("Hoe warm is het in de woonkamer?", answer_contains("21")),
    Case("Welke ramen staan er open?", answer_contains("slaapkamer")),
    Case("Is Vincent thuis?", answer_contains("thuis")),
    Case("Doe de voordeur van het slot", pending_for("lock.voordeur")),
    Case("Onthoud dat ik doordeweeks om 7 uur opsta", remembered("7")),
]


def make_context(config: Config, llm: OllamaClient, data_dir: str) -> ToolContext:
    index = EntityIndex()
    index.load(STATES, AREAS, DEVICES, ENTITIES, SERVICES)
    index.location_name = "Thuis"
    return ToolContext(config=config, ha=FakeHA(), index=index, llm=llm, policy=Policy(config, index),
                       db=Database(data_dir), confirmations=ConfirmationStore(), timezone="Europe/Amsterdam")


async def bench_model(session: aiohttp.ClientSession, args: argparse.Namespace, model: str) -> tuple[int, float]:
    passed, total_time = 0, 0.0
    print(f"\n=== {model} ===")
    for i, case in enumerate(CASES):
        config = Config(ollama_url=args.ollama, model=model, context_size=args.ctx, dry_run=False, think=args.think)
        with tempfile.TemporaryDirectory() as tmp:
            ctx = make_context(config, OllamaClient(config, session), tmp)
            start = time.monotonic()
            try:
                result = await Agent(ctx, build_registry()).run(case.prompt, f"bench{i}")
                answer, ok = result.answer, case.check(ctx, result.answer)
                tools = ", ".join(a["tool"] for a in result.actions) or "-"
            except Exception as err:  # noqa: BLE001
                answer, ok, tools = f"FOUT: {err}", False, "-"
            elapsed = time.monotonic() - start
            ctx.db.close()
        passed += ok
        total_time += elapsed
        print(f"{'✅' if ok else '❌'} {elapsed:5.1f}s  {case.prompt}\n      tools: {tools}\n      antwoord: "
              f"{answer[:150]!r}")
    return passed, total_time


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ollama", default="http://localhost:11434")
    parser.add_argument("--models", nargs="+", default=["qwen3:8b"])
    parser.add_argument("--ctx", type=int, default=12288)
    parser.add_argument("--think", action="store_true")
    args = parser.parse_args()

    async def no_sleep(ctx, entity_ids):
        return [ctx.index.get(e).compact() for e in entity_ids if ctx.index.get(e)]
    actions._states_after = no_sleep  # the fake HA never changes state

    results = {}
    async with aiohttp.ClientSession() as session:
        for model in args.models:
            results[model] = await bench_model(session, args, model)
    print("\n=== Resultaat ===")
    for model, (passed, total) in results.items():
        print(f"{model:30s} {passed}/{len(CASES)} goed, gemiddeld {total / len(CASES):.1f}s per vraag")


if __name__ == "__main__":
    asyncio.run(main())
