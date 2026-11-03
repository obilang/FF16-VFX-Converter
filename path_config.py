"""Path settings for the viewers and standalone tools; no Qt required.

The VFXB viewer and shader tools use config.json; the TLB viewer keeps its own
tlb_config.json so timelines can pack to a separate PackBack / PAC archive.
"""

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

TLB_CONFIG_PATH = CONFIG_PATH.with_name("tlb_config.json")
TLB_PATH_KEYS = ("output_root", "packback_dir", "diff_pac", "ff16tools_cli")


def load_config(path=CONFIG_PATH, keys=PATH_KEYS):
    """Read on demand so settings changes apply without restarting tools."""
    settings = dict.fromkeys(keys, "")
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"Cannot read {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError(f"{path} must contain a JSON object")
        for key in keys:
            value = data.get(key, "")
            if not isinstance(value, str) or "\x00" in value:
                raise ValueError(f"Invalid path setting: {key}")
            settings[key] = value.strip()
    return settings


def save_config(settings, path=CONFIG_PATH, keys=PATH_KEYS):
    """Replace the config atomically, retaining any unrelated JSON keys."""
    data = {}
    if path.exists():
        load_config(path, keys)  # Validate before replacing an existing file.
        data = json.loads(path.read_text(encoding="utf-8"))
    data.update({key: settings[key].strip() for key in keys})
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def configured_path(settings, key, path=CONFIG_PATH):
    value = settings.get(key, "").strip()
    if not value:
        raise ValueError(f"Set '{key}' in Settings > Paths or {path.name}")
    result = Path(os.path.expandvars(value)).expanduser()
    if not result.is_absolute():
        result = path.parent / result
    return result.resolve()
