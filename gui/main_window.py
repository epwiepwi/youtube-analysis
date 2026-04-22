"""Main window with sidebar navigation and stacked pages."""

from __future__ import annotations

import os
from pathlib import Path

from PyQt6.QtCore import Qt, QThread
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import (
    QHBoxLayout, QLabel, QMessageBox, QPushButton, QStackedWidget,
    QVBoxLayout, QWidget,
)

from gui import config_store
from gui.pages.progress_page import ProgressPage
from gui.pages.review_page import ReviewPage
from gui.pages.settings_page import SettingsPage
from gui.pages.setup_page import SetupPage
from gui.worker import GenerationWorker


class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Shorts Editor")
        self.setObjectName("CentralWidget")
        self.resize(1200, 780)

        self._thread: QThread | None = None
        self._worker: GenerationWorker | None = None
        self._last_params: dict | None = None

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_sidebar())
        root.addWidget(self._build_content(), 1)

    def _build_sidebar(self) -> QWidget:
        side = QWidget()
        side.setObjectName("Sidebar")
        v = QVBoxLayout(side)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(2)

        title = QLabel("Shorts Editor")
        title.setObjectName("AppTitle")
        subtitle = QLabel("AI 짜집기 쇼츠 어시스턴트")
        subtitle.setObjectName("AppSubtitle")
        v.addWidget(title)
        v.addWidget(subtitle)

        self._nav_buttons: dict[str, QPushButton] = {}
        for key, label in [("setup", "🎬  새 영상"),
                            ("progress", "⚙️  생성 진행"),
                            ("review", "📋  검토 & 선택"),
                            ("settings", "⚙️  설정")]:
            btn = QPushButton(label)
            btn.setObjectName("NavButton")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _=False, k=key: self._show(k))
            self._nav_buttons[key] = btn
            v.addWidget(btn)

        v.addStretch()
        return side

    def _build_content(self) -> QWidget:
        box = QWidget()
        box.setObjectName("ContentArea")
        v = QVBoxLayout(box)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        self.stack = QStackedWidget()
        self.setup_page = SetupPage()
        self.progress_page = ProgressPage()
        self.review_page = ReviewPage(on_regenerate=self._regenerate_with_selections)
        self.settings_page = SettingsPage()

        self.stack.addWidget(self.setup_page)
        self.stack.addWidget(self.progress_page)
        self.stack.addWidget(self.review_page)
        self.stack.addWidget(self.settings_page)

        self.setup_page.run_requested.connect(self._start_run)
        self.progress_page.review_requested.connect(self._open_review)
        self.progress_page.cancel_requested.connect(self._cancel)

        v.addWidget(self.stack)
        self._show("setup")
        return box

    def _show(self, key: str) -> None:
        index = {"setup": 0, "progress": 1, "review": 2, "settings": 3}[key]
        self.stack.setCurrentIndex(index)
        for k, btn in self._nav_buttons.items():
            btn.setProperty("active", "true" if k == key else "false")
            btn.style().unpolish(btn)
            btn.style().polish(btn)

    def _start_run(self, params: dict) -> None:
        cfg = config_store.load()
        api_key = cfg.get("gemini_api_key", "")
        if params.get("use_semantic") and not api_key:
            QMessageBox.warning(self, "API 키 필요",
                                "의미 매칭을 쓰려면 설정에서 Gemini API 키를 먼저 입력하세요.")
            self._show("settings")
            return

        # Propagate settings to subprocess env so src/config.py picks them up.
        if api_key:
            os.environ["GEMINI_API_KEY"] = api_key
        os.environ["WHISPER_MODEL"] = cfg.get("whisper_model") or "small"
        device = cfg.get("whisper_device") or "cpu"
        os.environ["WHISPER_DEVICE"] = device
        os.environ["WHISPER_COMPUTE"] = (
            "int8" if device == "cpu" else cfg.get("whisper_compute") or "float16"
        )

        self._last_params = params
        self.progress_page.reset()
        self._show("progress")

        self._thread = QThread(self)
        self._worker = GenerationWorker(params)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self.progress_page.on_progress)
        self._worker.log.connect(self.progress_page.on_log)
        self._worker.finished.connect(self._on_finished)
        self._worker.error.connect(self._on_error)
        self._worker.finished.connect(self._thread.quit)
        self._worker.error.connect(self._thread.quit)
        self._thread.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.start()

    def _on_finished(self, project_dir: str) -> None:
        self.progress_page.on_finished(project_dir)
        self._thread = None
        self._worker = None

    def _on_error(self, message: str) -> None:
        self.progress_page.on_error(message)
        self._thread = None
        self._worker = None

    def _open_review(self) -> None:
        if not self._last_params:
            QMessageBox.warning(self, "안내", "먼저 영상 생성을 한 번 완료하세요.")
            return
        output_dir = Path(self._last_params["output_dir"])
        self.review_page.load_viewer(output_dir)
        self._show("review")

    def _regenerate_with_selections(self) -> None:
        if not self._last_params:
            return
        params = dict(self._last_params)
        params["use_selections"] = True
        self._start_run(params)

    def _cancel(self) -> None:
        if self._thread and self._thread.isRunning():
            # Soft cancel: the worker has no interrupt hook yet, so we just
            # detach and let it finish in background.
            QMessageBox.information(self, "안내",
                                     "백그라운드 작업을 중단할 수 없어요. 대신 이 창을 닫았다 다시 열면 새 작업을 시작할 수 있습니다.")
