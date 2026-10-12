"""Widgets PySide6 réutilisables : MarkdownPanel (affichage + export Word) et ExportDialog."""
from __future__ import annotations

import contextlib
import io
import urllib.parse
from pathlib import Path

from PySide6.QtCore import QFileSystemWatcher, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QDesktopServices, QKeySequence
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QFileDialog, QFormLayout, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
    QMessageBox, QPushButton, QTextBrowser, QToolBar, QVBoxLayout, QWidget,
)

from .core import MarkdownLoader, find_note
from .docx_export import QUALITY_PRESETS, ExportOptions, export_docx
from .html_view import render_html


class ExportDialog(QDialog):
    def __init__(self, parent, opts: ExportOptions):
        super().__init__(parent)
        self.setWindowTitle("Export Word")
        form = QFormLayout(self)

        self.mode = QComboBox()
        self.mode.addItem("Classique (styles Word standard)", "classic")
        self.mode.addItem("Avancé (TOC, callouts, code coloré, légendes…)", "advanced")
        self.mode.setCurrentIndex(1 if opts.mode == "advanced" else 0)

        self.quality = QComboBox()
        for key in QUALITY_PRESETS:
            self.quality.addItem(key.capitalize(), key)
        self.quality.setCurrentIndex(list(QUALITY_PRESETS).index(opts.quality))

        self.page = QComboBox()
        self.page.addItems(["A4", "Letter"])
        self.page.setCurrentText(opts.page_size)

        self.margin = QDoubleSpinBox()
        self.margin.setRange(0.5, 6)
        self.margin.setSuffix(" cm")
        self.margin.setValue(opts.margin_cm)

        self.font = QLineEdit(opts.font)
        self.toc = QCheckBox("Table des matières")
        self.toc.setChecked(opts.toc)
        self.title_page = QCheckBox("Page de titre")
        self.title_page.setChecked(opts.title_page)
        self.numbers = QCheckBox("Numéros de page")
        self.numbers.setChecked(opts.page_numbers)
        self.captions = QCheckBox("Légendes d'images (texte alternatif)")
        self.captions.setChecked(opts.captions)

        self.template = QLineEdit(str(opts.template or ""))
        browse = QPushButton("…")
        browse.clicked.connect(self._browse)
        row = QHBoxLayout()
        row.addWidget(self.template, 1)
        row.addWidget(browse)
        row_w = QWidget()
        row_w.setLayout(row)

        form.addRow("Mode :", self.mode)
        form.addRow("Qualité des images :", self.quality)
        form.addRow("Format de page :", self.page)
        form.addRow("Marges :", self.margin)
        form.addRow("Police :", self.font)
        form.addRow(self.toc)
        form.addRow(self.title_page)
        form.addRow(self.numbers)
        form.addRow(self.captions)
        form.addRow("Modèle .docx/.dotx :", row_w)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

        self.mode.currentIndexChanged.connect(self._sync)
        self._sync()

    def _sync(self):
        advanced = self.mode.currentData() == "advanced"
        for w in (self.toc, self.title_page, self.numbers, self.captions):
            w.setEnabled(advanced)

    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(self, "Modèle Word", "", "Word (*.docx *.dotx)")
        if path:
            self.template.setText(path)

    def options(self) -> ExportOptions:
        return ExportOptions(
            mode=self.mode.currentData(), quality=self.quality.currentData(),
            page_size=self.page.currentText(), margin_cm=self.margin.value(),
            font=self.font.text().strip() or "Calibri",
            toc=self.toc.isChecked(), title_page=self.title_page.isChecked(),
            page_numbers=self.numbers.isChecked(), captions=self.captions.isChecked(),
            template=Path(self.template.text()) if self.template.text().strip() else None,
        )


class MarkdownPanel(QWidget):
    """Visionneuse Markdown + export Word, utilisable seule ou dans un onglet.

    bpmn_provider : fonction sans argument retournant le chemin du .bpmn courant
    (ou None). Si fournie, un bouton « Depuis le BPMN » génère le Markdown avec
    bpmnparser.convert() puis l'affiche.
    """

    title_changed = Signal(str)

    def __init__(self, parent=None, flavor="auto", vault=None, bpmn_provider=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.doc = None
        self.path: Path | None = None
        self.history: list[Path] = []
        self.export_opts = ExportOptions()
        self.vault = vault
        self.bpmn_provider = bpmn_provider

        self.browser = QTextBrowser()
        self.browser.setOpenLinks(False)
        self.browser.anchorClicked.connect(self._on_link)
        self.status = QLabel()
        self.status.setWordWrap(True)

        self.watcher = QFileSystemWatcher(self)     # rechargement auto (édition dans Obsidian)
        self.reload_timer = QTimer(self, singleShot=True, interval=300)
        self.reload_timer.timeout.connect(self.reload)
        self.watcher.fileChanged.connect(lambda _: self.reload_timer.start())

        bar = QToolBar()
        bar.setMovable(False)
        if bpmn_provider:
            self._action(bar, "Depuis le BPMN", self.generate_from_bpmn)
            bar.addSeparator()
        self._action(bar, "Ouvrir…", self.open_dialog, QKeySequence("Ctrl+Shift+O"))
        self._action(bar, "Recharger", self.reload)
        self._action(bar, "◀ Retour", self.back)
        bar.addSeparator()
        self.flavor = QComboBox()
        for label, key in (("Auto", "auto"), ("Markdown standard", "standard"), ("Obsidian", "obsidian")):
            self.flavor.addItem(label, key)
        self.flavor.setCurrentIndex(["auto", "standard", "obsidian"].index(flavor))
        self.flavor.currentIndexChanged.connect(lambda _: self.reload())
        bar.addWidget(self.flavor)
        bar.addSeparator()
        self._action(bar, "Exporter en Word…", self.export_word)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(bar)
        layout.addWidget(self.browser, 1)
        layout.addWidget(self.status)

    def _action(self, bar, text, slot, shortcut=None):
        act = QAction(text, self)
        if shortcut:
            act.setShortcut(shortcut)
        act.triggered.connect(slot)
        bar.addAction(act)

    def _msg(self, text):
        self.status.setText(text)

    # -- BPMN -> Markdown ----------------------------------------------------

    def generate_from_bpmn(self):
        src = self.bpmn_provider() if self.bpmn_provider else None
        if not src or not Path(src).is_file():
            QMessageBox.warning(self, "Aucun BPMN", "Ouvrez d'abord un fichier BPMN dans l'onglet Export d'images.")
            return
        try:
            import bpmnparser
            out = Path(src).parent / "output-bpmn"
            with contextlib.redirect_stdout(io.StringIO()):
                bpmnparser.convert(Path(src), out)
        except Exception as exc:
            QMessageBox.critical(self, "Conversion BPMN → Markdown échouée", str(exc))
            return
        prefix = bpmnparser.slugify(Path(src).stem) + "--"
        files = sorted(out.glob(f"{prefix}*.md"))
        if not files:
            QMessageBox.warning(self, "Aucun document", "Aucun Markdown généré pour ce BPMN.")
            return
        target = files[0]
        if len(files) > 1:
            names = [f.name for f in files]
            choice, ok = QInputDialog.getItem(self, "Processus", "Document à afficher :", names, 0, False)
            if not ok:
                return
            target = out / choice
        self.history.clear()
        self.open_file(target)

    # -- chargement --------------------------------------------------------

    def open_dialog(self):
        path, _ = QFileDialog.getOpenFileName(self, "Ouvrir", str(self.path.parent if self.path else Path.cwd()),
                                              "Markdown (*.md *.markdown *.txt);;Tous (*)")
        if path:
            self.history.clear()
            self.open_file(Path(path))

    def open_file(self, path: Path):
        if self.path:
            self.watcher.removePath(str(self.path))
        self.path = path
        self.watcher.addPath(str(path))
        self.reload(keep_scroll=False)

    def reload(self, keep_scroll=True):
        if not self.path:
            return
        bar = self.browser.verticalScrollBar()
        pos = bar.value() if keep_scroll else 0
        try:
            self.doc = MarkdownLoader(self.flavor.currentData(), self.vault).load_file(self.path)
        except Exception as exc:
            QMessageBox.critical(self, "Lecture impossible", str(exc))
            return
        self.browser.setHtml(render_html(self.doc))
        bar.setValue(pos)
        self.title_changed.emit(self.doc.title + (" [Obsidian]" if self.doc.obsidian else ""))
        self._msg(f"{len(self.doc.warnings)} avertissement(s) : " + " | ".join(self.doc.warnings[:3])
                  if self.doc.warnings else str(self.path))
        if str(self.path) not in self.watcher.files():   # certains éditeurs remplacent le fichier
            self.watcher.addPath(str(self.path))

    # -- liens -------------------------------------------------------------

    def _on_link(self, url: QUrl):
        if url.scheme() == "wikilink":
            name = urllib.parse.unquote(url.toString().split(":", 1)[1])
            target = find_note(name, self.doc.base_dir, self.doc.vault_root) if self.doc else None
            if target:
                self.history.append(self.path)
                self.open_file(target)
            else:
                self._msg(f"Note introuvable : {name}")
        elif url.scheme() in ("http", "https", "mailto"):
            QDesktopServices.openUrl(url)

    def back(self):
        if self.history:
            self.open_file(self.history.pop())

    # -- export ------------------------------------------------------------

    def export_word(self):
        if not self.doc:
            QMessageBox.information(self, "Export Word", "Ouvrez d'abord un fichier Markdown.")
            return
        dlg = ExportDialog(self, self.export_opts)
        if dlg.exec() != QDialog.Accepted:
            return
        self.export_opts = dlg.options()
        default = str((self.path or Path("export")).with_suffix(".docx"))
        out, _ = QFileDialog.getSaveFileName(self, "Enregistrer", default, "Word (*.docx)")
        if not out:
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            export_docx(self.doc, out, self.export_opts)
        except Exception as exc:
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, "Export échoué", str(exc))
            return
        QApplication.restoreOverrideCursor()
        self._msg(f"Exporté : {out}")
        QDesktopServices.openUrl(QUrl.fromLocalFile(out))

    # -- glisser-déposer ---------------------------------------------------

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        urls = e.mimeData().urls()
        if urls:
            self.history.clear()
            self.open_file(Path(urls[0].toLocalFile()))
