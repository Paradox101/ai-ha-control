"""Add-on configuration.

Inside Home Assistant the options come from /data/options.json and the
Supervisor token from the environment. For local development everything can be
overridden with environment variables (HA_URL, HA_TOKEN, OPTIONS_FILE, DATA_DIR).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

S6_ENV_DIR = Path("/run/s6/container_environment")


def container_env(name: str) -> str:
    """Read a container variable, also when s6-overlay started us with a clean environment."""
    value = os.environ.get(name)
    if value:
        return value
    try:
        return (S6_ENV_DIR / name).read_text().strip()
    except OSError:
        return ""


def supervisor_token() -> str:
    return container_env("SUPERVISOR_TOKEN") or container_env("HASSIO_TOKEN")


@dataclass
class Config:
    ollama_url: str = "http://localhost:11434"
    model: str = "qwen3:8b"
    vision_model: str = "qwen3-vl:4b"
    context_size: int = 12288
    keep_alive: str = "30m"
    think: bool = False
    language: str = "nl"
    dry_run: bool = True
    only_exposed_entities: bool = False
    confirm_domains: list[str] = field(
        default_factory=lambda: ["lock", "alarm_control_panel", "cover", "valve"]
    )
    deny_domains: list[str] = field(
        default_factory=lambda: ["shell_command", "hassio", "recorder", "python_script"]
    )
    deny_services: list[str] = field(
        default_factory=lambda: [
            "homeassistant.restart",
            "homeassistant.stop",
            "homeassistant.reload_all",
        ]
    )
    max_tool_calls: int = 8
    # Proactive behaviour
    notify_service: str = ""
    proactive: bool = True
    camera_alerts: str = "away"  # off | away | always
    open_window_minutes: int = 10
    unlocked_away_minutes: int = 15
    daily_summary_time: str = "07:30"
    quiet_hours: str = "23:00-07:00"
    routine_learning: bool = True
    tts_entity: str = ""
    announce_players: list[str] = field(default_factory=list)
    # Autonomous agent
    autonomous: bool = True
    autonomy: list[str] = field(
        default_factory=lambda: [
            "light=act", "climate=act", "fan=act", "switch=ask", "media_player=ask", "cover=ask",
            "lock=ask", "alarm_control_panel=ask", "valve=ask",
        ]
    )
    kill_switch: str = "input_boolean.ai_autonoom"
    max_climate_change: float = 2.0
    max_autonomous_actions_per_hour: int = 10
    triage_model: str = ""
    heartbeat_minutes: int = 10
    sleep_hours: str = "23:00-07:00"
    camera_patrol: str = "away"  # off | away | always
    # Per camera what's worth a notification: "camera.x=auto|door|private|street|off"
    camera_profiles: list[str] = field(default_factory=list)
    patrol_minutes: int = 15
    snapshot_retention_days: int = 7
    # Scenes/scripts the agent may start by itself (all others: ask)
    autonomous_scenes: list[str] = field(default_factory=list)
    override_minutes: int = 120  # after a resident changes something, the agent keeps its hands off
    # Comfort
    comfort: bool = True
    preheat_distance_km: float = 15.0
    # Safety playbooks (never doors or the alarm)
    leak_valve: str = ""
    emergency_lights: bool = True
    emergency_fans_off: list[str] = field(default_factory=list)
    # Notifications per person ("person.vincent=notify.mobile_app_vincent") and an evening digest
    person_notify: list[str] = field(default_factory=list)
    digest_time: str = ""
    llm_down_minutes: int = 30
    # Critical notifications without "Seen" are repeated to everybody after this many minutes (0 = off)
    escalate_minutes: int = 5
    # When the house goes to sleep: one overview of open windows/doors, locks and lights still on
    bedtime_check: bool = True
    # Washer/dryer/dishwasher done (by power use) and water that never stops running (0 = off)
    appliance_alerts: bool = True
    leak_flow_hours: float = 2.0
    # Monday morning: broken automations, updates, dead devices (only when there is something)
    health_report: bool = True
    # On vacation: lights on and off in the evening like the house is normally used (opt-in)
    presence_simulation: bool = False
    calendar_context: bool = True
    energy_context: bool = True
    api_key: str = ""
    log_level: str = "info"

    # Not user-configurable via options.json
    ha_url: str = "http://supervisor/core"
    ha_token: str = ""
    data_dir: str = "/data"
    port: int = 8099

    @property
    def ha_ws_url(self) -> str:
        base = self.ha_url.rstrip("/")
        if base.startswith("https://"):
            base = "wss://" + base[len("https://"):]
        elif base.startswith("http://"):
            base = "ws://" + base[len("http://"):]
        return f"{base}/websocket" if base.endswith("/core") else f"{base}/api/websocket"

    @classmethod
    def load(cls) -> "Config":
        cfg = cls()
        options_file = Path(os.environ.get("OPTIONS_FILE", "/data/options.json"))
        if options_file.exists():
            options = json.loads(options_file.read_text())
            for key, value in options.items():
                if hasattr(cfg, key) and value is not None:
                    setattr(cfg, key, value)

        cfg.ha_token = os.environ.get("HA_TOKEN") or supervisor_token()
        cfg.ha_url = os.environ.get("HA_URL", cfg.ha_url)
        cfg.data_dir = os.environ.get("DATA_DIR", cfg.data_dir)
        cfg.port = int(os.environ.get("PORT", cfg.port))
        if os.environ.get("OLLAMA_URL"):
            cfg.ollama_url = os.environ["OLLAMA_URL"]
        return cfg
