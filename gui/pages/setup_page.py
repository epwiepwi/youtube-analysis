"""Setup page: pick narration / clips / reference / project name."""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPushButton, QVBoxLayout, QWidget,
)

from gui import config_store


def _default_projects_root() -> Path:
    docs = Path.home() / "Documents" / "ShortsEditor" / "projects"
    return docs


class SetupPage(QWidget):
    run_requested = pyqtSignal(dict)

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(40, 40, 40, 40)
        layout.setSpacing(14)

        title = QLabel("새 영상 만들기")
        title.setProperty("class", "sectionTitle")
        sub = QLabel("프로젝트 이름만 입력하면 Documents\\ShortsEditor\\projects\\ 아래에 자동 폴더가 생성됩니다.")
        sub.setProperty("class", "sectionSub")
        layout.addWidget(title)
        layout.addWidget(sub)

        self.project_name = QLineEdit()
        self.project_name.setPlaceholderText("예: onion_shorts_001")
        self.project_name.textChanged.connect(self._on_project_name_changed)
        layout.addLayout(self._field("프로젝트 이름 (CapCut 프로젝트로도 저장됨)", self.project_name))

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
        self.output_edit.setPlaceholderText("자동: Documents\\ShortsEditor\\projects\\<이름>\\output")
        btn_out = QPushButton("폴더 선택")
        btn_out.setProperty("class", "secondary")
        btn_out.clicked.connect(self._pick_output)
        btn_out_reset = QPushButton("기본값")
        btn_out_reset.setProperty("class", "secondary")
        btn_out_reset.clicked.connect(self._reset_output_to_default)
        layout.addLayout(self._field_with_buttons("작업 폴더 (분석 결과 저장 위치)",
                                                   self.output_edit, [btn_out, btn_out_reset]))

        self.semantic_chk = QCheckBox("Gemini 의미 매칭 사용 (권장)")
        self.semantic_chk.setChecked(True)
        layout.addWidget(self.semantic_chk)

        self.strip_silence_chk = QCheckBox("나레이션 무음 구간 전부 제거 (권장)")
        self.strip_silence_chk.setChecked(True)
        layout.addWidget(self.strip_silence_chk)

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

        self._user_touched_output = False
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
        self.project_name.setText(last.get("project_name", ""))
        saved_output = last.get("output_dir", "")
        # If the saved output is under Desktop (the old default), quietly
        # migrate the user to the new Documents location.
        desktop = str(Path.home() / "Desktop")
        if saved_output and desktop.lower() in saved_output.lower():
            saved_output = ""
        self.output_edit.setText(saved_output)
        if not saved_output:
            self._refresh_default_output()

    def _on_project_name_changed(self, _text: str) -> None:
        if not self._user_touched_output:
            self._refresh_default_output()

    def _refresh_default_output(self) -> None:
        name = self.project_name.text().strip()
        if not name:
            self.output_edit.setText("")
            return
        default = _default_projects_root() / name / "output"
        self.output_edit.setText(str(default))

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
        start = self.output_edit.text() or str(_default_projects_root())
        path = QFileDialog.getExistingDirectory(self, "작업 폴더 선택", start)
        if path:
            self.output_edit.setText(path)
            self._user_touched_output = True

    def _reset_output_to_default(self) -> None:
        self._user_touched_output = False
        self._refresh_default_output()

    def _on_run(self) -> None:
        name = self.project_name.text().strip()
        narration = self.narration_edit.text().strip()
        clips = self.clips_edit.text().strip()
        output = self.output_edit.text().strip()
        if not name:
            QMessageBox.warning(self, "입력 부족", "프로젝트 이름을 입력하세요.")
            return
        if not narration:
            QMessageBox.warning(self, "입력 부족", "나레이션 오디오 파일을 선택하세요.")
            return
        if not clips:
            QMessageBox.warning(self, "입력 부족", "소스 영상 폴더를 선택하세요.")
            return
        if not output:
            # No output chosen and name is set — fill with default.
            self._refresh_default_output()
            output = self.output_edit.text().strip()

        config_store.save({
            **config_store.load(),
            "project_name": name,
            "narration": narration,
            "clips_dir": clips,
            "reference": self.ref_edit.text().strip(),
            "output_dir": output,
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
            "strip_silence": self.strip_silence_chk.isChecked(),
        })
