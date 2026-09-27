# bpmn-to-us

> Convertisseur **BPMN 2.0 → documentation Markdown** : user stories, critères d'acceptation, règles de gestion, matrice de traçabilité — avec diagrammes PNG. GUI + CLI.

**[English summary](#english-summary)**

---

## English summary

A desktop tool (Tkinter GUI + CLI) that parses **BPMN 2.0 XML** files and produces
Obsidian-compatible Markdown documentation: user stories, acceptance criteria, candidate
business rules, and a traceability matrix — plus PNG renderings of the diagram, of each
scenario, and of each activity.

- **Zero configuration** — drop a `.bpmn` file in `data/` and convert.
- **Best-effort image generation** — if `cairosvg` / `Pillow` are unavailable, the Markdown
  conversion still runs, just without images.
- **Three entry points** — `main.py` (GUI + CLI), `bpmnparser.py` (Markdown only),
  `exporter.py` (PNG/JPG export of a diagram).
- Built as a standalone executable with PyInstaller (Windows, onedir).

Licensed under the **GNU AGPL-3.0**.

---

## Sommaire

- [Ce que fait l'outil](#ce-que-fait-loutil)
- [Sorties générées](#sorties-générées)
- [Structure du Markdown généré](#structure-du-markdown-généré)
- [Prérequis](#prérequis)
- [Installation](#installation)
- [Utilisation — CLI](#utilisation--cli)
- [Utilisation — interface graphique](#utilisation--interface-graphique)
- [Construire l'exécutable](#construire-lexécutable)
- [Organisation du projet](#organisation-du-projet)
- [Limites connues](#limites-connues)
- [Licence](#licence)

---

## Ce que fait l'outil

L'outil lit un ou plusieurs fichiers **BPMN 2.0 XML** (fichier `.bpmn` ou répertoire entier)
et en extrait une lecture métier exploitable sous forme de documents Markdown :

| Production | Description |
|---|---|
| **Scénarios métier** | Parcours de bout en bout (`SC-01`, `SC-02`, …) : rôle, tâches, résultat, relais |
| **Scénarios alternatifs** | Chemins issus des événements limites (boundary events) : `SC-ALT-01`, … |
| **User stories** | Une par tâche BPMN (`US-<id>`), au format *En tant que / je veux / afin de* |
| **Critères d'acceptation** | Dérivés du type d'événement et de la tâche |
| **Règles de gestion candidates** | Extraites des conditions portées sur les *sequence flows* et les gateways (`RG-<id>`) |
| **Règles de gestion appliquées** | Tâches `businessRuleTask` explicitement nommées |
| **Matrice de traçabilité** | Table `ID BPMN → Type → Nom → Lane/rôle → User story → Scénario` |
| **Vue atomique** | Rôle et contexte de chaque activité prise isolément |
| **Diagrammes PNG** | Vue d'ensemble, un repère par scénario, un repère par activité |

Les documents produits sont **compatibles Obsidian** (front matter YAML + tags).

## Sorties générées

La conversion écrit dans le répertoire de sortie (par défaut `output-bpmn/`) :

```
output-bpmn/
├── exemple--process-1a2b3c.md          <- un .md par processus BPMN
└── images/
    ├── exemple--process-1a2b3c--overview.png
    ├── exemple--process-1a2b3c--scenario-sc-01.png
    ├── ...                                (un par scénario)
    └── exemple--process-1a2b3c--activity-activity-1a2b3c.png
```

Le nom du document suit le motif `{fichier}--{processus}.md` (par exemple
`exemple--process-1a2b3c.md`). En cas de collision, un suffixe numérique est ajouté.

> 💡 Un exemple réel est versionné dans le dépôt : ouvrez
> [`output-bpmn/exemple--process-1.md`](output-bpmn/exemple--process-1.md) et ses
> images dans [`output-bpmn/images/`](output-bpmn/images/) pour voir exactement ce
> que produit l'outil sur `data/exemple.bpmn`.

## Structure du Markdown généré

Chaque document produit un fichier unique regroupant toutes les sections :

```markdown
---
title: "Process_1a2b3c"
type: exigences-bpmn
bpmn_process_id: Process_1a2b3c
source_bpmn: exemple.bpmn
tags:
  - bpmn
  - user-story
  - regle-gestion
---

# Process_1a2b3c
## Métadonnées
## Vue d'ensemble                 (si images activées)
## Scénarios métier
### SC-01 — <lane> (n tâche(s))
## Scénarios alternatifs (événements limites)
## User stories
### US-<id> — <titre>
#### Données manipulées          (conditionnel)
#### Règle de gestion appliquée  (si businessRuleTask)
#### Critères d'acceptation
#### Traçabilité
## Règles de gestion candidates
## Règles de gestion appliquées (Business Rule Task)   (conditionnel)
## Matrice de traçabilité
## Vue atomique (par activité)
```

## Prérequis

- **Python 3.12 ou supérieur** (d'après `pyproject.toml` et `uv.lock` ; `.python-version` épingle `3.14`)
- Aucun autre composant système requis : uniquement Python et ses dépendances.

Dépendances déclarées dans `pyproject.toml` :

| Dépendance | Rôle |
|---|---|
| `cairosvg` | Rendu SVG → PNG des diagrammes |
| `pillow` | Manipulation d'images |
| `pyinstaller` | Construction de l'exécutable Windows |

L'interface graphique utilise **Tkinter**, fourni avec l'installation standard de Python
(sur Windows : cocher *tcl/tk and IDLE* dans l'installateur Python).

## Installation

Le projet utilise [uv](https://docs.astral.sh/uv/) :

```bash
uv sync
```

Cela crée/réutilise le `.venv` à partir de `uv.lock` et installe les dépendances.

Avec `pip` uniquement :

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
source .venv/bin/activate     # Linux / macOS
pip install cairosvg pillow pyinstaller
```

> `pyproject.toml` déclare `[tool.uv] package = false` : le projet **n'est pas installable
> comme paquet**. On l'exécute directement depuis les sources (`python main.py`).

## Utilisation — CLI

### `main.py` — point d'entrée principal (CLI + GUI)

```bash
python main.py                       # sans argument : lance l'interface graphique
python main.py <fichier.bpmn>       # convertit un fichier
python main.py <dossier> -o <sortie> # convertit tous les BPMN d'un dossier
```

| Argument | Défaut | Rôle |
|---|---|---|
| `input` | *(aucun)* | Fichier `.bpmn` ou répertoire. Optionnel : sans argument, la GUI démarre |
| `-o`, `--output` | `./output-bpmn` | Répertoire Markdown cible |
| `--gui` | — | Force l'interface graphique |
| `--no-images` | — | Désactive toute génération d'images |
| `--no-task-images` | — | Conserve la vue d'ensemble et les images de scénario, supprime les images par activité |

Exemple :

```bash
python main.py data/mon-processus.bpmn -o output-bpmn
```

Sortie : `1 document(s) Markdown généré(s).` (code de sortie `0` ; `1` si aucun document
n'a pu être produit).

### `bpmnparser.py` — convertisseur autonome

Même conversion, avec en plus le contrôle du rendu des images :

```bash
python bpmnparser.py <fichier.bpmn> -o output-bpmn --padding 40 --scale 1.5
```

| Argument | Défaut | Rôle |
|---|---|---|
| `input` | *(obligatoire)* | Fichier `.bpmn` ou répertoire |
| `-o`, `--output` | `./output-bpmn` | Répertoire Markdown cible |
| `--no-images` | — | Désactive toute génération d'images |
| `--no-task-images` | — | Supprime les images par activité |
| `--padding` | `30` | Marge (px) autour des diagrammes |
| `--scale` | `1.0` | Facteur d'échelle |

### `exporter.py` — export d'images seul

```bash
python exporter.py <fichier.bpmn> --list          # lister processus, lanes, sous-processus
python exporter.py <fichier.bpmn> --full          # diagramme complet
python exporter.py <fichier.bpmn> --process Process_1a2b3c
python exporter.py <fichier.bpmn> --lane "Nom de la lane"
python exporter.py <fichier.bpmn> --all --format png
```

| Argument | Rôle |
|---|---|
| `--list` | Liste les processus, lanes et sous-processus (détails des sous-processus repliés) |
| `--full` | Diagramme complet |
| `--process` / `--lane` / `--subprocess` | Cible un élément par ID ou nom |
| `--all` | Complet + tous les processus + toutes les lanes + sous-processus |
| `--output` | Répertoire de sortie (défaut `bpmn_exports`) |
| `--format` | `png`, `jpg` ou `both` (défaut `both`) |
| `--padding`, `--scale` | Marge et échelle |

## Utilisation — interface graphique

```bash
python main.py --gui
```

L'interface (Tkinter, ~1150×1040) offre :

- **Sélection de la source** — bouton *Fichier…* ou *Dossier…*, avec aperçu automatique
- **Aperçu du diagramme** — zoom (`＋` / `－` / *Ajuster*, `Ctrl+molette`), panoramique,
  défilement à la molette
- **User stories de la sélection courante** — rôle, objectif et critères d'acceptation
- **Export d'images** — périmètre *complet / processus / lane / sous-processus / tout*,
  format `png`/`jpg`/`both`, padding et échelle réglables
- **Journal** — sortie de la conversion redirigée en direct
- **Options** — génération des images, image par activité

## Construire l'exécutable

Le fichier `main.spec` décrit un build **PyInstaller onedir** :

```bash
uv run pyinstaller main.spec     # ou : pyinstaller main.spec
```

Le résultat se trouve dans `dist/main/` (`main.exe` + dossier `_internal/`).

> Le dépôt ne contient **aucun script de build** (ni `Makefile`, ni `.bat`/`.sh`/`.ps1`) :
> la commande ci-dessus est l'invocation standard de PyInstaller sur un `.spec`, pas une
> commande scriptée par le projet.

> **Prérequis au build :** `main.spec` déclare `datas = [('data/exemple.bpmn', 'data/')]`.
> Le fichier `data/exemple.bpmn` est versionné, donc **un clone frais peut
> construire l'exécutable** sans rien fournir. Seul le BPMN de démonstration est
> présent : vos BPMN réels restent hors du dépôt.

## Alias de lanes

Les libellés de lanes BPMN contiennent souvent des fautes de frappe ou des
abréviations internes. Chaque alias corrige un libellé : la **clé** est la forme
fautive, la **valeur** le libellé canonique.

Les alias par défaut (dans `_DEFAULT_LANE_ALIASES`, `bpmnparser.py`) reprennent
les deux lanes du BPMN de démonstration — `Mainteneur / CI` et `Contributeur` —
afin que l'exemple soit cohérent de bout en bout :

```json
{
  "contrib": "Contributeur",
  "mainteneur ci": "Mainteneur / CI"
}
```

Pour votre organisation, créez un fichier `lanes.local.json` **à côté de
`bpmnparser.py`**, qui s'ajoute aux alias par défaut :

```json
{
  "zone de depart": "Zone de départ",
  "responsable preparation": "Responsable préparation"
}
```

Ce fichier est **ignoré par git** : le dépôt reste réutilisable par d'autres.
Le format est documenté dans `lanes.example.json`.

> ⚠️ Les clés sont comparées **après normalisation** (minuscules, accents
> retirés — voir `normalize_lane_name`). Une clé contenant un accent ou une
> majuscule ne correspondra jamais :
> `"zone de départ"` est inopérant, `"zone de depart"` fonctionne.
> C'est aussi pourquoi les clés de `lanes.example.json` sont en minuscules
> sans accent, alors que les valeurs portent accents et majuscules.

Le fichier est lu une seule fois au démarrage, via `_load_lane_aliases()`. S'il
est illisible ou mal formé, un avertissement est affiché sur stderr et seuls
les alias par défaut s'appliquent.

## Organisation du projet

| Fichier | Rôle |
|---|---|
| `main.py` | Point d'entrée : CLI, interface graphique Tkinter, orchestration |
| `bpmnparser.py` | Cœur métier : parsing BPMN 2.0, scénarios, règles, rendu Markdown |
| `exporter.py` | Rendu SVG du diagramme (conforme BPMN 2.0) et export PNG/JPG |
| `main.spec` | Configuration de build PyInstaller |
| `pyproject.toml` / `uv.lock` | Dépendances et verrouillage |
| `lanes.example.json` | Exemple de fichier d'alias de lanes (format documenté) |
| `lanes.local.json` | Vos alias de lanes — **non versionné** |
| `data/exemple.bpmn` | BPMN de démonstration, versionné (workflow générique, sans donnée réelle) |
| `data/` | Dossier de travail : y déposer vos `.bpmn` — **seul `exemple.bpmn` est versionné** |
| `output-bpmn/` | Sorties Markdown + images — **seule la sortie d'`exemple.bpmn` est versionnée** |

## Limites connues

- **Cairo sous Windows.** `cairosvg` s'appuie sur la bibliothèque native cairo. Le build
  PyInstaller embarque les DLL (`libcairo-2.dll`, `libffi-8.dll`, `libfontconfig-1.dll`,
  `libfreetype-6.dll`…), ce qui rend l'exécutable autonome, mais une installation par
  `pip` sur Windows suppose que cairo est disponible sur la machine.
- **Les images sont facultatives.** `exporter.py` importe `cairosvg` et `Pillow` au niveau
  du module. Si l'un manque, un avertissement est affiché **une seule fois** et la
  conversion Markdown se poursuit **sans images**. Aucun échec d'image ne fait échouer la
  conversion.
- **Plafond d'images par activité.** Au-delà de **80 activités** dans un processus, les
  images par activité sont omises (un avertissement le signale et le Markdown le mentionne).
  Utilisez `--no-task-images` pour les réduire davantage.
- **Aucune suite de tests.** Le dépôt n'en contient pas. Le paramètre `_test_hook` de
  `launch_gui()` existe pour piloter l'interface depuis un test automatisé, mais aucun
  test ne l'exploite.
- **Cohérence des versions Python.** `pyproject.toml` et `uv.lock` exigent `>=3.12`,
  `.python-version` épingle `3.14`, et les docstrings annoncent encore « 3.10+ ».
- **Sous-dossiers `data/input/` et `data/jobs/`.** Conservés via `.gitkeep` mais
  actuellement non utilisés par le code.

## Licence

**GNU Affero General Public License v3.0** — voir [`LICENSE`](LICENSE).

Les fichiers BPMN traités par cet outil ne sont pas inclus dans le dépôt : ils restent
la propriété de leurs auteurs et ne sont pas couverts par cette licence. Seul
`data/exemple.bpmn` est versionné — un workflow de démonstration fabriqué pour la doc,
sans donnée réelle — et sa sortie correspondante dans `output-bpmn/`.
