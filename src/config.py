from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass


def _default_capcut_root() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "CapCut" / "User Data" / "Projects" / "com.lveditor.draft"
    return Path.home() / "AppData/Local/CapCut/User Data/Projects/com.lveditor.draft"


@dataclass(frozen=True)
class Paths:
    repo_root: Path
    profile: Path
    output_root: Path
    capcut_draft_root: Path


PATHS = Paths(
    repo_root=Path(__file__).resolve().parent.parent,
    profile=Path(__file__).resolve().parent.parent / "profiles" / "style_profile.json",
    output_root=Path(__file__).resolve().parent.parent / "output",
    capcut_draft_root=_default_capcut_root(),
)

WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "small")
WHISPER_DEVICE = os.environ.get("WHISPER_DEVICE", "cpu")
WHISPER_COMPUTE = os.environ.get("WHISPER_COMPUTE", "int8")

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")


def _gemini_key_pool() -> list[str]:
    """Return the ordered list of Gemini API keys from env.

    Priority: GEMINI_API_KEYS (comma/newline separated) > GEMINI_API_KEY.
    Duplicates and blanks are removed.
    """
    raw = os.environ.get("GEMINI_API_KEYS", "")
    parts: list[str] = []
    for chunk in raw.replace("\n", ",").split(","):
        key = chunk.strip()
        if key and key not in parts:
            parts.append(key)
    if GEMINI_API_KEY and GEMINI_API_KEY not in parts:
        parts.append(GEMINI_API_KEY)
    return parts


def current_gemini_keys() -> list[str]:
    """Evaluate env vars fresh every call so GUI-updated settings take effect."""
    return _gemini_key_pool()


def current_openai_key() -> str:
    return os.environ.get("OPENAI_API_KEY", "")


# Evaluated at import for back-compat but the helpers above should be
# preferred by code that may run after env vars change.
GEMINI_API_KEYS = _gemini_key_pool()
GEMINI_VISION_MODEL = os.environ.get("GEMINI_VISION_MODEL", "gemini-2.5-flash")
GEMINI_TEXT_MODEL = os.environ.get("GEMINI_TEXT_MODEL", "gemini-2.5-flash")

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_TEXT_MODEL = os.environ.get("OPENAI_TEXT_MODEL", "gpt-4o-mini")

CANVAS_WIDTH = 1080
CANVAS_HEIGHT = 1920
FPS = 30
