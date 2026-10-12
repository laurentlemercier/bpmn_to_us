"""Fenêtre autonome (PySide6) : enveloppe MarkdownPanel dans une QMainWindow."""
from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication, QMainWindow

from .panel import MarkdownPanel


class Viewer(QMainWindow):
    def __init__(self, flavor="auto", vault=None):
        super().__init__()
        self.resize(1000, 800)
        self.panel = MarkdownPanel(self, flavor, vault)
        self.panel.title_changed.connect(lambda t: self.setWindowTitle(f"{t} — Markdown Viewer"))
        self.setCentralWidget(self.panel)


def run_gui(file: Path | None = None, flavor="auto", vault=None) -> int:
    app = QApplication(sys.argv[:1])
    win = Viewer(flavor, vault)
    win.show()
    if file:
        win.panel.open_file(Path(file))
    return app.exec()
