"""Setup page: pick narration / clips / reference / project name."""

from __future__ import annotations

import shutil
from pathlib import Path

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QDragEnterEvent, QDropEvent
from PyQt6.QtWidgets import (
    QAbstractItemView, QCheckBox, QFileDialog, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMessageBox, QPushButton, QVBoxLayout, QWidget,
)

from gui import config_store


VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"}


def _default_projects_root() -> Path:
    docs = Path.home() / "Documents" / "ShortsEditor" / "projects"
    return docs


class ClipDropList(QListWidget):
    """QListWidget that accepts dropped video files and lists them."""

    files_changed = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.setAcceptDrops(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setStyleSheet(
            "QListWidget { background:#1c2029; border:2px dashed #2a2f3c;"
            " border-radius:8px; padding:8px; min-height:120px; }"
            "QListWidget::item { padding:6px; }"
            "QListWidget::item:selected { background:#1e3a26; }"
        )

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # type: ignore[override]
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:  # type: ignore[override]
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:  # type: ignore[override]
        added = 0
        for url in event.mimeData().urls():
            path = Path(url.toLocalFile())
            if path.is_dir():
                for p in sorted(path.iterdir()):
                    if p.suffix.lower() in VIDEO_EXTS and self._add_path(p):
                        added += 1
            elif path.suffix.lower() in VIDEO_EXTS:
                if self._add_path(path):
                    added += 1
        if added:
            self.files_changed.emit()
        event.acceptProposedAction()

    def _add_path(self, path: Path) -> bool:
        text = str(path.resolve())
        if any(self.item(i).text() == text for i in range(self.count())):
            return False
        QListWidgetItem(text, self)
        return True

    def add_files(self, paths: list[str]) -> None:
        added = 0
        for raw in paths:
            p = Path(raw)
            if p.suffix.lower() not in VIDEO_EXTS:
                continue
            if self._add_path(p):
                added += 1
        if added:
            self.files_changed.emit()

    def remove_selected(self) -> None:
        for item in self.selectedItems():
            self.takeItem(self.row(item))
        self.files_changed.emit()

    def clear_all(self) -> None:
        super().clear()
        self.files_changed.emit()

    def all_paths(self) -> list[str]:
        return [self.item(i).text() for i in range(self.count())]


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

        # Source clips: drag-drop list + add/remove buttons.
        clips_label = QLabel("소스 영상 (여기로 끌어다 놓거나 + 버튼으로 추가)")
        clips_label.setProperty("class", "fieldLabel")
        layout.addWidget(clips_label)
        self.clips_list = ClipDropList()
        self.clips_count = QLabel("0개 파일")
        self.clips_count.setProperty("class", "sectionSub")
        self.clips_list.files_changed.connect(self._update_clips_count)
        layout.addWidget(self.clips_list)
        clips_btns = QHBoxLayout()
        clips_btns.addWidget(self.clips_count, 1)
        btn_add_clips = QPushButton("+ 영상 파일 추가")
        btn_add_clips.setProperty("class", "secondary")
        btn_add_clips.clicked.connect(self._pick_clips_files)
        btn_remove_clips = QPushButton("선택 제거")
        btn_remove_clips.setProperty("class", "secondary")
        btn_remove_clips.clicked.connect(self.clips_list.remove_selected)
        btn_clear_clips = QPushButton("전체 비우기")
        btn_clear_clips.setProperty("class", "secondary")
        btn_clear_clips.clicked.connect(self.clips_list.clear_all)
        clips_btns.addWidget(btn_add_clips)
        clips_btns.addWidget(btn_remove_clips)
        clips_btns.addWidget(btn_clear_clips)
        layout.addLayout(clips_btns)

        self.ref_edit = QLineEdit()
        self.ref_edit.setReadOnly(True)
        self.ref_edit.setPlaceholderText("벤치마킹할 영상을 첨부해주세요! (.mp4)")
        ref_hint = QLabel("✨ 이 영상의 컷/페이싱/자막 스타일을 따라가서 새 영상을 만들어요.")
        ref_hint.setStyleSheet("color:#8aa; font-size:11px; padding-top:2px;")
        btn_ref = QPushButton("파일 선택")
        btn_ref.setProperty("class", "secondary")
        btn_ref.clicked.connect(self._pick_reference)
        btn_ref_clear = QPushButton("지우기")
        btn_ref_clear.setProperty("class", "secondary")
        btn_ref_clear.clicked.connect(lambda: self.ref_edit.clear())
        layout.addLayout(self._field_with_buttons("참조 영상 (벤치마킹할 잘된 영상)",
                                                  self.ref_edit, [btn_ref, btn_ref_clear]))
        layout.addWidget(ref_hint)

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
        self._update_clips_count()

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
        # Restore individual clip file paths if we saved them last time.
        # Drop entries whose files no longer exist (renamed/moved).
        saved_clips: list[str] = last.get("clip_files") or []
        existing = [p for p in saved_clips if Path(p).exists()]
        if existing:
            self.clips_list.add_files(existing)
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

    def _update_clips_count(self) -> None:
        n = self.clips_list.count()
        self.clips_count.setText(f"{n}개 파일")

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

    def _pick_clips_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "소스 영상 파일 선택 (여러 개 가능)", "",
            "Video (*.mp4 *.mov *.mkv *.webm *.m4v *.avi)"
        )
        if paths:
            self.clips_list.add_files(paths)

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

    def _stage_clips(self, clip_paths: list[str], project_root: Path) -> Path:
        """Copy/hardlink the chosen clip files into project_root/source_clips/.

        The downstream pipeline takes a directory, so we materialize the
        user's drag-dropped list as a real folder. Hardlinks first (instant,
        zero extra disk) and fall back to shutil.copy2 when the file lives
        on a different volume or the FS doesn't support links.
        """
        staging = project_root / "source_clips"
        staging.mkdir(parents=True, exist_ok=True)
        kept: set[str] = set()
        for raw in clip_paths:
            src = Path(raw)
            if not src.exists():
                continue
            dest = staging / src.name
            # If a same-named file from another folder is already staged,
            # disambiguate with a counter.
            counter = 1
            while dest.exists() and dest.resolve() != src.resolve():
                dest = staging / f"{src.stem}_{counter}{src.suffix}"
                counter += 1
            if dest.exists():
                kept.add(dest.name)
                continue
            try:
                dest.hardlink_to(src)
            except (OSError, NotImplementedError):
                try:
                    shutil.copy2(src, dest)
                except Exception as e:
                    print(f"  copy {src} -> {dest} failed: {e}")
                    continue
            kept.add(dest.name)
        # Remove any leftover files from a previous run that aren't in the
        # current list anymore so re-running with a smaller set doesn't
        # silently keep stale clips.
        for existing in staging.iterdir():
            if existing.is_file() and existing.name not in kept:
                try:
                    existing.unlink()
                except OSError:
                    pass
        return staging

    def _on_run(self) -> None:
        name = self.project_name.text().strip()
        narration = self.narration_edit.text().strip()
        clip_files = self.clips_list.all_paths()
        output = self.output_edit.text().strip()
        if not name:
            QMessageBox.warning(self, "입력 부족", "프로젝트 이름을 입력하세요.")
            return
        if not narration:
            QMessageBox.warning(self, "입력 부족", "나레이션 오디오 파일을 선택하세요.")
            return
        if not clip_files:
            QMessageBox.warning(self, "입력 부족",
                                 "소스 영상을 한 개 이상 추가하세요. (드래그하거나 + 버튼)")
            return
        if not output:
            self._refresh_default_output()
            output = self.output_edit.text().strip()

        output_path = Path(output)
        output_path.mkdir(parents=True, exist_ok=True)
        # Project folder is the parent of output (output_dir is project/output).
        project_root = output_path.parent if output_path.name == "output" else output_path
        try:
            clips_dir = self._stage_clips(clip_files, project_root)
        except Exception as e:
            QMessageBox.critical(self, "파일 복사 실패", f"소스 영상 준비 중 오류:\n{e}")
            return

        config_store.save({
            **config_store.load(),
            "project_name": name,
            "narration": narration,
            "clip_files": clip_files,
            "clips_dir": str(clips_dir),
            "reference": self.ref_edit.text().strip(),
            "output_dir": output,
        })

        self.run_requested.emit({
            "project_name": name,
            "narration": narration,
            "clips_dir": str(clips_dir),
            "reference": self.ref_edit.text().strip() or None,
            "output_dir": output,
            "use_semantic": self.semantic_chk.isChecked(),
            "use_selections": self.selections_chk.isChecked(),
            "strip_silence": self.strip_silence_chk.isChecked(),
        })
