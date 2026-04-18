"""Progress page: shows real-time status while the pipeline runs."""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QTextCursor
from PyQt6.QtWidgets import (
    QHBoxLayout, QLabel, QProgressBar, QPushButton, QTextEdit,
    QVBoxLayout, QWidget,
)


class ProgressPage(QWidget):
    cancel_requested = pyqtSignal()
    review_requested = pyqtSignal()

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(40, 40, 40, 40)
        layout.setSpacing(16)

        title = QLabel("영상 생성 중...")
        title.setProperty("class", "sectionTitle")
        self.status_label = QLabel("준비 중")
        self.status_label.setProperty("class", "sectionSub")
        layout.addWidget(title)
        layout.addWidget(self.status_label)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        layout.addWidget(self.progress)

        self.log_area = QTextEdit()
        self.log_area.setObjectName("LogArea")
        self.log_area.setReadOnly(True)
        layout.addWidget(self.log_area, 1)

        btns = QHBoxLayout()
        btns.addStretch()
        self.cancel_btn = QPushButton("취소")
        self.cancel_btn.setProperty("class", "secondary")
        self.cancel_btn.clicked.connect(self.cancel_requested.emit)
        btns.addWidget(self.cancel_btn)
        self.review_btn = QPushButton("📋 검토 & 선택 열기")
        self.review_btn.setProperty("class", "primary")
        self.review_btn.setEnabled(False)
        self.review_btn.clicked.connect(self.review_requested.emit)
        btns.addWidget(self.review_btn)
        layout.addLayout(btns)

    def reset(self) -> None:
        self.progress.setValue(0)
        self.status_label.setText("준비 중")
        self.log_area.clear()
        self.review_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)

    def on_progress(self, message: str, fraction: float) -> None:
        pct = int(max(0.0, min(1.0, fraction)) * 100)
        self.progress.setValue(pct)
        self.status_label.setText(message)
        self._append_log(message)

    def on_log(self, line: str) -> None:
        self._append_log(line)

    def _append_log(self, text: str) -> None:
        self.log_area.append(text)
        cursor = self.log_area.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.log_area.setTextCursor(cursor)

    def on_finished(self, project_dir: str) -> None:
        self.status_label.setText(f"완료 — CapCut 프로젝트: {project_dir}")
        self.progress.setValue(100)
        self.cancel_btn.setEnabled(False)
        self.review_btn.setEnabled(True)

    def on_error(self, message: str) -> None:
        self.status_label.setText("에러 발생")
        self._append_log("\n--- ERROR ---\n" + message)
        self.cancel_btn.setEnabled(False)
