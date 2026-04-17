from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


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

WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "large-v3")
WHISPER_DEVICE = os.environ.get("WHISPER_DEVICE", "auto")
WHISPER_COMPUTE = os.environ.get("WHISPER_COMPUTE", "default")

CANVAS_WIDTH = 1080
CANVAS_HEIGHT = 1920
FPS = 30
