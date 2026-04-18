"""Settings page: Gemini API key, Whisper model size."""

from __future__ import annotations

from PyQt6.QtWidgets import (
    QComboBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton,
    QVBoxLayout, QWidget,
)

from gui import config_store


class SettingsPage(QWidget):
    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(40, 40, 40, 40)
        layout.setSpacing(14)

        title = QLabel("설정")
        title.setProperty("class", "sectionTitle")
        sub = QLabel("Gemini API 키와 기본값을 저장해두면 매번 입력 안 해도 됩니다.")
        sub.setProperty("class", "sectionSub")
        layout.addWidget(title)
        layout.addWidget(sub)

        layout.addLayout(self._field("Gemini API 키 (필수)", self._build_api_row()))
        layout.addLayout(self._field("Whisper 모델 크기", self._build_model_row()))
        layout.addLayout(self._field("Whisper 실행 장치", self._build_device_row()))

        save_row = QHBoxLayout()
        save_row.addStretch()
        save_btn = QPushButton("저장")
        save_btn.setProperty("class", "primary")
        save_btn.clicked.connect(self._save)
        save_row.addWidget(save_btn)
        layout.addStretch()
        layout.addLayout(save_row)

        self._load()

    def _field(self, label_text: str, widget_or_layout) -> QVBoxLayout:
        box = QVBoxLayout()
        box.setSpacing(4)
        lbl = QLabel(label_text)
        lbl.setProperty("class", "fieldLabel")
        box.addWidget(lbl)
        if isinstance(widget_or_layout, QWidget):
            box.addWidget(widget_or_layout)
        else:
            box.addLayout(widget_or_layout)
        return box

    def _build_api_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        self.api_edit = QLineEdit()
        self.api_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_edit.setPlaceholderText("AI Studio에서 발급받은 키")
        row.addWidget(self.api_edit, 1)
        toggle = QPushButton("보기")
        toggle.setProperty("class", "secondary")
        toggle.setCheckable(True)
        toggle.toggled.connect(
            lambda on: self.api_edit.setEchoMode(
                QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password
            )
        )
        row.addWidget(toggle)
        return row

    def _build_model_row(self) -> QComboBox:
        self.model_combo = QComboBox()
        for name in ["tiny", "base", "small", "medium", "large-v3"]:
            self.model_combo.addItem(name)
        return self.model_combo

    def _build_device_row(self) -> QComboBox:
        self.device_combo = QComboBox()
        self.device_combo.addItems(["cpu", "cuda", "auto"])
        return self.device_combo

    def _load(self) -> None:
        cfg = config_store.load()
        self.api_edit.setText(cfg.get("gemini_api_key", ""))
        model = cfg.get("whisper_model", "small")
        idx = self.model_combo.findText(model)
        if idx >= 0:
            self.model_combo.setCurrentIndex(idx)
        device = cfg.get("whisper_device", "cpu")
        idx = self.device_combo.findText(device)
        if idx >= 0:
            self.device_combo.setCurrentIndex(idx)

    def _save(self) -> None:
        cfg = config_store.load()
        cfg["gemini_api_key"] = self.api_edit.text().strip()
        cfg["whisper_model"] = self.model_combo.currentText()
        cfg["whisper_device"] = self.device_combo.currentText()
        config_store.save(cfg)
        QMessageBox.information(self, "저장 완료", "설정이 저장됐습니다.")
