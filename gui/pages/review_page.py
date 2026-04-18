"""Review page: embeds viewer.html so the user can pick alternatives."""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QUrl
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWidgets import (
    QFileDialog, QHBoxLayout, QLabel, QMessageBox, QPushButton,
    QVBoxLayout, QWidget,
)


class ReviewPage(QWidget):
    regenerate_requested = None  # set by MainWindow; simple attribute holder

    def __init__(self, on_regenerate=None):
        super().__init__()
        self._on_regenerate = on_regenerate
        self._output_dir: Path | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        header = QHBoxLayout()
        title = QLabel("검토 & 선택")
        title.setProperty("class", "sectionTitle")
        header.addWidget(title)
        header.addStretch()

        import_btn = QPushButton("selections.json 불러오기")
        import_btn.setProperty("class", "secondary")
        import_btn.clicked.connect(self._import_selections)
        header.addWidget(import_btn)

        regen_btn = QPushButton("선택대로 CapCut 재생성")
        regen_btn.setProperty("class", "primary")
        regen_btn.clicked.connect(self._trigger_regenerate)
        header.addWidget(regen_btn)
        layout.addLayout(header)

        self.hint = QLabel("뷰어가 여기에 표시돼요. 썸네일 클릭으로 선택 → 다운로드 버튼으로 selections.json 저장 → 위 버튼으로 재생성.")
        self.hint.setProperty("class", "sectionSub")
        layout.addWidget(self.hint)

        self.web = QWebEngineView()
        layout.addWidget(self.web, 1)

    def load_viewer(self, output_dir: Path) -> None:
        self._output_dir = Path(output_dir)
        viewer = self._output_dir / "viewer.html"
        if not viewer.exists():
            self.hint.setText(f"아직 viewer.html이 없어요. 먼저 영상 생성을 완료하세요.  ({viewer})")
            return
        self.web.load(QUrl.fromLocalFile(str(viewer.resolve())))
        self.hint.setText(
            "썸네일 클릭 = 선택. '전체 풀에서 고르기'로 모든 장면 탐색. "
            "맨 아래 'selections.json 다운로드' 누르고 위의 '불러오기' 버튼으로 가져오세요."
        )

    def _import_selections(self) -> None:
        if not self._output_dir:
            QMessageBox.warning(self, "안내", "먼저 영상 생성을 한 번 완료하세요.")
            return
        path, _ = QFileDialog.getOpenFileName(self, "selections.json 선택", "",
                                               "JSON (*.json)")
        if not path:
            return
        dest = self._output_dir / "selections.json"
        dest.write_bytes(Path(path).read_bytes())
        QMessageBox.information(self, "완료", f"{dest}에 저장됐어요. 이제 재생성하세요.")

    def _trigger_regenerate(self) -> None:
        if not self._output_dir:
            QMessageBox.warning(self, "안내", "먼저 영상 생성을 한 번 완료하세요.")
            return
        if not (self._output_dir / "selections.json").exists():
            QMessageBox.warning(self, "선택 파일 없음",
                                "selections.json이 작업 폴더에 없어요. 뷰어에서 선택 후 '불러오기'로 저장하세요.")
            return
        if self._on_regenerate:
            self._on_regenerate()
