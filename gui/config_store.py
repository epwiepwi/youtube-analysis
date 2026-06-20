"""Persistent user settings: API key, default paths, last project."""

from __future__ import annotations

import json
import os
from pathlib import Path


def _app_data_dir() -> Path:
    base = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
    root = Path(base) if base else (Path.home() / ".config")
    path = root / "ShortsEditor"
    path.mkdir(parents=True, exist_ok=True)
    return path


SETTINGS_PATH = _app_data_dir() / "settings.json"


def load() -> dict:
    if not SETTINGS_PATH.exists():
        return {}
    try:
        return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save(data: dict) -> None:
    SETTINGS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def get(key: str, default=None):
    return load().get(key, default)


def set_value(key: str, value) -> None:
    data = load()
    data[key] = value
    save(data)
