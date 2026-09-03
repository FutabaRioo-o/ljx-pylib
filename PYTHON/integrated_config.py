"""Configuration for the Raspberry Pi integrated HMI.

The legacy PC_MAIN application stored its runtime values in a Windows XML
file and changed network adapters through WMI.  The Raspberry Pi HMI keeps
only application settings here; operating-system network configuration stays
under the control of the device administrator.
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_ENV = "INTEGRATED_HMI_CONFIG"
DEFAULT_CONFIG_PATH = SCRIPT_DIR / "integrated_hmi_config.json"

DEFAULT_CONFIG: dict[str, Any] = {
    "network": {
        "raspberry_pi_ip": "192.168.4.1",
        "tablet_ip": "192.168.4.100",
        "hmi_host": "0.0.0.0",
        "hmi_port": 8000,
    },
    "main_controller": {
        "enabled": False,
        "host": "",
        # PC_MAIN used TCP client mode.  The PCB joins the Raspberry Pi Wi-Fi
        # network, but its address must be configured explicitly by the site.
        "port": 1200,
        "mode": "client",
        "connect_timeout_seconds": 2.0,
        "protocol": "legacy-16-byte-aa-function-payload-ff",
    },
    "measurement": {
        "scale_k": 0.0004,
        "offset_b": -330.94,
        "limit": 50000,
        "precision": 1,
        "unit": "g",
    },
    "motion": {
        "a_position_scale": 1.0,
        "b_steps_per_unit": 1.0,
        "c_steps_per_unit": 1.0,
        "b_default_speed": 8000,
        "c_default_speed": 8000,
    },
    "workflow": {
        "startup_wait_seconds": 1.0,
        "a_target_position": 180,
        "a_speed": 30000,
        "b_scan_steps": 0,
        "c_scan_steps": 0,
        "b_stress_steps": 0,
        "c_stress_steps": 0,
        "scan_profiles": 100,
        "scan_timeout_seconds": 15.0,
        "scan_sample_interval_seconds": 0.1,
    },
}


def config_path() -> Path:
    """Return the explicitly selected configuration file, if any."""
    return Path(os.environ.get(CONFIG_ENV, str(DEFAULT_CONFIG_PATH))).expanduser().resolve()


def _merge(default: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(default)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(path: Path | None = None) -> dict[str, Any]:
    """Load user configuration without ever filling in an unknown controller IP."""
    selected_path = path or config_path()
    if not selected_path.exists():
        return copy.deepcopy(DEFAULT_CONFIG)
    try:
        raw = json.loads(selected_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"配置文件 JSON 格式错误：{selected_path}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"配置文件根节点必须是对象：{selected_path}")
    return _merge(DEFAULT_CONFIG, raw)


def public_config(config: dict[str, Any]) -> dict[str, Any]:
    """Return only settings that are safe and useful for the HMI to display."""
    return {
        "network": dict(config["network"]),
        "main_controller": {
            key: config["main_controller"][key]
            for key in ("enabled", "host", "port", "mode", "protocol")
        },
        "measurement": dict(config["measurement"]),
        "motion": dict(config["motion"]),
        "workflow": dict(config["workflow"]),
        "config_path": str(config_path()),
    }
