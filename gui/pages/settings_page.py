"""Settings page: Gemini + OpenAI API keys, Whisper model size."""

from __future__ import annotations

from PyQt6.QtWidgets import (
    QComboBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPlainTextEdit,
    QPushButton, QVBoxLayout, QWidget,
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
        sub = QLabel("API 키 여러 개 등록하면 자동 로테이션으로 속도↑/503↓. OpenAI 키 넣으면 문장 계획을 GPT가 맡아요.")
        sub.setProperty("class", "sectionSub")
        layout.addWidget(title)
        layout.addWidget(sub)

        layout.addLayout(self._field("Gemini API 키 (한 줄에 하나씩, 여러 개 등록 권장)",
                                      self._build_gemini_keys_row()))
        layout.addLayout(self._field("OpenAI API 키 (Stage 1 문장 계획용, Optional)",
                                      self._build_openai_row()))
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

    def _build_gemini_keys_row(self) -> QPlainTextEdit:
        self.gemini_keys_edit = QPlainTextEdit()
        self.gemini_keys_edit.setPlaceholderText(
            "AI Studio에서 발급받은 키를 한 줄에 하나씩.\n"
            "여러 개 넣으면 요청마다 번갈아 사용해서 503/429 확률 낮아짐."
        )
        self.gemini_keys_edit.setFixedHeight(90)
        return self.gemini_keys_edit

    def _build_openai_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        self.openai_edit = QLineEdit()
        self.openai_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.openai_edit.setPlaceholderText("sk-... (platform.openai.com/api-keys)")
        row.addWidget(self.openai_edit, 1)
        toggle = QPushButton("보기")
        toggle.setProperty("class", "secondary")
        toggle.setCheckable(True)
        toggle.toggled.connect(
            lambda on: self.openai_edit.setEchoMode(
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
        keys_list = cfg.get("gemini_api_keys") or []
        if not keys_list and cfg.get("gemini_api_key"):
            keys_list = [cfg["gemini_api_key"]]
        self.gemini_keys_edit.setPlainText("\n".join(keys_list))
        self.openai_edit.setText(cfg.get("openai_api_key", ""))
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
        raw = self.gemini_keys_edit.toPlainText()
        keys = [k.strip() for k in raw.replace(",", "\n").splitlines() if k.strip()]
        # Deduplicate while preserving order.
        seen = set()
        ordered = []
        for k in keys:
            if k not in seen:
                seen.add(k)
                ordered.append(k)
        cfg["gemini_api_keys"] = ordered
        cfg["gemini_api_key"] = ordered[0] if ordered else ""  # backwards compat
        cfg["openai_api_key"] = self.openai_edit.text().strip()
        cfg["whisper_model"] = self.model_combo.currentText()
        cfg["whisper_device"] = self.device_combo.currentText()
        config_store.save(cfg)
        msg = f"Gemini 키 {len(ordered)}개 저장됨."
        if cfg["openai_api_key"]:
            msg += " OpenAI 키 연결됨 (Stage 1 GPT)."
        QMessageBox.information(self, "저장 완료", msg)
