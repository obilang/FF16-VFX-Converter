"""Shared path settings for the viewer and standalone tools; no Qt required."""

import json
import os
import sys
from pathlib import Path

def config_path():
    # PyInstaller's __file__ points inside its bundle, not beside the EXE.
    base = Path(sys.executable) if getattr(sys, "frozen", False) else Path(__file__)
    return base.resolve().with_name("config.json")


CONFIG_PATH = config_path()
PATH_KEYS = ("output_root", "packback_dir", "diff_pac", "ff16tools_cli",
             "dxbc2spirv", "spirv_cross")


def load_config():
    """Read on demand so settings changes apply without restarting tools."""
    settings = dict.fromkeys(PATH_KEYS, "")
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"Cannot read {CONFIG_PATH}: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError(f"{CONFIG_PATH} must contain a JSON object")
        for key in PATH_KEYS:
            value = data.get(key, "")
            if not isinstance(value, str) or "\x00" in value:
                raise ValueError(f"Invalid path setting: {key}")
            settings[key] = value.strip()
    return settings


def save_config(settings):
    """Replace the config atomically, retaining any unrelated JSON keys."""
    data = {}
    if CONFIG_PATH.exists():
        load_config()  # Validate before replacing an existing file.
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    data.update({key: settings[key].strip() for key in PATH_KEYS})
    temporary = CONFIG_PATH.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    temporary.replace(CONFIG_PATH)


def configured_path(settings, key):
    value = settings.get(key, "").strip()
    if not value:
        raise ValueError(f"Set '{key}' in Settings > Paths or {CONFIG_PATH.name}")
    path = Path(os.path.expandvars(value)).expanduser()
    if not path.is_absolute():
        path = CONFIG_PATH.parent / path
    return path.resolve()
