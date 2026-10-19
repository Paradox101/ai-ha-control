"""Replay real heartbeats of your own house against one or more models, to compare them before switching.

Copy the database from the add-on (/addon_configs/…/ai_ha_control.db or /data inside the container), then:

    python scripts/replay.py --db ai_ha_control.db --ollama http://192.168.1.10:11434 --models qwen3:8b qwen3:4b

Every stored situation report is given to the model with the autonomous tool set. Nothing is sent to Home
Assistant: tool calls go to a fake client. Scored per run:
- 👍 from the resident: the model should do the same kind of thing (act / report / nothing) as back then;
- 👎 from the resident: the model should do something else;
- no feedback: agreement with what the agent did back then (for information).
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
import tempfile
import time
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "ai_ha_control"), str(ROOT)]

from app.agent.autonomous import SYSTEM_PROMPT  # noqa: E402
from app.agent.loop import run_tool_loop  # noqa: E402
from app.config import Config  # noqa: E402
from app.llm.ollama import OllamaClient  # noqa: E402
from app.storage.db import Database  # noqa: E402
from app.tools.home import build_autonomous_registry  # noqa: E402
from scripts.benchmark import make_context  # noqa: E402

ACT_TOOLS = {"control_light", "set_climate", "turn_on_off", "call_service"}
REPORT_TOOLS = {"open_incident", "update_incident", "notify_user"}


def kind(tools: list[str]) -> str:
    """What a heartbeat amounted to: acting, reporting or nothing."""
    if ACT_TOOLS & set(tools):
        return "handelen"
    return "melden" if REPORT_TOOLS & set(tools) else "niets"


async def replay(session: aiohttp.ClientSession, args: argparse.Namespace, model: str,
                 runs: list[dict]) -> tuple[int, int, int, int, float]:
    judged = right = agree = 0
    elapsed = 0.0
    print(f"\n=== {model} ===")
    for run in runs:
        config = Config(ollama_url=args.ollama, model=model, context_size=args.ctx, dry_run=True, think=args.think)
        with tempfile.TemporaryDirectory() as tmp:
            ctx = make_context(config, OllamaClient(config, session), tmp)
            messages = [{"role": "system", "content": SYSTEM_PROMPT.format(autonomy="alleen melden (replay)")},
                        {"role": "user", "content": run["report"]}]
            start = time.monotonic()
            try:
                result = await run_tool_loop(ctx, build_autonomous_registry(), messages, model=model)
                tools = [a["tool"] for a in result.actions]
            except Exception as err:  # noqa: BLE001
                tools = [f"FOUT: {err}"]
            elapsed += time.monotonic() - start
            ctx.db.close()
        now, then = ("fout" if tools and tools[0].startswith("FOUT") else kind(tools)), kind(run["tools"])
        same = now == then
        agree += same
        mark = "·"
        if run.get("feedback"):
            judged += 1
            ok = same if run["feedback"] > 0 else not same
            right += ok
            mark = "✅" if ok else "❌"
        print(f"{mark} run {run['id']} ({run['trigger']}): toen {then}, nu {now}  {', '.join(tools) or '-'}")
    return judged, right, agree, len(runs), elapsed


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", required=True, help="copy of ai_ha_control.db")
    parser.add_argument("--ollama", default="http://localhost:11434")
    parser.add_argument("--models", nargs="+", default=["qwen3:8b"])
    parser.add_argument("--ctx", type=int, default=8192)
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--think", action="store_true")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as tmp:
        shutil.copy(args.db, Path(tmp) / "ai_ha_control.db")  # any file name; never touch the original
        db = Database(tmp)
        runs = db.runs_with_reports(args.limit)
        db.close()
    if not runs:
        sys.exit("Geen opgeslagen situatierapporten gevonden (pas vanaf versie 0.6.0 worden ze bewaard).")
    results = {}
    async with aiohttp.ClientSession() as session:
        for model in args.models:
            results[model] = await replay(session, args, model, runs)
    print("\n=== Resultaat ===")
    for model, (judged, right, agree, total, elapsed) in results.items():
        score = f"{right}/{judged} goed beoordeeld" if judged else "nog geen 👍/👎 gegeven"
        print(f"{model:30s} {score}, {agree}/{total} hetzelfde als toen, gemiddeld {elapsed / total:.1f}s")


if __name__ == "__main__":
    asyncio.run(main())
