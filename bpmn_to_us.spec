# -*- mode: python ; coding: utf-8 -*-
#
# Build :  pyinstaller bpmn_to_us.spec --noconfirm
# Résultat : dist/bpmn_to_us/bpmn_to_us(.exe)  -> interface graphique (sans console)
#            dist/bpmn_to_us/bpmn-cli(.exe)      -> même programme avec console (CLI)

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH)

# --- Données embarquées ------------------------------------------------------
added_files = [
         ( 'data/exemple.bpmn', 'data/' )
    ]

datas = added_files

# BPMN d'exemple recopiés dans ./data au premier lancement (cf. ensure_data_dir)
#if (ROOT / "data").is_dir():
#    datas.append((str(ROOT / "data"), "data"))

# Paquets qui lisent des fichiers à l'exécution (VERSION, modèle .docx, etc.)
for package in ("docx", "cairosvg", "cairocffi", "tinycss2", "cssselect2"):
    datas += collect_data_files(package)

# python-docx lit "docx/parts/../templates/default-footer.xml" : sous Linux/macOS le
# chemin n'est résolu que si docx/parts/ existe réellement sur disque. On y dépose
# donc un fichier vide (inutile sous Windows, sans effet néfaste).
_keep = ROOT / "build" / "pyi_keep"
_keep.parent.mkdir(exist_ok=True)
_keep.write_text("")
datas.append((str(_keep), "docx/parts"))

# --- Imports que l'analyse statique ne voit pas -------------------------------
hiddenimports = [
    "exporter",          # importé dans bpmnparser.generate_images() et exporter_gui
    "exporter_gui",      # importé dans main.launch_gui()
    "bpmnparser",
]
hiddenimports += collect_submodules("mdviewer")
hiddenimports += collect_submodules("mdit_py_plugins")
hiddenimports += collect_submodules("pygments.lexers")   # lexers chargés par nom (```python)
hiddenimports += collect_submodules("pygments.styles")

a = Analysis(
    ["main.py"],
    pathex=[str(ROOT)],
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "_tkinter"],   # l'ancienne GUI Tk a été remplacée par PySide6
    noarchive=False,
)
pyz = PYZ(a.pure)

exe_gui = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="bpmn_to_us",
    console=False,          # pas de fenêtre noire sous Windows
    # icon="icon.ico",
)
exe_cli = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="bpmn_to_us_cli",
    console=True,           # sortie texte du mode ligne de commande
)

coll = COLLECT(
    exe_gui, exe_cli,
    a.binaries, a.datas,
    name="bpmn_to_us",
)
