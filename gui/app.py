"""Qt application entry point. Launch via `python -m gui.app` or via the
PyInstaller-built ShortsEditor.exe."""

from __future__ import annotations

import os
import sys
from pathlib import Path


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
