"""Qt application entry point. Launch via `python -m gui.app` or via the
PyInstaller-built ShortsEditor.exe."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def _silence_windows_subprocesses() -> None:
    """Stop Windows from flashing a console window for every ffmpeg call.

    The pipeline shells out to ffmpeg / ffprobe dozens of times per run.
    In a windowed PyInstaller build (console=False) Windows still pops a
    fresh cmd.exe for each subprocess, which steals focus and visibly
    rains windows across the desktop. CREATE_NO_WINDOW suppresses that.

    Monkey-patches subprocess.run and subprocess.Popen so every caller
    in the project — and any third-party lib we use — picks it up
    automatically without touching the call sites.
    """
    if sys.platform != "win32":
        return
    create_no_window = 0x08000000

    _original_run = subprocess.run
    _original_popen = subprocess.Popen

    def patched_run(*args, **kwargs):
        if "creationflags" not in kwargs:
            kwargs["creationflags"] = create_no_window
        return _original_run(*args, **kwargs)

    class PatchedPopen(_original_popen):  # type: ignore[misc]
        def __init__(self, *args, **kwargs):
            if "creationflags" not in kwargs:
                kwargs["creationflags"] = create_no_window
            super().__init__(*args, **kwargs)

    subprocess.run = patched_run  # type: ignore[assignment]
    subprocess.Popen = PatchedPopen  # type: ignore[assignment]


_silence_windows_subprocesses()


def _bundle_root() -> Path:
    """Return the directory that contains bundled data files.

    - PyInstaller frozen build: sys._MEIPASS (temp extraction dir).
    - Dev / pip install: the repo root one level above this file.
    """
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _setup_bundled_paths() -> None:
    """Make bundled ffmpeg discoverable by subprocess.run('ffmpeg', ...)."""
    root = _bundle_root()
    ffmpeg_dir = root / "vendor" / "ffmpeg"
    if ffmpeg_dir.exists():
        os.environ["PATH"] = str(ffmpeg_dir) + os.pathsep + os.environ.get("PATH", "")


_setup_bundled_paths()

from PyQt6.QtGui import QFont  # noqa: E402  (after PATH setup)
from PyQt6.QtWidgets import QApplication  # noqa: E402

from gui.main_window import MainWindow  # noqa: E402


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Shorts Editor")
    app.setApplicationDisplayName("Shorts Editor")

    font = QFont("Segoe UI", 10)
    app.setFont(font)

    # In frozen mode the styles.qss lives under _MEIPASS/gui/, otherwise
    # next to this file.
    candidates = [
        _bundle_root() / "gui" / "styles.qss",
        Path(__file__).with_name("styles.qss"),
    ]
    for style_path in candidates:
        if style_path.exists():
            app.setStyleSheet(style_path.read_text(encoding="utf-8"))
            break

    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
