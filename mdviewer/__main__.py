"""
Utilisation :
    python -m mdviewer                          # ouvre la fenêtre
    python -m mdviewer note.md                  # affiche un fichier
    python -m mdviewer note.md --export out.docx --mode advanced --quality haute
                                                # export sans interface (batch)
"""
import argparse
import sys
from pathlib import Path

from .core import MarkdownLoader
from .docx_export import ExportOptions, QUALITY_PRESETS, export_docx


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="mdviewer")
    ap.add_argument("file", nargs="?", type=Path)
    ap.add_argument("--flavor", choices=("auto", "standard", "obsidian"), default="auto")
    ap.add_argument("--vault", type=Path, help="racine du coffre Obsidian (auto-détectée sinon)")
    ap.add_argument("--export", type=Path, metavar="OUT.docx")
    ap.add_argument("--mode", choices=("classic", "advanced"), default="advanced")
    ap.add_argument("--quality", choices=tuple(QUALITY_PRESETS), default="standard")
    ap.add_argument("--page", choices=("A4", "Letter"), default="A4")
    ap.add_argument("--template", type=Path)
    ap.add_argument("--no-toc", action="store_true")
    ap.add_argument("--title-page", action="store_true")
    ap.add_argument("--fetch-remote", action="store_true", help="télécharge les images http(s)")
    args = ap.parse_args(argv)

    if args.export:
        if not args.file:
            ap.error("--export nécessite un fichier source")
        loader = MarkdownLoader(args.flavor, args.vault, fetch_remote=args.fetch_remote)
        doc = loader.load_file(args.file)
        opts = ExportOptions(mode=args.mode, quality=args.quality, page_size=args.page,
                             template=args.template, toc=not args.no_toc,
                             title_page=args.title_page)
        out = export_docx(doc, args.export, opts)
        for w in doc.warnings:
            print("⚠", w, file=sys.stderr)
        print(f"Word : {out}")
        return 0

    from .gui import run_gui          # import tardif : l'export batch n'exige pas Qt
    return run_gui(args.file, args.flavor, args.vault)


if __name__ == "__main__":
    sys.exit(main())
