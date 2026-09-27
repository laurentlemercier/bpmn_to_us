#!/usr/bin/env python3
"""
Convertit des fichiers BPMN 2.0 XML en Markdown :
- User stories
- Critères d'acceptation
- Règles de gestion candidates
- Matrice de traçabilité

Wrapper CLI + GUI. La logique d'extraction est déléguée à bpmnparser.py
(mis à jour : vues métier/atomique, alias de lanes, résultats proches, etc).

Aucune dépendance externe.
Compatible Python 3.10+.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Gestion du répertoire ./data (par défaut) et ressources embarquées
# ---------------------------------------------------------------------------

def get_base_dir() -> Path:
    """Répertoire de base de l'application (gère mode PyInstaller frozen)."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        # En mode frozen one-file, l'exécutable est dans sys.executable
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def get_data_dir() -> Path:
    """Répertoire ./data à côté de l'exécutable / du script."""
    return get_base_dir() / "data"


def get_bundled_data_dir() -> Path | None:
    """Répertoire data embarqué via PyInstaller (datas). Retourne None si indisponible."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        bundled = Path(sys._MEIPASS) / "data"  # type: ignore[attr-defined]
        if bundled.is_dir():
            return bundled
    return None


def ensure_data_dir() -> Path:
    """
    Crée ./data s'il n'existe pas et, s'il est vide, le peuple avec les BPMN
    de test embarqués (ou, en mode développement, depuis ./tests et
    ./tests-legacy).
    Retourne le chemin du répertoire data.
    """
    data_dir = get_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)

    has_bpmn = any(data_dir.glob("*.bpmn"))
    if not has_bpmn:
        # 1) Essayer depuis le bundle PyInstaller
        bundled = get_bundled_data_dir()
        sources: list[Path] = []
        if bundled and bundled.is_dir():
            sources.extend(sorted(bundled.glob("*.bpmn")))
        # 2) Fallback développement : copier depuis tests / tests-legacy / data source
        if not sources:
            dev_base = Path(__file__).resolve().parent
            for candidate in [dev_base / "data", dev_base / "tests", dev_base / "tests-legacy"]:
                if candidate.is_dir():
                    found = sorted(candidate.glob("*.bpmn"))
                    # éviter de copier depuis data_dir lui-même si vide
                    if candidate.resolve() != data_dir.resolve():
                        sources.extend(found)
            # déduplication par nom
            seen: set[str] = set()
            deduped: list[Path] = []
            for p in sources:
                if p.name not in seen:
                    seen.add(p.name)
                    deduped.append(p)
            sources = deduped

        for src in sources:
            try:
                shutil.copy2(src, data_dir / src.name)
            except Exception:
                pass

    return data_dir


# ---------------------------------------------------------------------------
# Délégation vers la nouvelle logique d'extraction
# ---------------------------------------------------------------------------
# main.py reste le point d'entrée CLI/GUI, mais toute la conversion
# (parse, scénarios, markdown) vient de bpmnparser.py.
try:
    from bpmnparser import (
        BpmnNode,
        ProcessModel,
        Scenario,
        SequenceFlow,
        # constantes utiles (ré-exportées pour compatibilité)
        BPMN_NS,
        NS,
        TASK_TAGS,
        HUMAN_TASK_TAGS,
        TECHNICAL_TASK_TAGS,
        GATEWAY_TAGS,
        EVENT_TAGS,
        # helpers
        clean_text,
        normalize_text,
        normalize_lane_name,
        slugify,
        markdown_escape,
        node_label,
        story_goal,
        criteria_for_task,
        # parsing / génération
        parse_bpmn,
        find_bpmn_files,
        render_process_markdown,
        convert,
        build_scenarios,
    )
    # alias compat : ancien nom md_escape -> markdown_escape
    md_escape = markdown_escape
except ImportError as exc:
    print(f"ERREUR : impossible d'importer bpmnparser.py : {exc}", file=sys.stderr)
    print("Vérifiez que bpmnparser.py est présent et syntaxiquement valide.", file=sys.stderr)
    sys.exit(1)

# ---------------------------------------------------------------------------
# Export visuel (optionnel) : repris de exporter.py / exporter_gui.py
# ---------------------------------------------------------------------------
# Comme dans bpmnparser.py, cette dépendance (elle-même dépendante de
# cairosvg et Pillow) est optionnelle : sans elle, la GUI se limite à la
# conversion Markdown (sans aperçu ni export d'image direct).
try:
    import exporter as _exporter
except Exception:
    _exporter = None


# ---------------------------------------------------------------------------
# Logique pure (indépendante de Tkinter), réutilisée par la GUI
# ---------------------------------------------------------------------------

def render_user_stories_preview(
    model: ProcessModel, lane_name: str | None = None, container_id: str | None = None,
) -> str:
    """
    Rendu texte allégé des user stories d'un modèle, éventuellement filtré
    sur une seule lane (déjà "alias-normalisée", cf. normalize_lane_name)
    et/ou restreint aux tâches directement nichées dans un sous-processus
    donné (container_id, cf. BpmnNode.parent_container). Utilisé pour le
    bandeau d'aperçu de la GUI : contrairement à render_process_markdown(),
    ne génère ni scénarios, ni images, ni matrice de traçabilité — juste
    les fiches "En tant que / je veux / afin de" + critères, pour un
    aperçu rapide.
    """
    tasks = [n for n in model.nodes.values() if n.tag in TASK_TAGS and not n.is_container]

    if lane_name is not None:
        tasks = [t for t in tasks if (t.lane or "") == lane_name]

    if container_id is not None:
        tasks = [t for t in tasks if t.parent_container == container_id]

    if not tasks:
        return "(Aucune tâche pour cette sélection.)"

    lines: list[str] = []

    for task in tasks:
        role = task.lane or "utilisateur métier"
        title = node_label(task)
        goal = story_goal(model, task)
        criteria = criteria_for_task(model, task)

        lines.append(f"# {title}")
        lines.append(f"En tant que {role},")
        lines.append(f"je veux {title.lower()},")
        lines.append(f"afin de {goal}.")

        if criteria:
            lines.append("Critères d'acceptation :")
            lines.extend(f"  - {c}" for c in criteria)

        lines.append("")

    return "\n".join(lines).rstrip()


def compute_scope_element_ids(
    document, mode: str, process_id: str | None, lane_id: str | None,
    subprocess_id: str | None = None,
):
    """
    Détermine le périmètre (ids d'éléments BPMN) correspondant au mode
    d'export courant, sur un exporter.BPMNDocument.

    Retourne (element_ids, libellé, plane_id) :
    - element_ids : None pour le diagramme complet ("full"/"all") ;
      set(...) pour un processus, une lane ou un sous-processus ;
      False si la sélection est incomplète (aucun processus/lane/
      sous-processus choisi alors que le mode l'exige).
    - plane_id : le plan BPMN-DI à utiliser pour le rendu (voir
      exporter.SVGRenderer.render) — None signifie "le plan principal du
      document" ; un sous-processus replié a son propre plan de détail,
      dans un repère de coordonnées différent du diagramme principal, et
      doit donc être rendu avec son propre plane_id.
    """
    if document is None:
        return False, None, None

    if mode in ("full", "all"):
        return None, "Diagramme complet", None

    if mode == "process":
        if not process_id:
            return False, None, None
        return document.process_elements(process_id), document.get_process_name(process_id), None

    if mode == "lane":
        if not lane_id:
            return False, None, None
        return document.lane_elements(lane_id), document.get_lane_name(lane_id), None

    if mode == "subprocess":
        if not subprocess_id:
            return False, None, None
        return (
            document.subprocess_elements(subprocess_id),
            document.get_subprocess_name(subprocess_id),
            subprocess_id,
        )

    return False, None, None


def build_export_targets(document, mode: str, process_id: str | None, lane_id: str | None,
                          subprocess_id: str | None, output_dir: Path, prefix: str):
    """
    Construit la liste de (element_ids, chemin_de_base, plane_id) à
    exporter en image pour le mode courant — reprend la logique de
    exporter_gui.ExporterWindow._build_targets, adaptée à main.py, en y
    ajoutant les sous-processus détaillés séparément (voir
    exporter.BPMNDocument.detail_plane_ids).
    """
    targets: list[tuple[object, Path, str | None]] = []

    if mode in ("full", "all"):
        targets.append((None, output_dir / f"{prefix}full", None))

    if mode == "process":
        if not process_id:
            return []
        targets.append((
            document.process_elements(process_id),
            output_dir / f"{prefix}process_{_exporter.safe_filename(process_id)}",
            None,
        ))

    elif mode == "lane":
        if not lane_id:
            return []
        lane_name = document.get_lane_name(lane_id)
        targets.append((
            document.lane_elements(lane_id),
            output_dir / f"{prefix}lane_{_exporter.safe_filename(lane_name)}",
            None,
        ))

    elif mode == "subprocess":
        if not subprocess_id:
            return []
        targets.append((
            document.subprocess_elements(subprocess_id),
            output_dir / f"{prefix}subprocess_{_exporter.safe_filename(subprocess_id)}",
            subprocess_id,
        ))

    elif mode == "all":
        for pid in document.processes:
            targets.append((
                document.process_elements(pid),
                output_dir / f"{prefix}process_{_exporter.safe_filename(pid)}",
                None,
            ))
        for lid in document.lanes:
            lane_name = document.get_lane_name(lid)
            targets.append((
                document.lane_elements(lid),
                output_dir / f"{prefix}lane_{_exporter.safe_filename(lane_name)}",
                None,
            ))
        for sid in document.detail_plane_ids:
            targets.append((
                document.subprocess_elements(sid),
                output_dir / f"{prefix}subprocess_{_exporter.safe_filename(sid)}",
                sid,
            ))

    return targets


def launch_gui(_test_hook=None) -> None:
    """Lance une interface graphique Tkinter pour sélectionner les fichiers BPMN.

    `_test_hook`, si fourni, est appelé avec les variables locales de cette
    fonction (root, widgets, StringVar...) juste avant `root.mainloop()`,
    et remplace celui-ci : ceci permet de piloter la GUI depuis des tests
    automatisés (voir tests), sans bloquer sur la boucle d'événements. Ce
    n'est pas une API publique destinée à l'usage normal de l'application.
    """
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk
    except ImportError as exc:
        print(f"Tkinter non disponible : {exc}", file=sys.stderr)
        print("Installez Tkinter ou utilisez le mode CLI : python main.py <fichier.bpmn> -o <sortie>", file=sys.stderr)
        sys.exit(1)

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print(f"Impossible d'ouvrir l'interface graphique : {exc}", file=sys.stderr)
        print("Vérifiez que vous êtes sur un environnement avec affichage (Windows/Mac/Linux desktop).", file=sys.stderr)
        print("Alternative CLI : python main.py <fichier.bpmn> -o <sortie>", file=sys.stderr)
        sys.exit(1)
    root.title("BPMN → Markdown  — Convertisseur")
    root.geometry("1150x1040")
    root.minsize(1000, 820)

    # S'assurer que ./data existe et est peuplé (répertoire par défaut)
    data_dir = ensure_data_dir()

    # Variables (par défaut : ./data comme source BPMN)
    default_input = str(data_dir.resolve()) if data_dir.exists() else ""
    input_var = tk.StringVar(value=default_input)
    output_var = tk.StringVar(value=str((get_base_dir() / "output-bpmn").resolve()))
    with_images_var = tk.BooleanVar(value=True)
    with_task_images_var = tk.BooleanVar(value=True)

    # Options "fichier unique", reprises de exporter_gui.py
    mode_var = tk.StringVar(value="full")
    process_display_var = tk.StringVar()
    lane_display_var = tk.StringVar()
    subprocess_display_var = tk.StringVar()
    format_var = tk.StringVar(value="both")
    padding_var = tk.DoubleVar(value=30.0)
    scale_var = tk.DoubleVar(value=1.0)
    zoom_var = tk.DoubleVar(value=1.0)

    # État courant du fichier unique chargé (document exporter.py + modèles
    # bpmnparser + correspondances affichage -> id pour les comboboxes).
    state: dict = {
        "path": None,
        "document": None,
        "models": [],
        "process_ids_by_display": {},
        "lane_ids_by_display": {},
        "subprocess_ids_by_display": {},
        "preview_image": None,
    }

    def log(message: str) -> None:
        log_text.configure(state="normal")
        log_text.insert(tk.END, message + "\n")
        log_text.see(tk.END)
        log_text.configure(state="disabled")
        root.update_idletasks()

    # ------------------------------------------------------------------
    # Fichier / dossier source
    # ------------------------------------------------------------------

    def browse_file() -> None:
        path = filedialog.askopenfilename(
            title="Sélectionner un fichier BPMN",
            initialdir=str(data_dir) if data_dir.exists() else None,
            filetypes=[("Fichiers BPMN", "*.bpmn"), ("Tous les fichiers", "*.*")],
        )
        if path:
            input_var.set(path)

    def browse_dir() -> None:
        path = filedialog.askdirectory(
            title="Sélectionner un dossier contenant des BPMN",
            initialdir=str(data_dir) if data_dir.exists() else None,
        )
        if path:
            input_var.set(path)

    def browse_output() -> None:
        path = filedialog.askdirectory(title="Sélectionner le dossier de sortie")
        if path:
            output_var.set(path)

    # ------------------------------------------------------------------
    # Panneau "Repérage & export du diagramme" (fichier unique seulement)
    # ------------------------------------------------------------------

    def _set_story_text(text: str) -> None:
        story_text.configure(state="normal")
        story_text.delete("1.0", tk.END)
        story_text.insert(tk.END, text)
        story_text.configure(state="disabled")

    def _clear_file_options() -> None:
        state.update(path=None, document=None, models=[],
                      process_ids_by_display={}, lane_ids_by_display={},
                      subprocess_ids_by_display={}, preview_image=None)
        options_body.pack_forget()
        options_placeholder.pack(anchor="w")
        tree.delete(*tree.get_children())
        zoom_var.set(1.0)
        _set_preview_status("(sélectionnez un fichier .bpmn ci-dessus)")
        _redraw_preview_canvas()
        _set_story_text(
            "Sélectionnez un fichier .bpmn (et non un dossier) pour afficher "
            "les user stories ici."
        )

    def _refresh_tree_and_combos() -> None:
        tree.delete(*tree.get_children())
        state["process_ids_by_display"] = {}
        state["lane_ids_by_display"] = {}
        state["subprocess_ids_by_display"] = {}

        doc = state["document"]
        models = state["models"]

        proc_root = tree.insert("", "end", text="Processus", open=True)
        process_values = []
        if doc is not None:
            for process_id, process in doc.processes.items():
                name = process.get("name") or process_id
                display = f"{name}  [{process_id}]"
                tree.insert(proc_root, "end", iid=f"process::{process_id}", text=name)
                state["process_ids_by_display"][display] = process_id
                process_values.append(display)
        else:
            # Repli sans exporter.py : les processus viennent de bpmnparser,
            # mais ni aperçu ni export d'image ne seront possibles.
            for model in models:
                display = f"{model.name}  [{model.id}]"
                tree.insert(proc_root, "end", iid=f"process::{model.id}", text=model.name)
                state["process_ids_by_display"][display] = model.id
                process_values.append(display)

        process_combo["values"] = process_values
        process_display_var.set(process_values[0] if process_values else "")

        lane_root = tree.insert("", "end", text="Lanes", open=True)
        lane_values = []
        if doc is not None:
            for lane_id, lane in doc.lanes.items():
                name = lane.get("name") or lane_id
                display = f"{name}  [{lane_id}]"
                tree.insert(lane_root, "end", iid=f"lane::{lane_id}", text=name)
                state["lane_ids_by_display"][display] = lane_id
                lane_values.append(display)
        lane_combo["values"] = lane_values
        lane_display_var.set(lane_values[0] if lane_values else "")

        # Sous-processus repliés dont le contenu est détaillé dans un
        # diagramme séparé (voir exporter.BPMNDocument.detail_plane_ids) :
        # ils n'apparaissent qu'avec exporter.py, puisqu'ils reposent sur
        # le BPMN-DI (positions), pas seulement sur le modèle sémantique.
        subprocess_root = tree.insert("", "end", text="Sous-processus (détail)", open=True)
        subprocess_values = []
        if doc is not None:
            for subprocess_id in doc.detail_plane_ids:
                name = doc.get_subprocess_name(subprocess_id)
                display = f"{name}  [{subprocess_id}]"
                tree.insert(subprocess_root, "end", iid=f"subprocess::{subprocess_id}", text=name)
                state["subprocess_ids_by_display"][display] = subprocess_id
                subprocess_values.append(display)
        subprocess_combo["values"] = subprocess_values
        subprocess_display_var.set(subprocess_values[0] if subprocess_values else "")

        tree.item(proc_root, open=True)
        tree.item(lane_root, open=True)
        tree.item(subprocess_root, open=True)

        has_doc = doc is not None
        for radio in (process_radio, lane_radio, subprocess_radio, all_radio):
            radio.configure(state="normal" if has_doc else "disabled")
        export_button.configure(state="normal" if has_doc else "disabled")
        if not has_doc:
            mode_var.set("full")
            state["preview_image"] = None
            _set_preview_status(
                "(aperçu et export d'image indisponibles : exporter.py ou "
                "cairosvg/Pillow non installés — seule la conversion "
                "Markdown, sans images, reste possible)"
            )
            _redraw_preview_canvas()

    def _current_ids(display_map: dict, display_value: str) -> str | None:
        return display_map.get(display_value)

    def _refresh_mode_state(*_args) -> None:
        mode = mode_var.get()
        has_doc = state["document"] is not None
        process_combo.configure(
            state="readonly" if has_doc and mode == "process" and process_combo["values"] else "disabled"
        )
        lane_combo.configure(
            state="readonly" if has_doc and mode == "lane" and lane_combo["values"] else "disabled"
        )
        subprocess_combo.configure(
            state="readonly" if has_doc and mode == "subprocess" and subprocess_combo["values"] else "disabled"
        )

    def _update_preview(*_args) -> None:
        doc = state["document"]

        if doc is None:
            return  # message déjà posé par _refresh_tree_and_combos / _clear_file_options

        process_id = _current_ids(state["process_ids_by_display"], process_display_var.get())
        lane_id = _current_ids(state["lane_ids_by_display"], lane_display_var.get())
        subprocess_id = _current_ids(state["subprocess_ids_by_display"], subprocess_display_var.get())
        element_ids, _label, plane_id = compute_scope_element_ids(
            doc, mode_var.get(), process_id, lane_id, subprocess_id,
        )

        if element_ids is False:
            state["preview_image"] = None
            _set_preview_status("(choisissez un processus, une lane ou un sous-processus)")
            _redraw_preview_canvas()
            return

        try:
            import io

            renderer = _exporter.SVGRenderer(doc, padding=padding_var.get(), scale=scale_var.get())
            svg = renderer.render(element_ids, plane_id=plane_id)
            png_bytes = _exporter.cairosvg.svg2png(bytestring=svg.encode("utf-8"))
            image = _exporter.Image.open(io.BytesIO(png_bytes)).convert("RGB")
        except Exception as exc:
            state["preview_image"] = None
            _set_preview_status(f"Aperçu impossible : {exc}")
            _redraw_preview_canvas()
            return

        state["preview_image"] = image
        _set_preview_status(None)
        _redraw_preview_canvas()

    def _set_preview_status(text: str | None) -> None:
        if text:
            preview_status.configure(text=text)
            preview_status.pack(fill=tk.X)
        else:
            preview_status.pack_forget()

    def _fit_factor(image) -> float:
        canvas_w = preview_canvas.winfo_width() or 1
        canvas_h = preview_canvas.winfo_height() or 1
        iw, ih = image.size
        if iw <= 0 or ih <= 0:
            return 1.0
        return max(min(canvas_w / iw, canvas_h / ih, 1.0), 0.01)

    def _redraw_preview_canvas(*_args) -> None:
        image = state.get("preview_image")
        preview_canvas.delete("all")

        if image is None:
            zoom_label.configure(text="—")
            for btn in (zoom_out_btn, zoom_in_btn, zoom_fit_btn):
                btn.configure(state="disabled")
            return

        for btn in (zoom_out_btn, zoom_in_btn, zoom_fit_btn):
            btn.configure(state="normal")

        factor = _fit_factor(image) * zoom_var.get()
        factor = max(factor, 0.02)
        iw, ih = image.size
        new_size = (max(1, int(iw * factor)), max(1, int(ih * factor)))

        from PIL import ImageTk

        resample = getattr(_exporter.Image, "LANCZOS", None) or getattr(_exporter.Image, "BICUBIC")
        display_image = image.resize(new_size, resample)
        photo = ImageTk.PhotoImage(display_image)

        preview_canvas.create_image(0, 0, anchor="nw", image=photo)
        preview_canvas.configure(scrollregion=(0, 0, new_size[0], new_size[1]))
        preview_canvas.image = photo  # garder une référence : Tkinter ne le fait pas lui-même

        zoom_label.configure(text=f"{round(zoom_var.get() * 100)}%")

    def _zoom_by(factor: float) -> None:
        if state.get("preview_image") is None:
            return
        zoom_var.set(max(0.2, min(6.0, zoom_var.get() * factor)))
        _redraw_preview_canvas()

    def _zoom_in() -> None:
        _zoom_by(1.25)

    def _zoom_out() -> None:
        _zoom_by(1 / 1.25)

    def _zoom_reset() -> None:
        if state.get("preview_image") is None:
            return
        zoom_var.set(1.0)
        _redraw_preview_canvas()

    def _on_preview_wheel(event) -> None:
        ctrl_held = bool(event.state & 0x4)
        if ctrl_held:
            if getattr(event, "num", None) == 4 or getattr(event, "delta", 0) > 0:
                _zoom_in()
            else:
                _zoom_out()
            return
        if getattr(event, "num", None) == 4:
            preview_canvas.yview_scroll(-3, "units")
        elif getattr(event, "num", None) == 5:
            preview_canvas.yview_scroll(3, "units")
        else:
            preview_canvas.yview_scroll(int(-1 * (event.delta / 120)) * 3, "units")

    def _update_story_banner(*_args) -> None:
        models = state["models"]

        if not models:
            return  # message déjà posé par _clear_file_options

        mode = mode_var.get()
        doc = state["document"]

        if mode == "process":
            process_id = _current_ids(state["process_ids_by_display"], process_display_var.get())
            model = next((m for m in models if m.id == process_id), None)
            if model is None:
                _set_story_text("(sélectionnez un processus)")
            else:
                _set_story_text(render_user_stories_preview(model))
            return

        if mode == "lane":
            lane_id = _current_ids(state["lane_ids_by_display"], lane_display_var.get())
            if lane_id is None or doc is None:
                _set_story_text("(sélectionnez une lane)")
                return
            lane_name = normalize_lane_name(doc.get_lane_name(lane_id))
            chunks = [
                render_user_stories_preview(model, lane_name=lane_name)
                for model in models
            ]
            chunks = [c for c in chunks if c and "Aucune tâche" not in c]
            _set_story_text("\n\n".join(chunks) or "(Aucune tâche pour cette lane.)")
            return

        if mode == "subprocess":
            subprocess_id = _current_ids(
                state["subprocess_ids_by_display"], subprocess_display_var.get(),
            )
            if subprocess_id is None:
                _set_story_text("(sélectionnez un sous-processus)")
                return
            chunks = [
                render_user_stories_preview(model, container_id=subprocess_id)
                for model in models
            ]
            chunks = [c for c in chunks if c and "Aucune tâche" not in c]
            _set_story_text(
                "\n\n".join(chunks) or "(Aucune tâche trouvée pour ce sous-processus.)"
            )
            return

        # "full" / "all" : tous les processus du fichier.
        chunks = [
            (f"== {model.name} ==\n\n" if len(models) > 1 else "")
            + render_user_stories_preview(model)
            for model in models
        ]
        _set_story_text("\n\n".join(chunks))

    def _on_selection_changed(*_args) -> None:
        _refresh_mode_state()
        _update_preview()
        _update_story_banner()

    def _on_tree_select(_event=None) -> None:
        selection = tree.selection()
        if not selection:
            return
        iid = selection[0]

        if iid.startswith("process::"):
            process_id = iid.split("::", 1)[1]
            display = next(
                (d for d, pid in state["process_ids_by_display"].items() if pid == process_id),
                None,
            )
            if display:
                process_display_var.set(display)
            mode_var.set("process")
        elif iid.startswith("subprocess::"):
            subprocess_id = iid.split("::", 1)[1]
            display = next(
                (d for d, sid in state["subprocess_ids_by_display"].items() if sid == subprocess_id),
                None,
            )
            if display:
                subprocess_display_var.set(display)
            mode_var.set("subprocess")
        elif iid.startswith("lane::"):
            lane_id = iid.split("::", 1)[1]
            display = next(
                (d for d, lid in state["lane_ids_by_display"].items() if lid == lane_id),
                None,
            )
            if display:
                lane_display_var.set(display)
            mode_var.set("lane")
        else:
            return

        _on_selection_changed()

    def _load_single_file(path: Path) -> None:
        try:
            models = parse_bpmn(path)
        except Exception as exc:
            log(f"⚠ Lecture BPMN impossible : {exc}")
            models = []

        document = None
        if _exporter is not None:
            try:
                document = _exporter.BPMNDocument(path)
            except Exception as exc:
                log(f"⚠ exporter.py : lecture impossible ({exc})")

        state.update(path=path, document=document, models=models)

        options_placeholder.pack_forget()
        options_body.pack(fill=tk.BOTH, expand=True)

        _refresh_tree_and_combos()
        mode_var.set("full")
        _on_selection_changed()

    def _on_input_changed(*_args) -> None:
        raw = input_var.get().strip()
        path = Path(raw) if raw else None

        if path and path.is_file() and path.suffix.lower() == ".bpmn":
            _load_single_file(path)
        else:
            _clear_file_options()

    def _export_diagram() -> None:
        doc = state["document"]
        path = state["path"]

        if doc is None or path is None:
            messagebox.showwarning(
                "Indisponible",
                "L'export d'image nécessite exporter.py (et cairosvg/Pillow) "
                "ainsi qu'un fichier .bpmn unique sélectionné.",
            )
            return

        process_id = _current_ids(state["process_ids_by_display"], process_display_var.get())
        lane_id = _current_ids(state["lane_ids_by_display"], lane_display_var.get())
        subprocess_id = _current_ids(state["subprocess_ids_by_display"], subprocess_display_var.get())
        mode = mode_var.get()

        element_ids, _label, _plane_id = compute_scope_element_ids(
            doc, mode, process_id, lane_id, subprocess_id,
        )
        if element_ids is False:
            messagebox.showwarning(
                "Sélection incomplète", "Choisissez un processus, une lane ou un sous-processus.",
            )
            return

        output_dir = Path(output_var.get().strip() or "bpmn_exports")
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror("Dossier de sortie invalide", str(exc))
            return

        prefix = _exporter.safe_filename(path.stem) + "_"
        targets = build_export_targets(
            doc, mode, process_id, lane_id, subprocess_id, output_dir, prefix,
        )

        if not targets:
            messagebox.showwarning(
                "Rien à exporter", "Choisissez un processus, une lane ou un sous-processus.",
            )
            return

        label = format_var.get()
        formats = {"png"} if label == "png" else ({"jpg"} if label == "jpg" else {"png", "jpg"})

        try:
            for target_ids, base, target_plane_id in targets:
                _exporter.export_selection(
                    doc, target_ids, base, formats,
                    padding_var.get(), scale_var.get(),
                    plane_id=target_plane_id,
                )
        except Exception as exc:
            messagebox.showerror("Export échoué", str(exc))
            return

        log(f"✓ Image(s) exportée(s) dans : {output_dir}")
        messagebox.showinfo("Export terminé", f"Image(s) exportée(s) dans :\n{output_dir}")

    # ------------------------------------------------------------------
    # Conversion Markdown
    # ------------------------------------------------------------------

    def do_convert() -> None:
        input_str = input_var.get().strip()
        output_str = output_var.get().strip()

        if not input_str:
            messagebox.showwarning("Entrée manquante", "Veuillez sélectionner un fichier .bpmn ou un dossier.")
            return

        input_path = Path(input_str)
        output_path = Path(output_str) if output_str else Path("./output-bpmn")

        if not input_path.exists():
            messagebox.showerror("Chemin introuvable", f"Chemin introuvable : {input_path}")
            return

        log_text.configure(state="normal")
        log_text.delete("1.0", tk.END)
        log_text.configure(state="disabled")

        log(f"▶ Entrée  : {input_path}")
        log(f"▶ Sortie  : {output_path}")
        log("─" * 60)

        try:
            # Redirige stdout/stderr temporairement vers le widget
            import io
            import contextlib

            buf_out = io.StringIO()
            buf_err = io.StringIO()
            with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
                count = convert(
                    input_path, output_path,
                    with_images=with_images_var.get(),
                    with_task_images=with_task_images_var.get(),
                    padding=padding_var.get(),
                    scale=scale_var.get(),
                )

            out = buf_out.getvalue().strip()
            err = buf_err.getvalue().strip()
            if out:
                for line in out.splitlines():
                    log(line)
            if err:
                for line in err.splitlines():
                    log(f"⚠ {line}")

            if count == 0:
                log("Aucun document généré.")
                messagebox.showwarning("Terminé", "Aucun document Markdown n'a été généré.\nVérifiez le fichier BPMN.")
            else:
                log(f"✓ {count} document(s) Markdown généré(s) dans : {output_path.resolve()}")
                if messagebox.askyesno("Succès", f"{count} document(s) généré(s).\nOuvrir le dossier de sortie ?"):
                    import os
                    import platform
                    import subprocess

                    try:
                        if platform.system() == "Windows":
                            os.startfile(output_path.resolve())  # type: ignore[attr-defined]
                        elif platform.system() == "Darwin":
                            subprocess.Popen(["open", str(output_path.resolve())])
                        else:
                            subprocess.Popen(["xdg-open", str(output_path.resolve())])
                    except Exception:
                        pass
        except Exception as exc:
            log(f"✗ Erreur : {exc}")
            messagebox.showerror("Erreur", str(exc))

    # ---- Layout ----
    style = ttk.Style()
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass

    main_frame = ttk.Frame(root, padding=16)
    main_frame.pack(fill=tk.BOTH, expand=True)

    title_lbl = ttk.Label(main_frame, text="Convertisseur BPMN 2.0 → Markdown", font=("Segoe UI", 13, "bold"))
    title_lbl.pack(anchor="w", pady=(0, 4))
    ttk.Label(
        main_frame,
        text="Sélectionnez un fichier .bpmn (options de repérage + aperçu) "
             "ou un dossier (conversion en lot), puis choisissez la sortie.",
    ).pack(anchor="w", pady=(0, 12))

    # Entrée BPMN
    input_group = ttk.LabelFrame(main_frame, text=" Source BPMN ", padding=8)
    input_group.pack(fill=tk.X, pady=(0, 10))

    ttk.Entry(input_group, textvariable=input_var).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))

    ttk.Button(input_group, text="Fichier…", command=browse_file).pack(side=tk.LEFT, padx=2)
    ttk.Button(input_group, text="Dossier…", command=browse_dir).pack(side=tk.LEFT, padx=2)

    # Sortie + options d'images : toujours visibles, quel que soit le mode.
    settings_row = ttk.Frame(main_frame)
    settings_row.pack(fill=tk.X, pady=(0, 10))

    output_group = ttk.LabelFrame(settings_row, text=" Dossier de sortie ", padding=8)
    output_group.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))

    ttk.Entry(output_group, textvariable=output_var).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))
    ttk.Button(output_group, text="Parcourir…", command=browse_output).pack(side=tk.LEFT)

    images_group = ttk.LabelFrame(settings_row, text=" Images Markdown ", padding=8)
    images_group.pack(side=tk.LEFT, fill=tk.Y)

    def _on_toggle_images() -> None:
        state_str = "normal" if with_images_var.get() else "disabled"
        task_images_check.configure(state=state_str)

    ttk.Checkbutton(
        images_group,
        text="Générer les images (vue d'ensemble + scénarios + activités)",
        variable=with_images_var,
        command=_on_toggle_images,
    ).pack(anchor="w")

    task_images_check = ttk.Checkbutton(
        images_group,
        text="Inclure une image par activité",
        variable=with_task_images_var,
    )
    task_images_check.pack(anchor="w", padx=(20, 0))

    # Boutons action — ancrés en bas (empaquetés avant le contenu principal :
    # voir la note plus bas sur l'ordre de pack()).
    btn_frame = ttk.Frame(main_frame)
    btn_frame.pack(side=tk.BOTTOM, fill=tk.X, pady=(10, 0))

    ttk.Button(btn_frame, text="▶  Convertir", command=do_convert).pack(side=tk.LEFT, padx=(0, 8))
    ttk.Button(btn_frame, text="Quitter", command=root.destroy).pack(side=tk.LEFT)

    # ------------------------------------------------------------------
    # Corps principal : colonne gauche (mode/export + journal) et colonne
    # droite (aperçu zoomable + user stories), séparées par une poignée de
    # redimensionnement (PanedWindow horizontal). Chaque colonne a elle-même
    # une poignée verticale entre ses deux zones.
    # ------------------------------------------------------------------
    main_pane = ttk.PanedWindow(main_frame, orient=tk.HORIZONTAL)
    main_pane.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

    left_pane = ttk.PanedWindow(main_pane, orient=tk.VERTICAL)
    main_pane.add(left_pane, weight=1)

    right_pane = ttk.PanedWindow(main_pane, orient=tk.VERTICAL)
    main_pane.add(right_pane, weight=2)

    # ------------------------------------------------------------------
    # Colonne gauche, haut : "Repérage & export du diagramme"
    # (contenu, mode, options d'export) — visible seulement pour un
    # fichier .bpmn unique.
    # ------------------------------------------------------------------
    options_frame = ttk.LabelFrame(
        left_pane, text=" Repérage & export du diagramme (fichier unique) ", padding=8,
    )
    left_pane.add(options_frame, weight=2)

    options_placeholder = ttk.Label(
        options_frame,
        text="Sélectionnez un fichier .bpmn (pas un dossier) pour afficher le "
             "contenu du diagramme, choisir un mode d'export, régler le "
             "padding/l'échelle et voir un aperçu.",
        foreground="#666",
        wraplength=320,
    )
    options_placeholder.pack(anchor="w")

    options_body = ttk.Frame(options_frame)
    # Pas encore empaqueté : affiché uniquement quand un fichier est chargé
    # (voir _load_single_file / _clear_file_options).

    # -- Contenu (--list) : arbre Processus / Lanes -----------------------
    tree_group = ttk.LabelFrame(options_body, text=" Contenu du diagramme (--list) ", padding=6)
    tree_group.pack(fill=tk.X, pady=(0, 8))

    tree = ttk.Treeview(tree_group, show="tree", height=6, selectmode="browse")
    tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    tree.column("#0", width=200)

    tree_scroll = ttk.Scrollbar(tree_group, orient=tk.VERTICAL, command=tree.yview)
    tree_scroll.pack(side=tk.LEFT, fill=tk.Y)
    tree.configure(yscrollcommand=tree_scroll.set)

    tree.bind("<<TreeviewSelect>>", _on_tree_select)

    # -- Mode d'export ------------------------------------------------------
    mode_group = ttk.LabelFrame(options_body, text=" Mode ", padding=6)
    mode_group.pack(fill=tk.X, pady=(0, 8))

    full_radio = ttk.Radiobutton(
        mode_group, text="Diagramme complet (--full)", value="full",
        variable=mode_var, command=_on_selection_changed,
    )
    process_radio = ttk.Radiobutton(
        mode_group, text="Un processus (--process)", value="process",
        variable=mode_var, command=_on_selection_changed,
    )
    lane_radio = ttk.Radiobutton(
        mode_group, text="Une lane (--lane)", value="lane",
        variable=mode_var, command=_on_selection_changed,
    )
    subprocess_radio = ttk.Radiobutton(
        mode_group, text="Un sous-processus, détail (--subprocess)", value="subprocess",
        variable=mode_var, command=_on_selection_changed,
    )
    all_radio = ttk.Radiobutton(
        mode_group, text="Tout : complet + processus + lanes + sous-processus (--all)",
        value="all", variable=mode_var, command=_on_selection_changed,
    )
    for radio in (full_radio, process_radio, lane_radio, subprocess_radio, all_radio):
        radio.pack(anchor="w")

    combo_row1 = ttk.Frame(mode_group)
    combo_row1.pack(fill=tk.X, pady=(4, 0))
    ttk.Label(combo_row1, text="Processus :").pack(side=tk.LEFT)
    process_combo = ttk.Combobox(
        combo_row1, textvariable=process_display_var, state="disabled",
    )
    process_combo.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 0))
    process_combo.bind("<<ComboboxSelected>>", _on_selection_changed)

    combo_row2 = ttk.Frame(mode_group)
    combo_row2.pack(fill=tk.X, pady=(4, 0))
    ttk.Label(combo_row2, text="Lane :").pack(side=tk.LEFT)
    lane_combo = ttk.Combobox(
        combo_row2, textvariable=lane_display_var, state="disabled",
    )
    lane_combo.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 0))
    lane_combo.bind("<<ComboboxSelected>>", _on_selection_changed)

    combo_row3 = ttk.Frame(mode_group)
    combo_row3.pack(fill=tk.X, pady=(4, 0))
    ttk.Label(combo_row3, text="Sous-processus :").pack(side=tk.LEFT)
    subprocess_combo = ttk.Combobox(
        combo_row3, textvariable=subprocess_display_var, state="disabled",
    )
    subprocess_combo.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 0))
    subprocess_combo.bind("<<ComboboxSelected>>", _on_selection_changed)

    # -- Options d'export (format / padding / échelle) --------------------
    export_group = ttk.LabelFrame(options_body, text=" Export image ", padding=6)
    export_group.pack(fill=tk.X)

    format_row = ttk.Frame(export_group)
    format_row.pack(fill=tk.X)
    ttk.Label(format_row, text="Format :").pack(side=tk.LEFT)
    format_combo = ttk.Combobox(
        format_row, textvariable=format_var, state="readonly",
        values=["png", "jpg", "both"], width=8,
    )
    format_combo.pack(side=tk.LEFT, padx=(4, 0))

    padding_row = ttk.Frame(export_group)
    padding_row.pack(fill=tk.X, pady=(4, 0))
    ttk.Label(padding_row, text="Padding :").pack(side=tk.LEFT)
    padding_spin = ttk.Spinbox(
        padding_row, from_=0, to=500, increment=5, textvariable=padding_var, width=8,
        command=_update_preview,
    )
    padding_spin.pack(side=tk.LEFT, padx=(4, 0))
    padding_spin.bind("<Return>", _update_preview)
    padding_spin.bind("<FocusOut>", _update_preview)

    scale_row = ttk.Frame(export_group)
    scale_row.pack(fill=tk.X, pady=(4, 0))
    ttk.Label(scale_row, text="Échelle :").pack(side=tk.LEFT)
    scale_spin = ttk.Spinbox(
        scale_row, from_=0.1, to=10.0, increment=0.1, textvariable=scale_var, width=8,
        command=_update_preview,
    )
    scale_spin.pack(side=tk.LEFT, padx=(4, 0))
    scale_spin.bind("<Return>", _update_preview)
    scale_spin.bind("<FocusOut>", _update_preview)

    export_button = ttk.Button(
        export_group, text="Exporter le diagramme…", command=_export_diagram,
    )
    export_button.pack(anchor="w", pady=(6, 0))

    # ------------------------------------------------------------------
    # Colonne gauche, bas : Journal
    # ------------------------------------------------------------------
    log_frame = ttk.LabelFrame(left_pane, text=" Journal ", padding=6)
    left_pane.add(log_frame, weight=1)

    log_text = tk.Text(log_frame, height=8, wrap=tk.WORD, state="disabled", font=("Consolas", 9))
    log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    log_scrollbar = ttk.Scrollbar(log_frame, orient=tk.VERTICAL, command=log_text.yview)
    log_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
    log_text.configure(yscrollcommand=log_scrollbar.set)

    # ------------------------------------------------------------------
    # Colonne droite, haut : Aperçu (zoomable, déplaçable)
    # ------------------------------------------------------------------
    preview_group = ttk.LabelFrame(right_pane, text=" Aperçu ", padding=6)
    right_pane.add(preview_group, weight=3)

    preview_status = ttk.Label(preview_group, foreground="#666", wraplength=520, justify="left")
    # Empaqueté/masqué dynamiquement par _set_preview_status().

    zoom_row = ttk.Frame(preview_group)
    zoom_row.pack(fill=tk.X, pady=(0, 4))

    zoom_out_btn = ttk.Button(zoom_row, text="－", width=3, command=_zoom_out)
    zoom_out_btn.pack(side=tk.LEFT)
    zoom_label = ttk.Label(zoom_row, text="—", width=6, anchor="center")
    zoom_label.pack(side=tk.LEFT, padx=4)
    zoom_in_btn = ttk.Button(zoom_row, text="＋", width=3, command=_zoom_in)
    zoom_in_btn.pack(side=tk.LEFT)
    zoom_fit_btn = ttk.Button(zoom_row, text="Ajuster", command=_zoom_reset)
    zoom_fit_btn.pack(side=tk.LEFT, padx=(8, 0))
    ttk.Label(
        zoom_row, text="(molette = défiler, Ctrl+molette = zoomer)", foreground="#666",
    ).pack(side=tk.LEFT, padx=(10, 0))

    canvas_frame = ttk.Frame(preview_group)
    canvas_frame.pack(fill=tk.BOTH, expand=True)
    canvas_frame.rowconfigure(0, weight=1)
    canvas_frame.columnconfigure(0, weight=1)

    preview_canvas = tk.Canvas(canvas_frame, background="#dddddd", highlightthickness=0)
    preview_canvas.grid(row=0, column=0, sticky="nsew")

    preview_hbar = ttk.Scrollbar(canvas_frame, orient=tk.HORIZONTAL, command=preview_canvas.xview)
    preview_hbar.grid(row=1, column=0, sticky="ew")
    preview_vbar = ttk.Scrollbar(canvas_frame, orient=tk.VERTICAL, command=preview_canvas.yview)
    preview_vbar.grid(row=0, column=1, sticky="ns")
    preview_canvas.configure(xscrollcommand=preview_hbar.set, yscrollcommand=preview_vbar.set)

    preview_canvas.bind("<Configure>", _redraw_preview_canvas)
    preview_canvas.bind("<MouseWheel>", _on_preview_wheel)
    preview_canvas.bind("<Button-4>", _on_preview_wheel)
    preview_canvas.bind("<Button-5>", _on_preview_wheel)
    # Glisser avec le clic gauche pour déplacer la vue (panoramique).
    preview_canvas.bind("<ButtonPress-1>", lambda e: preview_canvas.scan_mark(e.x, e.y))
    preview_canvas.bind("<B1-Motion>", lambda e: preview_canvas.scan_dragto(e.x, e.y, gain=1))

    # ------------------------------------------------------------------
    # Colonne droite, bas : User stories
    # ------------------------------------------------------------------
    story_frame = ttk.LabelFrame(right_pane, text=" User stories (sélection courante) ", padding=6)
    right_pane.add(story_frame, weight=2)

    story_text = tk.Text(story_frame, height=8, wrap=tk.WORD, state="disabled", font=("Consolas", 9))
    story_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    story_scrollbar = ttk.Scrollbar(story_frame, orient=tk.VERTICAL, command=story_text.yview)
    story_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
    story_text.configure(yscrollcommand=story_scrollbar.set)

    input_var.trace_add("write", _on_input_changed)

    log("Prêt. Sélectionnez un fichier .bpmn puis cliquez sur Convertir.")
    _clear_file_options()
    _on_input_changed()

    # Positions initiales des poignées (après que Tk ait calculé les tailles
    # naturelles) : colonne gauche plus étroite que la colonne droite, et
    # dans chaque colonne, la zone du haut un peu plus grande que celle du
    # bas.
    def _set_initial_sashes() -> None:
        try:
            main_pane.sashpos(0, 400)
            left_pane.sashpos(0, 560)
            right_pane.sashpos(0, 460)
        except tk.TclError:
            pass

    root.update_idletasks()
    root.after(0, _set_initial_sashes)

    if _test_hook is not None:
        _test_hook(locals())
        return

    root.mainloop()


def main() -> None:
    # Répertoire par défaut : ./data (créé/peuplé à la volée)
    data_dir = ensure_data_dir()
    default_output = get_base_dir() / "output-bpmn"

    parser = argparse.ArgumentParser(
        description="Convertit des BPMN 2.0 en user stories et règles Markdown."
    )
    parser.add_argument(
        "input",
        type=Path,
        nargs="?",
        default=None,
        help="Fichier .bpmn ou répertoire contenant des BPMN (défaut : ./data, optionnel en mode --gui)",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=default_output,
        help="Répertoire Markdown cible (défaut : ./output-bpmn)",
    )
    parser.add_argument(
        "--gui",
        action="store_true",
        help="Lance l'interface graphique de sélection de fichier",
    )
    parser.add_argument(
        "--no-images",
        action="store_true",
        help="Désactive la génération d'images (vue d'ensemble, scénarios, activités).",
    )
    parser.add_argument(
        "--no-task-images",
        action="store_true",
        help="Ne génère pas les images par activité (conserve la vue d'ensemble "
             "et les images de scénario).",
    )
    args = parser.parse_args()

    # Mode GUI : --gui ou aucun argument
    # Le répertoire par défaut pour la GUI est ./data (pré-rempli via ensure_data_dir / launch_gui)
    if args.gui or args.input is None:
        if args.input is None and not args.gui and len(sys.argv) == 1:
            try:
                launch_gui()
            except SystemExit:
                raise
            except Exception as exc:
                print(f"Impossible de lancer la GUI : {exc}", file=sys.stderr)
                sys.exit(2)
            return
        if args.gui:
            try:
                launch_gui()
            except SystemExit:
                raise
            except Exception as exc:
                print(f"Impossible de lancer la GUI : {exc}", file=sys.stderr)
                sys.exit(2)
            return
        # Aucun input fourni sans --gui -> afficher aide + proposer GUI
        parser.print_help()
        print("\nAstuce : lancez avec --gui pour ouvrir la fenêtre de sélection.", file=sys.stderr)
        try:
            launch_gui()
        except SystemExit:
            raise
        except Exception as exc:
            print(f"Impossible de lancer la GUI : {exc}", file=sys.stderr)
            sys.exit(2)
        return

    count = convert(
        args.input, args.output,
        with_images=not args.no_images,
        with_task_images=not args.no_task_images,
    )

    if count == 0:
        print("Aucun document généré.", file=sys.stderr)
        sys.exit(1)

    print(f"{count} document(s) Markdown généré(s).")


if __name__ == "__main__":
    main()
