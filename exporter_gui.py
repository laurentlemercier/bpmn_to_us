#!/usr/bin/env python3
"""
BPMN Toolkit - Interface graphique unique (PySide6)

Onglets : BPMN → Markdown | Export d'images | Markdown / Word.
Lancée par main.py (--gui ou sans argument) ou directement.

Ancien en-tête : BPMN Exporter - Interface graphique (PySide6)

Fenêtre de sélection des options d'export de exporter.py :
    - fichier BPMN 2.0
    - mode : diagramme complet / processus / lane / tout
    - format : PNG / JPG / les deux
    - répertoire de sortie, padding, échelle
    - aperçu du rendu (QtSvg), logs de l'export
"""

import contextlib
import io
import sys
from pathlib import Path

import bpmnparser
import exporter
from mdviewer.panel import MarkdownPanel

from PySide6.QtCore import (
    Qt,
    QByteArray,
    QUrl,
    Signal,
)
from PySide6.QtGui import QDesktopServices
from PySide6.QtSvgWidgets import QSvgWidget
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSplitter,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)


MODES = ("full", "process", "lane", "all")

FORMAT_BOTH = {"png", "jpg"}

DEFAULT_OUTPUT = "bpmn_exports"


class LogStream:
    """Fait office de flux texte pour rediriger la sortie vers un QPlainTextEdit."""

    def __init__(self, widget):
        self.widget = widget

    def write(self, text):
        text = text.strip("\n")

        if text:
            self.widget.appendPlainText(text)

    def flush(self):
        pass


class ConvertPanel(QWidget):
    """Onglet BPMN → Markdown (remplace l'ancienne fenêtre Tkinter de main.py)."""

    converted = Signal(list)    # liste des Path .md générés

    def __init__(self, default_input="", default_output="output-bpmn", parent=None):
        super().__init__(parent)

        layout = QVBoxLayout(self)

        src_group = QGroupBox("Source BPMN (fichier ou dossier)")
        row = QHBoxLayout(src_group)
        self.input_edit = QLineEdit(default_input)
        file_btn = QPushButton("Fichier…")
        dir_btn = QPushButton("Dossier…")
        file_btn.clicked.connect(self._choose_file)
        dir_btn.clicked.connect(self._choose_dir)
        row.addWidget(self.input_edit, 1)
        row.addWidget(file_btn)
        row.addWidget(dir_btn)

        out_group = QGroupBox("Dossier de sortie")
        row = QHBoxLayout(out_group)
        self.output_edit = QLineEdit(default_output)
        out_btn = QPushButton("Parcourir…")
        open_btn = QPushButton("Ouvrir le dossier")
        out_btn.clicked.connect(self._choose_output)
        open_btn.clicked.connect(self._open_output)
        row.addWidget(self.output_edit, 1)
        row.addWidget(out_btn)
        row.addWidget(open_btn)

        img_group = QGroupBox("Images du diagramme")
        img_layout = QHBoxLayout(img_group)
        self.images_check = QCheckBox("Générer les PNG et les intégrer au Markdown")
        self.images_check.setChecked(True)
        self.scenarios_check = QCheckBox("Tâches de chaque scénario en évidence")
        self.scenarios_check.setChecked(True)
        self.lanes_check = QCheckBox("Une image par lane")
        self.scale_spin = QDoubleSpinBox()
        self.scale_spin.setRange(0.5, 4.0)
        self.scale_spin.setSingleStep(0.5)
        self.scale_spin.setValue(1.0)
        self.scale_spin.setPrefix("Échelle ")
        img_layout.addWidget(self.images_check, 1)
        img_layout.addWidget(self.scenarios_check)
        img_layout.addWidget(self.lanes_check)
        img_layout.addWidget(self.scale_spin)
        self.images_check.toggled.connect(self.lanes_check.setEnabled)
        self.images_check.toggled.connect(self.scenarios_check.setEnabled)
        self.images_check.toggled.connect(self.scale_spin.setEnabled)

        self.show_check = QCheckBox("Afficher le résultat dans l'onglet « Markdown / Word »")
        self.show_check.setChecked(True)

        self.convert_button = QPushButton("Convertir en Markdown")
        self.convert_button.setMinimumHeight(36)
        self.convert_button.clicked.connect(self.convert)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setPlaceholderText("Journal de conversion")

        layout.addWidget(src_group)
        layout.addWidget(out_group)
        layout.addWidget(img_group)
        layout.addWidget(self.show_check)
        layout.addWidget(self.convert_button)
        layout.addWidget(QLabel("Journal"))
        layout.addWidget(self.log_view, 1)

    # ------------------------------------------------------------------

    def _choose_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Sélectionner un fichier BPMN",
            self.input_edit.text() or str(Path.cwd()),
            "Fichiers BPMN (*.bpmn);;Tous les fichiers (*)",
        )
        if path:
            self.input_edit.setText(path)

    def _choose_dir(self):
        path = QFileDialog.getExistingDirectory(
            self, "Dossier contenant des BPMN",
            self.input_edit.text() or str(Path.cwd()),
        )
        if path:
            self.input_edit.setText(path)

    def _choose_output(self):
        path = QFileDialog.getExistingDirectory(
            self, "Dossier de sortie", self.output_edit.text() or str(Path.cwd()),
        )
        if path:
            self.output_edit.setText(path)

    def _open_output(self):
        out = Path(self.output_edit.text().strip() or "output-bpmn")
        if out.is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(out.resolve())))

    # ------------------------------------------------------------------

    def convert(self):
        input_str = self.input_edit.text().strip()
        output_str = self.output_edit.text().strip() or "output-bpmn"

        if not input_str:
            QMessageBox.warning(self, "Entrée manquante",
                                "Sélectionnez un fichier .bpmn ou un dossier.")
            return

        input_path, output_path = Path(input_str), Path(output_str)

        if not input_path.exists():
            QMessageBox.critical(self, "Chemin introuvable", f"Chemin introuvable : {input_path}")
            return

        self.log_view.clear()
        self.log_view.appendPlainText(f"▶ Entrée : {input_path}\n▶ Sortie : {output_path}\n" + "─" * 50)

        buf_out, buf_err = io.StringIO(), io.StringIO()
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
                count = bpmnparser.convert(
                    input_path, output_path,
                    images=self.images_check.isChecked(),
                    image_scale=self.scale_spin.value(),
                    lane_images=self.lanes_check.isChecked(),
                    scenario_images=self.scenarios_check.isChecked(),
                )
        except Exception as exc:
            QApplication.restoreOverrideCursor()
            self.log_view.appendPlainText(f"✗ Erreur : {exc}")
            QMessageBox.critical(self, "Erreur", str(exc))
            return
        QApplication.restoreOverrideCursor()

        out_text, err_text = buf_out.getvalue().strip(), buf_err.getvalue().strip()
        if out_text:
            self.log_view.appendPlainText(out_text)
        for line in err_text.splitlines():
            self.log_view.appendPlainText(f"⚠ {line}")

        if count == 0:
            self.log_view.appendPlainText("Aucun document généré.")
            QMessageBox.warning(self, "Terminé",
                                "Aucun document Markdown n'a été généré.\nVérifiez le fichier BPMN.")
            return

        prefix = "Généré :"
        generated = [Path(line[len(prefix):].strip())
                     for line in out_text.splitlines() if line.startswith(prefix)]
        self.log_view.appendPlainText(f"✓ {count} document(s) Markdown généré(s) dans : {output_path.resolve()}")
        self.converted.emit(generated if self.show_check.isChecked() else [])


class ExporterWindow(QMainWindow):
    def __init__(self, data_dir=None, output_dir=None):
        super().__init__()

        self._data_dir = data_dir
        self._output_dir = output_dir

        self.setWindowTitle("BPMN Toolkit")
        self.resize(1150, 740)

        self.document = None
        self._scenarios = {}

        self._build_layout()
        self._connect_signals()
        self._refresh_widget_state()

    def _current_bpmn(self):
        """BPMN courant : celui de l'onglet images, sinon la source de la conversion."""
        path = self.file_edit.text().strip()
        if path:
            return path
        source = self.convert_panel.input_edit.text().strip()
        return source if source.lower().endswith(".bpmn") else None

    # ------------------------------------------------------------------

    def _on_converted(self, files):
        if files:
            self.md_panel.history.clear()
            self.md_panel.open_file(files[0])
            self.tabs.setCurrentWidget(self.md_panel)

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def _build_layout(self):
        controls = self._build_controls()

        self.preview_scroll = QScrollArea()
        self.preview_scroll.setWidgetResizable(True)

        self.svg_widget = QSvgWidget()
        self.svg_widget.setBackgroundRole(self.preview_scroll.backgroundRole())

        self.preview_scroll.setWidget(self.svg_widget)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(controls)
        splitter.addWidget(self.preview_scroll)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([420, 730])

        # Onglet 2 : visionneuse Markdown (standard/Obsidian) + export Word.
        # Le bouton « Depuis le BPMN » convertit le fichier chargé à gauche.
        self.md_panel = MarkdownPanel(
            self, bpmn_provider=self._current_bpmn,
        )

        self.convert_panel = ConvertPanel(
            default_input=str(self._data_dir or ""),
            default_output=str(self._output_dir or "output-bpmn"),
        )
        self.convert_panel.converted.connect(self._on_converted)

        self.tabs = QTabWidget()
        self.tabs.addTab(self.convert_panel, "BPMN → Markdown")
        self.tabs.addTab(splitter, "Export d'images BPMN")
        self.tabs.addTab(self.md_panel, "Markdown / Word")

        self.setCentralWidget(self.tabs)

    # ------------------------------------------------------------------

    def _build_controls(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)

        layout.addWidget(self._build_file_group())
        layout.addWidget(self._build_content_group())
        layout.addWidget(self._build_mode_group())
        layout.addWidget(self._build_options_group())
        layout.addWidget(self._build_export_button())

        layout.addWidget(QLabel("Logs"))
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(5000)
        layout.addWidget(self.log_view, 1)

        return panel

    # ------------------------------------------------------------------

    def _build_file_group(self):
        group = QGroupBox("Fichier BPMN")
        row = QHBoxLayout(group)

        self.file_edit = QLineEdit()
        self.file_edit.setPlaceholderText("Chemin vers un fichier .bpmn")

        open_button = QPushButton("Ouvrir…")
        open_button.clicked.connect(self.choose_file)

        row.addWidget(self.file_edit, 1)
        row.addWidget(open_button)

        return group

    # ------------------------------------------------------------------

    def _build_content_group(self):
        group = QGroupBox("Contenu du diagramme (--list)")
        layout = QVBoxLayout(group)

        self.content_tree = QTreeWidget()
        self.content_tree.setHeaderHidden(True)
        self.content_tree.setRootIsDecorated(True)

        layout.addWidget(self.content_tree)

        return group

    # ------------------------------------------------------------------

    def _build_mode_group(self):
        group = QGroupBox("Mode d'export")
        layout = QVBoxLayout(group)

        self.full_radio = QRadioButton("Diagramme complet (--full)")
        self.process_radio = QRadioButton("Un processus (--process)")
        self.lane_radio = QRadioButton("Une lane (--lane)")
        self.all_radio = QRadioButton("Tout : complet + processus + lanes (--all)")

        self.full_radio.setChecked(True)

        for radio in (self.full_radio, self.process_radio,
                      self.lane_radio, self.all_radio):
            layout.addWidget(radio)

        process_row = QHBoxLayout()
        self.process_combo = QComboBox()

        process_row.addWidget(QLabel("Processus :"))
        process_row.addWidget(self.process_combo, 1)

        lane_row = QHBoxLayout()
        self.lane_combo = QComboBox()

        lane_row.addWidget(QLabel("Lane :"))
        lane_row.addWidget(self.lane_combo, 1)

        layout.addLayout(process_row)
        layout.addLayout(lane_row)

        return group

    # ------------------------------------------------------------------

    def _build_options_group(self):
        group = QGroupBox("Options d'export")
        form = QFormLayout(group)

        self.format_combo = QComboBox()
        self.format_combo.addItem("PNG", "png")
        self.format_combo.addItem("JPG", "jpg")
        self.format_combo.addItem("PNG + JPG", "both")

        output_row = QHBoxLayout()
        self.output_edit = QLineEdit(DEFAULT_OUTPUT)

        browse_button = QPushButton("Parcourir…")
        browse_button.clicked.connect(self.choose_output_dir)

        output_row.addWidget(self.output_edit, 1)
        output_row.addWidget(browse_button)

        self.padding_spin = QDoubleSpinBox()
        self.padding_spin.setRange(0, 500)
        self.padding_spin.setDecimals(1)
        self.padding_spin.setSingleStep(5)
        self.padding_spin.setValue(30)

        self.scale_spin = QDoubleSpinBox()
        self.scale_spin.setRange(0.1, 10.0)
        self.scale_spin.setDecimals(2)
        self.scale_spin.setSingleStep(0.5)
        self.scale_spin.setValue(1.0)

        self.highlight_combo = QComboBox()
        self.highlight_combo.addItem("Aucune", None)

        form.addRow("Mise en évidence :", self.highlight_combo)
        form.addRow("Format :", self.format_combo)
        form.addRow("Dossier de sortie :", output_row)
        form.addRow("Padding :", self.padding_spin)
        form.addRow("Échelle :", self.scale_spin)

        return group

    # ------------------------------------------------------------------

    def _build_export_button(self):
        self.export_button = QPushButton("Exporter")
        self.export_button.setMinimumHeight(36)
        self.export_button.clicked.connect(self.export)
        return self.export_button

    # ------------------------------------------------------------------

    def _connect_signals(self):
        self.file_edit.returnPressed.connect(self.load_file_from_field)

        for radio in (self.full_radio, self.process_radio,
                      self.lane_radio, self.all_radio):
            radio.toggled.connect(self._refresh_widget_state)
            radio.toggled.connect(self.update_preview)

        self.process_combo.currentIndexChanged.connect(self.update_preview)
        self.lane_combo.currentIndexChanged.connect(self.update_preview)
        self.highlight_combo.currentIndexChanged.connect(self.update_preview)
        self.content_tree.itemSelectionChanged.connect(
            self._on_tree_selection_changed
        )

    # ------------------------------------------------------------------
    # Fichier
    # ------------------------------------------------------------------

    def choose_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Sélectionner un diagramme BPMN",
            str(Path.cwd()),
            "BPMN 2.0 (*.bpmn);;Tous les fichiers (*)",
        )

        if path:
            self.file_edit.setText(path)
            self.load_file(path)

    # ------------------------------------------------------------------

    def load_file_from_field(self):
        self.load_file(self.file_edit.text().strip())

    # ------------------------------------------------------------------

    def load_file(self, path):
        path = Path(path)

        if not path.exists():
            QMessageBox.critical(
                self, "Fichier introuvable",
                f"Le fichier n'existe pas :\n{path}",
            )
            return

        try:
            document = exporter.BPMNDocument(path)
        except Exception as exc:
            QMessageBox.critical(
                self, "Lecture du BPMN impossible",
                f"Erreur lors de la lecture du BPMN :\n{exc}",
            )
            return

        self.document = document
        self.file_edit.setText(str(path))
        self._populate_content(document)
        self.log_view.clear()
        self._log(f"Lecture : {path}")
        self._log(f"{len(document.processes)} processus, "
                  f"{len(document.lanes)} lanes.")
        self.statusBar().showMessage(str(path), 5000)

        self._refresh_widget_state()
        self.update_preview()

    # ------------------------------------------------------------------

    def _populate_content(self, document):
        self.process_combo.clear()
        self.lane_combo.clear()

        self.content_tree.clear()

        root = QTreeWidgetItem(["Processus"])
        root.setFlags(root.flags() & ~Qt.ItemIsSelectable)

        for process_id, process in document.processes.items():
            name = process.get("name") or process_id
            item = QTreeWidgetItem([name])
            item.setData(0, Qt.UserRole, ("process", process_id))
            root.addChild(item)

            self.process_combo.addItem(f"{name}  [{process_id}]", process_id)

        self.content_tree.addTopLevelItem(root)

        lane_root = QTreeWidgetItem(["Lanes"])
        lane_root.setFlags(lane_root.flags() & ~Qt.ItemIsSelectable)

        for lane_id, lane in document.lanes.items():
            name = lane.get("name") or lane_id
            item = QTreeWidgetItem([name])
            item.setData(0, Qt.UserRole, ("lane", lane_id))
            lane_root.addChild(item)

            self.lane_combo.addItem(f"{name}  [{lane_id}]", lane_id)

        self.content_tree.addTopLevelItem(lane_root)
        self.content_tree.expandAll()

        self._populate_highlights(document)

    # ------------------------------------------------------------------

    def _populate_highlights(self, document):
        """Un scénario = un jeu de tâches à mettre en évidence (cf. bpmnparser)."""

        self._scenarios = {}
        self.highlight_combo.blockSignals(True)
        self.highlight_combo.clear()
        self.highlight_combo.addItem("Aucune", None)

        try:
            models = bpmnparser.parse_bpmn(document.filename)
            multiple = len(models) > 1

            for index, model in enumerate(models, start=1):
                for scenario in bpmnparser.build_scenarios(model):
                    key = f"P{index}_{scenario.id}" if multiple else scenario.id
                    self._scenarios[key] = {t.id for t in scenario.tasks}
                    self.highlight_combo.addItem(
                        f"{key} — {scenario.title} ({len(scenario.tasks)} tâche(s))", key,
                    )
        except Exception as exc:
            self._log(f"Scénarios indisponibles : {exc}")

        self.highlight_combo.blockSignals(False)

    # ------------------------------------------------------------------

    def choose_output_dir(self):
        directory = QFileDialog.getExistingDirectory(
            self,
            "Dossier de sortie",
            self.output_edit.text() or str(Path.cwd()),
        )

        if directory:
            self.output_edit.setText(directory)

    # ------------------------------------------------------------------
    # État des widgets
    # ------------------------------------------------------------------

    def _refresh_widget_state(self):
        loaded = self.document is not None
        n_processes = len(self.document.processes) if loaded else 0
        n_lanes = len(self.document.lanes) if loaded else 0

        mode = self.current_mode()

        self.export_button.setEnabled(loaded)
        self.process_combo.setEnabled(loaded and mode == "process" and n_processes > 0)
        self.lane_combo.setEnabled(loaded and mode == "lane" and n_lanes > 0)

        self.process_radio.setEnabled(loaded and n_processes > 0)
        self.lane_radio.setEnabled(loaded and n_lanes > 0)

        if mode == "process" and self.process_radio.isEnabled() is False:
            self.full_radio.setChecked(True)

        if mode == "lane" and self.lane_radio.isEnabled() is False:
            self.full_radio.setChecked(True)

    # ------------------------------------------------------------------

    def current_mode(self):
        if self.process_radio.isChecked():
            return "process"
        if self.lane_radio.isChecked():
            return "lane"
        if self.all_radio.isChecked():
            return "all"
        return "full"

    # ------------------------------------------------------------------
    # Aperçu
    # ------------------------------------------------------------------

    def update_preview(self):
        if self.document is None:
            self.svg_widget.load("<svg xmlns='http://www.w3.org/2000/svg'/>")
            return

        element_ids = self._selection_element_ids()

        if element_ids is False:
            self.svg_widget.load("<svg xmlns='http://www.w3.org/2000/svg'/>")
            return

        try:
            renderer = exporter.SVGRenderer(
                self.document,
                padding=self.padding_spin.value(),
                scale=1.0,
            )
            svg = renderer.render(element_ids, self._highlight_ids())
        except Exception as exc:
            self._log(f"Aperçu impossible : {exc}")
            return

        self.svg_widget.load(QByteArray(svg.encode("utf-8")))

    # ------------------------------------------------------------------

    def _highlight_ids(self):
        key = self.highlight_combo.currentData()
        return self._scenarios.get(key) if key else None

    # ------------------------------------------------------------------

    def _selection_element_ids(self):
        """Retourne les ids à afficher pour le mode courant.

        None => diagramme complet.
        False => sélection incomplète (pas de preview).
        set(...) => éléments du processus / de la lane.
        """

        mode = self.current_mode()

        if mode in ("full", "all"):
            return None

        if mode == "process":
            process_id = self.process_combo.currentData()

            if process_id is None:
                return False

            return self.document.process_elements(process_id)

        lane_id = self.lane_combo.currentData()

        if lane_id is None:
            return False

        return self.document.lane_elements(lane_id)

    # ------------------------------------------------------------------

    def _on_tree_selection_changed(self):
        items = self.content_tree.selectedItems()

        if not items:
            return

        data = items[0].data(0, Qt.UserRole)

        if data is None:
            return

        kind, element_id = data

        if kind == "process":
            index = self.process_combo.findData(element_id)

            if index >= 0:
                self.process_combo.setCurrentIndex(index)

            self.process_radio.setChecked(True)

        elif kind == "lane":
            index = self.lane_combo.findData(element_id)

            if index >= 0:
                self.lane_combo.setCurrentIndex(index)

            self.lane_radio.setChecked(True)

        # setCurrentIndex()/setChecked() ne déclenchent leurs signaux que
        # si la valeur change réellement (ex : ré-sélectionner le même
        # élément) ; on force donc explicitement le rafraîchissement de
        # l'aperçu pour rester toujours synchronisé avec la sélection.
        self.update_preview()

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    def export(self):
        if self.document is None:
            QMessageBox.warning(
                self, "Aucun diagramme",
                "Ouvrez d'abord un fichier BPMN.",
            )
            return

        targets = self._build_targets()

        if not targets:
            QMessageBox.warning(
                self, "Rien à exporter",
                "Le diagramme ne contient ni processus ni lane "
                "à exporter.",
            )
            return

        output_dir = Path(self.output_edit.text().strip() or DEFAULT_OUTPUT)

        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            QMessageBox.critical(
                self, "Dossier de sortie invalide",
                f"Impossible de créer le dossier :\n{exc}",
            )
            return

        formats = self._selected_formats()
        padding = self.padding_spin.value()
        scale = self.scale_spin.value()

        self.log_view.clear()
        self._log(f"Export vers : {output_dir}")

        stream = LogStream(self.log_view)

        try:
            with contextlib.redirect_stdout(stream):
                for element_ids, output_base in targets:
                    exporter.export_selection(
                        self.document, element_ids, output_base,
                        formats, padding, scale,
                        highlight=self._highlight_ids(),
                    )
        except Exception as exc:
            self._log(f"Erreur pendant l'export : {exc}")
            QMessageBox.critical(self, "Export échoué", str(exc))
            return

        self._log("Export terminé.")
        self.statusBar().showMessage("Export terminé", 5000)

    # ------------------------------------------------------------------

    def _build_targets(self):
        """Retourne la liste de (element_ids, output_base) à exporter."""

        document = self.document
        output_dir = Path(self.output_edit.text().strip() or DEFAULT_OUTPUT)

        # Préfixe tous les fichiers exportés par le nom du fichier BPMN
        # source (ex : "testdev_ref.bpmn" -> "testdev_ref_full.png"),
        # comme le fait l'export en ligne de commande.
        source_path = self.file_edit.text().strip()
        prefix = exporter.safe_filename(Path(source_path).stem) + "_" if source_path else ""

        key = self.highlight_combo.currentData()

        if key:
            prefix += f"{exporter.safe_filename(key)}_"

        mode = self.current_mode()

        targets = []

        if mode in ("full", "all"):
            targets.append((None, output_dir / f"{prefix}full"))

        if mode == "process":
            process_id = self.process_combo.currentData()

            if process_id is None:
                return []

            targets.append((
                document.process_elements(process_id),
                output_dir / f"{prefix}process_{exporter.safe_filename(process_id)}",
            ))

        elif mode == "lane":
            lane_id = self.lane_combo.currentData()

            if lane_id is None:
                return []

            lane_name = document.get_lane_name(lane_id)

            targets.append((
                document.lane_elements(lane_id),
                output_dir / f"{prefix}lane_{exporter.safe_filename(lane_name)}",
            ))

        elif mode == "all":
            for process_id in document.processes:
                targets.append((
                    document.process_elements(process_id),
                    output_dir / (
                        f"{prefix}process_{exporter.safe_filename(process_id)}"
                    ),
                ))

            for lane_id in document.lanes:
                lane_name = document.get_lane_name(lane_id)
                targets.append((
                    document.lane_elements(lane_id),
                    output_dir / f"{prefix}lane_{exporter.safe_filename(lane_name)}",
                ))

        return targets

    # ------------------------------------------------------------------

    def _selected_formats(self):
        label = self.format_combo.currentData()

        if label == "png":
            return {"png"}
        if label == "jpg":
            return {"jpg"}
        return set(FORMAT_BOTH)

    # ------------------------------------------------------------------

    def _log(self, text):
        self.log_view.appendPlainText(text)


def run_gui(data_dir=None, output_dir=None, initial_input=None):
    """Point d'entrée utilisé par main.py (et par main() ci-dessous)."""

    app = QApplication.instance() or QApplication(sys.argv[:1])
    window = ExporterWindow(data_dir, output_dir)
    window.show()

    if initial_input:
        initial = Path(initial_input)

        if initial.suffix.lower() == ".bpmn" and initial.is_file():
            window.load_file(str(initial))
            window.convert_panel.input_edit.setText(str(initial))
        elif initial.exists():
            window.convert_panel.input_edit.setText(str(initial))

    return app.exec()


def main():
    return run_gui()


if __name__ == "__main__":
    sys.exit(main())
