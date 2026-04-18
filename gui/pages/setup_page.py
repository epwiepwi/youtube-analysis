"""Setup page: pick narration / clips / reference / project name."""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPushButton, QVBoxLayout, QWidget,
)

from gui import config_store


class SetupPage(QWidget):
    run_requested = pyqtSignal(dict)

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(40, 40, 40, 40)
        layout.setSpacing(14)

        title = QLabel("새 영상 만들기")
        title.setProperty("class", "sectionTitle")
        sub = QLabel("나레이션과 클립을 선택하면 AI가 편집 초안을 만들어요.")
        sub.setProperty("class", "sectionSub")
        layout.addWidget(title)
        layout.addWidget(sub)

        self.project_name = QLineEdit()
        self.project_name.setPlaceholderText("예: onion_shorts_001")
        layout.addLayout(self._field("프로젝트 이름 (CapCut 프로젝트로 저장됨)", self.project_name))

        self.narration_edit = QLineEdit()
        self.narration_edit.setReadOnly(True)
        self.narration_edit.setPlaceholderText("나레이션 오디오 (.wav, .mp3)")
        btn_narr = QPushButton("파일 선택")
        btn_narr.setProperty("class", "secondary")
        btn_narr.clicked.connect(self._pick_narration)
        layout.addLayout(self._field_with_button("나레이션 오디오", self.narration_edit, btn_narr))

        self.clips_edit = QLineEdit()
        self.clips_edit.setReadOnly(True)
        self.clips_edit.setPlaceholderText("클립이 담긴 폴더")
        btn_clips = QPushButton("폴더 선택")
        btn_clips.setProperty("class", "secondary")
        btn_clips.clicked.connect(self._pick_clips)
        layout.addLayout(self._field_with_button("소스 영상 폴더", self.clips_edit, btn_clips))

        self.ref_edit = QLineEdit()
        self.ref_edit.setReadOnly(True)
        self.ref_edit.setPlaceholderText("모방할 잘된 영상 (선택사항)")
        btn_ref = QPushButton("파일 선택")
        btn_ref.setProperty("class", "secondary")
        btn_ref.clicked.connect(self._pick_reference)
        btn_ref_clear = QPushButton("지우기")
        btn_ref_clear.setProperty("class", "secondary")
        btn_ref_clear.clicked.connect(lambda: self.ref_edit.clear())
        layout.addLayout(self._field_with_buttons("참조 영상 (Optional)",
                                                  self.ref_edit, [btn_ref, btn_ref_clear]))

        self.output_edit = QLineEdit()
        self.output_edit.setReadOnly(True)
        self.output_edit.setPlaceholderText("분석 결과 & 뷰어를 저장할 폴더")
        btn_out = QPushButton("폴더 선택")
        btn_out.setProperty("class", "secondary")
        btn_out.clicked.connect(self._pick_output)
        layout.addLayout(self._field_with_button("작업 폴더", self.output_edit, btn_out))

        self.semantic_chk = QCheckBox("Gemini 의미 매칭 사용 (권장)")
        self.semantic_chk.setChecked(True)
        layout.addWidget(self.semantic_chk)

        self.selections_chk = QCheckBox("뷰어에서 저장한 선택(selections.json) 적용해서 재생성")
        layout.addWidget(self.selections_chk)

        layout.addStretch()

        btns = QHBoxLayout()
        btns.addStretch()
        self.run_btn = QPushButton("🎬 영상 생성 시작")
        self.run_btn.setProperty("class", "primary")
        self.run_btn.clicked.connect(self._on_run)
        btns.addWidget(self.run_btn)
        layout.addLayout(btns)

        self._load_defaults()

    def _field(self, label_text: str, widget) -> QVBoxLayout:
        box = QVBoxLayout()
        box.setSpacing(4)
        lbl = QLabel(label_text)
        lbl.setProperty("class", "fieldLabel")
        box.addWidget(lbl)
        box.addWidget(widget)
        return box

    def _field_with_button(self, label_text: str, widget, button) -> QVBoxLayout:
        box = QVBoxLayout()
        box.setSpacing(4)
        lbl = QLabel(label_text)
        lbl.setProperty("class", "fieldLabel")
        box.addWidget(lbl)
        row = QHBoxLayout()
        row.addWidget(widget, 1)
        row.addWidget(button)
        box.addLayout(row)
        return box

    def _field_with_buttons(self, label_text: str, widget, buttons) -> QVBoxLayout:
        box = QVBoxLayout()
        box.setSpacing(4)
        lbl = QLabel(label_text)
        lbl.setProperty("class", "fieldLabel")
        box.addWidget(lbl)
        row = QHBoxLayout()
        row.addWidget(widget, 1)
        for b in buttons:
            row.addWidget(b)
        box.addLayout(row)
        return box

    def _load_defaults(self) -> None:
        last = config_store.load()
        self.narration_edit.setText(last.get("narration", ""))
        self.clips_edit.setText(last.get("clips_dir", ""))
        self.ref_edit.setText(last.get("reference", ""))
        self.output_edit.setText(last.get("output_dir", ""))
        self.project_name.setText(last.get("project_name", ""))

    def _pick_narration(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "나레이션 오디오 선택", "",
                                               "Audio (*.wav *.mp3 *.m4a *.flac)")
        if path:
            self.narration_edit.setText(path)

    def _pick_clips(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "클립 폴더 선택")
        if path:
            self.clips_edit.setText(path)

    def _pick_reference(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "참조 영상 선택", "",
                                               "Video (*.mp4 *.mov *.mkv *.webm)")
        if path:
            self.ref_edit.setText(path)

    def _pick_output(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "작업 폴더 선택")
        if path:
            self.output_edit.setText(path)

    def _on_run(self) -> None:
        name = self.project_name.text().strip()
        narration = self.narration_edit.text().strip()
        clips = self.clips_edit.text().strip()
        output = self.output_edit.text().strip()
        if not (name and narration and clips and output):
            QMessageBox.warning(self, "입력 부족",
                                "프로젝트 이름, 나레이션, 클립 폴더, 작업 폴더를 모두 지정하세요.")
            return

        config_store.save({
            "project_name": name,
            "narration": narration,
            "clips_dir": clips,
            "reference": self.ref_edit.text().strip(),
            "output_dir": output,
            **{k: v for k, v in config_store.load().items()
               if k not in {"project_name", "narration", "clips_dir", "reference", "output_dir"}},
        })

        Path(output).mkdir(parents=True, exist_ok=True)
        self.run_requested.emit({
            "project_name": name,
            "narration": narration,
            "clips_dir": clips,
            "reference": self.ref_edit.text().strip() or None,
            "output_dir": output,
            "use_semantic": self.semantic_chk.isChecked(),
            "use_selections": self.selections_chk.isChecked(),
        })
