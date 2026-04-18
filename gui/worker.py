"""QThread worker that runs the pipeline and emits progress signals."""

from __future__ import annotations

import io
import sys
import traceback
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from PyQt6.QtCore import QObject, QThread, pyqtSignal

from src.config import PATHS
from src.main import run as pipeline_run


class _StreamToSignal(io.TextIOBase):
    def __init__(self, signal):
        super().__init__()
        self._signal = signal
        self._buffer = ""

    def write(self, s: str) -> int:
        if not s:
            return 0
        self._buffer += s
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            if line:
                self._signal.emit(line)
        return len(s)

    def flush(self) -> None:
        if self._buffer:
            self._signal.emit(self._buffer)
            self._buffer = ""


class GenerationWorker(QObject):
    progress = pyqtSignal(str, float)  # message, 0..1
    log = pyqtSignal(str)
    finished = pyqtSignal(str)  # project_dir path
    error = pyqtSignal(str)

    def __init__(self, params: dict):
        super().__init__()
        self._params = params

    def run(self) -> None:
        try:
            stream = _StreamToSignal(self.log)
            p = self._params

            def cb(msg: str, frac: float) -> None:
                self.progress.emit(msg, float(frac))

            with redirect_stdout(stream), redirect_stderr(stream):
                project_dir = pipeline_run(
                    narration=Path(p["narration"]),
                    clips_dir=Path(p["clips_dir"]),
                    project_name=p["project_name"],
                    profile_path=Path(p.get("profile") or PATHS.profile),
                    draft_root=Path(p.get("draft_root") or PATHS.capcut_draft_root),
                    output_dir=Path(p["output_dir"]),
                    use_semantic=bool(p.get("use_semantic", False)),
                    reference_video=Path(p["reference"]) if p.get("reference") else None,
                    use_selections=bool(p.get("use_selections", False)),
                    strip_silence=bool(p.get("strip_silence", True)),
                    progress=cb,
                )
            self.finished.emit(str(project_dir))
        except Exception as exc:
            tb = traceback.format_exc()
            self.error.emit(f"{exc}\n\n{tb}")
