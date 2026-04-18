"""Qt application entry point. Launch via `python -m gui.app`."""

from __future__ import annotations

import sys
from pathlib import Path

from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import QApplication

from gui.main_window import MainWindow


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Shorts Editor")
    app.setApplicationDisplayName("Shorts Editor")

    font = QFont("Segoe UI", 10)
    app.setFont(font)

    style_path = Path(__file__).with_name("styles.qss")
    if style_path.exists():
        app.setStyleSheet(style_path.read_text(encoding="utf-8"))

    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
