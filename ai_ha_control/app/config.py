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

        cfg.ha_token = os.environ.get("HA_TOKEN") or os.environ.get("SUPERVISOR_TOKEN", "")
        cfg.ha_url = os.environ.get("HA_URL", cfg.ha_url)
        cfg.data_dir = os.environ.get("DATA_DIR", cfg.data_dir)
        cfg.port = int(os.environ.get("PORT", cfg.port))
        if os.environ.get("OLLAMA_URL"):
            cfg.ollama_url = os.environ["OLLAMA_URL"]
        return cfg
