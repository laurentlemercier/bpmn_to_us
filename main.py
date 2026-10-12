#!/usr/bin/env python3
"""
Convertit des fichiers BPMN 2.0 XML en Markdown :
- User stories
- Critères d'acceptation
- Règles de gestion candidates
- Matrice de traçabilité

Point d'entrée unique :
- CLI  : python main.py <fichier|dossier> -o <sortie>   (aucune dépendance externe)
- GUI  : python main.py [--gui] [fichier.bpmn]          (interface PySide6 unifiée,
         voir exporter_gui.py : BPMN → Markdown | export d'images | Markdown / Word)

La logique d'extraction est déléguée à bpmnparser.py.
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
    de test embarqués (ou, en mode développement, depuis ./tests et ./testsKT).
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
        # 2) Fallback développement : copier depuis tests / testsKT / data source
        if not sources:
            dev_base = Path(__file__).resolve().parent
            for candidate in [dev_base / "data", dev_base / "tests", dev_base / "testsKT"]:
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


def launch_gui(initial_input: Path | None = None) -> int:
    """Lance l'interface graphique unifiée (PySide6, exporter_gui.py)."""
    try:
        import exporter_gui
    except ImportError as exc:
        print(f"Interface graphique indisponible : {exc}", file=sys.stderr)
        print("Installez les dépendances : pip install PySide6 cairosvg pillow "
              "markdown-it-py mdit-py-plugins python-docx pygments", file=sys.stderr)
        print("Alternative CLI : python main.py <fichier.bpmn> -o <sortie>", file=sys.stderr)
        sys.exit(1)

    # S'assurer que ./data existe et est peuplé (source BPMN par défaut)
    data_dir = ensure_data_dir()

    return exporter_gui.run_gui(
        data_dir=data_dir.resolve(),
        output_dir=(get_base_dir() / "output-bpmn").resolve(),
        initial_input=initial_input,
    )


def main() -> None:
    default_output = get_base_dir() / "output-bpmn"

    parser = argparse.ArgumentParser(
        description="Convertit des BPMN 2.0 en user stories et règles Markdown."
    )
    parser.add_argument(
        "input",
        type=Path,
        nargs="?",
        default=None,
        help="Fichier .bpmn ou répertoire contenant des BPMN "
             "(CLI ; avec --gui : source pré-remplie dans l'interface)",
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
        help="Lance l'interface graphique (par défaut si aucun argument)",
    )
    parser.add_argument(
        "--no-images",
        action="store_true",
        help="Ne génère pas les images PNG du diagramme (générées par défaut)",
    )
    parser.add_argument(
        "--no-scenario-images",
        action="store_true",
        help="Pas d'image avec les tâches de chaque scénario en évidence (générée par défaut)",
    )
    parser.add_argument(
        "--lane-images",
        action="store_true",
        help="Ajoute une image par lane",
    )
    parser.add_argument(
        "--image-scale",
        type=float,
        default=1.0,
        help="Échelle des images (défaut : 1.0)",
    )
    args = parser.parse_args()

    # GUI : --gui, ou aucun argument du tout
    if args.gui or args.input is None:
        sys.exit(launch_gui(args.input))

    ensure_data_dir()
    count = convert(
        args.input, args.output,
        images=not args.no_images,
        image_scale=args.image_scale,
        lane_images=args.lane_images,
        scenario_images=not args.no_scenario_images,
    )

    if count == 0:
        print("Aucun document généré.", file=sys.stderr)
        sys.exit(1)

    print(f"{count} document(s) Markdown généré(s).")


if __name__ == "__main__":
    main()
