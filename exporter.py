#!/usr/bin/env python3
"""
BPMN Exporter - V2

Exporte un fichier BPMN 2.0 en PNG et/ou JPG.

Modes :
    --full
    --process ID
    --lane NAME
    --all
    --list

Exemples :
    python exporter.py exemple.bpmn --list

    python exporter.py exemple.bpmn --full

    python exporter.py exemple.bpmn \
        --process Process_1a2b3c

    python exporter.py exemple.bpmn \
        --lane "Preparation"

    python exporter.py exemple.bpmn --all

Dépendances :
    pip install cairosvg pillow

Changements par rapport à la V2 (conformité BPMN 2.0)
--------------------------------------------------------
1. Associations de données (dataInputAssociation / dataOutputAssociation,
   ex. les flux vers un data store) : elles étaient dessinées comme des
   flèches pleines identiques aux flux de séquence. Elles sont
   maintenant en pointillés fins avec une flèche ouverte, conformément
   à la norme (bien distinctes d'un sequenceFlow).
2. Événements intermédiaires (catch/throw) et boundary events : cercle
   double (norme BPMN), au lieu d'un simple cercle à trait épaissi. Les
   événements de fin gardent un trait unique épais, les débuts un trait
   unique fin. Un boundary event non-interruptif (cancelActivity=
   "false") ou un start event non-interruptif (isInterrupting="false")
   est tracé en pointillés.
3. Icônes de déclencheurs d'événements étendues à toute la norme :
   message, timer, error, escalation, signal, conditional, compensate,
   terminate, cancel, multiple, parallel-multiple (en plus de link déjà
   présent), avec la distinction plein (throw) / contour (catch) là où
   la norme l'exige.
4. Gateways : ajout des symboles event-based (pentagone dans un cercle)
   et complex (astérisque), en plus de exclusive / parallel / inclusive.
5. Icônes de type de tâche (user / manual / service / send / receive /
   business rule / script) repositionnées en haut à gauche de la tâche
   (comme Camunda Modeler / bpmn.io), avec un ajustement automatique et
   borné du libellé pour éviter tout chevauchement sans faire déborder
   les noms longs.
6. Marqueurs d'activité ajoutés, en bas de la tâche : boucle (flèche
   circulaire), multi-instance séquentielle / parallèle (barres),
   ad-hoc (tilde), compensation (double triangle) — lus depuis les
   véritables éléments BPMN (multiInstanceLoopCharacteristics,
   standardLoopCharacteristics, isForCompensation, adHocSubProcess).
7. Call activity : bordure épaisse ; sous-processus "transaction" :
   double bordure — pour les distinguer visuellement d'un sous-processus
   classique, comme le prévoit la norme.
8. Groupe (artefact BPMN "group") : rectangle à grands coins arrondis,
   en pointillés, sans remplissage (au lieu d'être confondu avec une
   forme générique).

Changements par rapport à la V1
--------------------------------
1. BUG CRITIQUE corrigé : le pool (participant) et les sous-processus
   dépliés (subProcess isExpanded) étaient dessinés avec un fond blanc
   OPAQUE après les flèches dans l'ordre du document SVG. Comme SVG peint
   dans l'ordre d'apparition, ce fond blanc recouvrait entièrement toutes
   les flèches (sequenceFlow) qui passaient sous ces zones : sur le
   --full et le --process, aucune connexion n'était visible. Correctif :
   le rendu se fait maintenant en 3 passes dans cet ordre :
       1) fonds des conteneurs (pool, lanes, sous-processus dépliés)
       2) flèches / flux
       3) formes "feuilles" (tâches, événements, gateways, data...)
   Les flèches sont ainsi toujours dessinées AU-DESSUS des fonds de
   conteneurs et EN DESSOUS des formes, comme dans n'importe quel outil
   BPMN.
2. Les libellés (noms) des événements, gateways, objets/entrepôts de
   données utilisent maintenant la position réellement enregistrée dans
   le diagramme (bpmndi:BPMNLabel / dc:Bounds) quand elle existe, au lieu
   d'un simple centrage sous la forme. Cela évite les chevauchements de
   texte (ex : le nom d'un événement qui recouvrait le nom de la lane).
3. Les noms portés par les flux de séquence (ex : "Oui" / "Non" sur les
   branches d'une passerelle exclusive) sont désormais dessinés, en
   utilisant leur bpmndi:BPMNLabel si présent, sinon le milieu du tracé.
"""

import argparse
import html
import math
import re
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

import cairosvg
from PIL import Image


# ---------------------------------------------------------------------------
# Namespaces BPMN
# ---------------------------------------------------------------------------

NS = {
    "bpmn": "http://www.omg.org/spec/BPMN/20100524/MODEL",
    "bpmndi": "http://www.omg.org/spec/BPMN/20100524/DI",
    "dc": "http://www.omg.org/spec/DD/20100524/DC",
    "di": "http://www.omg.org/spec/DD/20100524/DI",
}

BPMN = "{" + NS["bpmn"] + "}"
BPMNDI = "{" + NS["bpmndi"] + "}"
DC = "{" + NS["dc"] + "}"
DI = "{" + NS["di"] + "}"

# Tags dont la forme se comporte comme un "conteneur" : leur fond doit
# être peint AVANT les flèches, sous peine de les masquer.
CONTAINER_TAGS = {"participant", "lane"}
EXPANDABLE_TAGS = {"subProcess", "callActivity", "adHocSubProcess", "transaction"}

# Marqueur BPMN 2.0 à dessiner en haut de chaque type de tâche :
# sans marqueur, une tâche "abstraite" perd son type.
TASK_MARKERS = {
    "userTask": "user",
    "manualTask": "manual",
    "serviceTask": "service",
    "sendTask": "send",
    "receiveTask": "receive",
    "businessRuleTask": "business",
    "scriptTask": "script",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def local_name(tag):
    """Retourne le nom local d'une balise XML."""
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def esc(value):
    """Echappe une chaîne pour SVG."""
    return html.escape(str(value), quote=True)


def safe_filename(value):
    """Transforme une valeur en nom de fichier sûr."""
    value = str(value).strip()
    value = re.sub(r"[^\w\-\.]+", "_", value, flags=re.UNICODE)
    return value.strip("_") or "export"


def clean_text_content(value):
    """Normalise un texte multi-lignes (ex. contenu d'une bpmn:text) en une
    chaîne unique, espaces/retours à la ligne réduits — le retour à la
    ligne est ensuite recalculé par wrap_text() selon la largeur de la
    forme, comme pour n'importe quel autre libellé."""
    if not value:
        return ""
    return " ".join(str(value).split())


def attr(element, name, default=None):
    """Lecture robuste d'un attribut."""
    if element is None:
        return default
    return element.get(name, default)


def wrap_text(text, max_chars):
    """Découpage grossier d'un texte en lignes sur les espaces."""

    words = str(text).split()

    lines = []
    current = ""

    for word in words:
        candidate = word if not current else current + " " + word

        if len(candidate) > max_chars and current:
            lines.append(current)
            current = word
        else:
            current = candidate

    if current:
        lines.append(current)

    return lines


# ---------------------------------------------------------------------------
# Structures
# ---------------------------------------------------------------------------

class Bounds:
    def __init__(self, x=0, y=0, width=0, height=0):
        self.x = float(x)
        self.y = float(y)
        self.width = float(width)
        self.height = float(height)

    @property
    def right(self):
        return self.x + self.width

    @property
    def bottom(self):
        return self.y + self.height

    @property
    def center_x(self):
        return self.x + self.width / 2

    @property
    def center_y(self):
        return self.y + self.height / 2


class Shape:
    def __init__(self, element_id, bpmn_element, bounds,
                 label_bounds=None, is_expanded=True):
        self.id = element_id
        self.bpmn_element = bpmn_element
        self.bounds = bounds
        # Position explicite du libellé (bpmndi:BPMNLabel/dc:Bounds),
        # si le diagramme en fournit une. None si absente.
        self.label_bounds = label_bounds
        # Pertinent uniquement pour subProcess / callActivity.
        self.is_expanded = is_expanded


class Edge:
    def __init__(self, element_id, bpmn_element, label_bounds=None):
        self.id = element_id
        self.bpmn_element = bpmn_element
        self.points = []
        self.label_bounds = label_bounds


# ---------------------------------------------------------------------------
# BPMN parser
# ---------------------------------------------------------------------------

class BPMNDocument:
    def __init__(self, filename):
        self.filename = Path(filename)

        if not self.filename.exists():
            raise FileNotFoundError(
                f"Fichier BPMN introuvable : {self.filename}"
            )

        self.tree = ET.parse(self.filename)
        self.root = self.tree.getroot()

        self.elements = {}
        self.shapes = {}
        self.edges = {}

        self.processes = {}
        self.lanes = {}

        # Suivi des plans BPMN-DI (bpmndi:BPMNDiagram / BPMNPlane). Un
        # fichier BPMN peut contenir plusieurs diagrammes : le principal
        # (collaboration ou processus racine) et, pour chaque sous-processus
        # replié dont le contenu a été détaillé séparément dans l'outil de
        # modélisation, un diagramme dédié dont le bpmnElement est l'id de
        # ce sous-processus. Sans cette distinction, les formes du
        # diagramme de détail (positionnées dans leur propre repère de
        # coordonnées) se retrouveraient mélangées à celles du diagramme
        # principal lors du rendu.
        self.main_plane_id = None
        self.detail_plane_ids = []          # ordre d'apparition dans le fichier
        self.subprocess_diagrams = {}       # id sous-processus -> élément XML
        self._shape_plane = {}              # id de BPMNShape -> id de plan
        self._edge_plane = {}               # id de BPMNEdge -> id de plan

        self._parse()

    # ---------------------------------------------------------------------

    @staticmethod
    def _read_label_bounds(di_element):
        """Lit bpmndi:BPMNLabel/dc:Bounds s'il existe et est valide."""

        label = di_element.find(f"{BPMNDI}BPMNLabel")

        if label is None:
            return None

        bounds = label.find(f"{DC}Bounds")

        if bounds is None:
            return None

        width = float(bounds.get("width", 0) or 0)
        height = float(bounds.get("height", 0) or 0)

        if width <= 0 or height <= 0:
            return None

        return Bounds(
            bounds.get("x", 0),
            bounds.get("y", 0),
            width,
            height,
        )

    # ---------------------------------------------------------------------

    def _parse(self):
        """Parse le modèle BPMN et le BPMN-DI."""

        # Tous les éléments BPMN possédant un id, ainsi qu'une table
        # enfant -> parent (utile pour résoudre les associations de
        # données, dont la source ou la cible implicite est l'activité
        # qui les contient).
        self._parent_id = {}

        for element in self.root.iter():
            element_id = element.get("id")

            if element_id:
                self.elements[element_id] = element

            parent_id = element_id

            for child in element:
                child_id = child.get("id")

                if child_id:
                    self._parent_id[child_id] = parent_id

        # Processus
        for process in self.root.findall(f".//{BPMN}process"):
            process_id = process.get("id")

            if process_id:
                self.processes[process_id] = process

        # Lanes
        for lane in self.root.findall(f".//{BPMN}lane"):
            lane_id = lane.get("id")

            if lane_id:
                self.lanes[lane_id] = lane

        # BPMN-DI : un ou plusieurs diagrammes, chacun avec son propre plan
        # et son propre repère de coordonnées. Le premier diagramme du
        # fichier est le diagramme principal (collaboration ou processus
        # racine) ; tout diagramme supplémentaire est un diagramme de
        # détail pour le sous-processus replié dont l'id est son
        # bpmnElement (ouvert séparément dans l'outil de modélisation).
        for diagram in self.root.findall(f".//{BPMNDI}BPMNDiagram"):
            plane = diagram.find(f"{BPMNDI}BPMNPlane")

            if plane is None:
                continue

            plane_id = plane.get("bpmnElement")

            if not plane_id:
                continue

            if self.main_plane_id is None:
                self.main_plane_id = plane_id
            elif plane_id != self.main_plane_id:
                self.detail_plane_ids.append(plane_id)
                subprocess_element = self.elements.get(plane_id)
                if subprocess_element is not None:
                    self.subprocess_diagrams[plane_id] = subprocess_element

            self._parse_plane(plane, plane_id)

    # ---------------------------------------------------------------------

    def _parse_plane(self, plane, plane_id):
        """Parse les BPMNShape/BPMNEdge d'un seul plan (voir _parse)."""

        for shape in plane.findall(f".//{BPMNDI}BPMNShape"):
            element_id = shape.get("id")
            bpmn_element = shape.get("bpmnElement")

            bounds = shape.find(f"{DC}Bounds")

            if not element_id or not bpmn_element or bounds is None:
                continue

            # Norme BPMN-DI : isExpanded vaut false par défaut quand absent
            # (un sous-processus dont la forme ne porte pas cet attribut,
            # avec des dimensions de tâche simple, est replié).
            is_expanded = shape.get("isExpanded", "false").lower() == "true"

            self.shapes[element_id] = Shape(
                element_id,
                bpmn_element,
                Bounds(
                    bounds.get("x", 0),
                    bounds.get("y", 0),
                    bounds.get("width", 0),
                    bounds.get("height", 0),
                ),
                label_bounds=self._read_label_bounds(shape),
                is_expanded=is_expanded,
            )
            self._shape_plane[element_id] = plane_id

        for edge in plane.findall(f".//{BPMNDI}BPMNEdge"):
            edge_id = edge.get("id")
            bpmn_element = edge.get("bpmnElement")

            if not edge_id or not bpmn_element:
                continue

            result = Edge(
                edge_id,
                bpmn_element,
                label_bounds=self._read_label_bounds(edge),
            )

            for waypoint in edge.findall(f"{DI}waypoint"):
                try:
                    x = float(waypoint.get("x", 0))
                    y = float(waypoint.get("y", 0))
                    result.points.append((x, y))
                except (TypeError, ValueError):
                    pass

            self.edges[edge_id] = result
            self._edge_plane[edge_id] = plane_id

    # ---------------------------------------------------------------------

    def get_subprocess_name(self, subprocess_id):
        """Nom d'un sous-processus détaillé dans son propre diagramme."""

        element = self.subprocess_diagrams.get(subprocess_id)

        if element is None:
            return subprocess_id

        return element.get("name") or element.get("id") or subprocess_id

    # ---------------------------------------------------------------------

    def subprocess_elements(self, subprocess_id):
        """Ids BPMN appartenant à un sous-processus détaillé (lui-même inclus)."""

        element = self.elements.get(subprocess_id)

        if element is None:
            return set()

        result = {subprocess_id}

        for descendant in element.iter():
            descendant_id = descendant.get("id")

            if descendant_id:
                result.add(descendant_id)

        return result

    # ---------------------------------------------------------------------

    def get_process_name(self, process_id):
        process = self.processes.get(process_id)

        if process is None:
            return process_id

        return process.get("name") or process.get("id") or process_id

    # ---------------------------------------------------------------------

    def get_lane_name(self, lane_id):
        lane = self.lanes.get(lane_id)

        if lane is None:
            return lane_id

        return lane.get("name") or lane.get("id") or lane_id

    # ---------------------------------------------------------------------

    def shape_for_element(self, element_id):
        for shape in self.shapes.values():
            if shape.bpmn_element == element_id:
                return shape
        return None

    # ---------------------------------------------------------------------

    def process_elements(self, process_id):
        """Retourne les ids BPMN appartenant au processus."""

        process = self.processes.get(process_id)

        if process is None:
            return set()

        result = {process_id}

        for element in process.iter():
            element_id = element.get("id")

            if element_id:
                result.add(element_id)

        return result

    # ---------------------------------------------------------------------

    def lane_elements(self, lane_id):
        """Retourne les ids des éléments à afficher pour une lane.

        Au-delà des flowNodeRef directs (ce que la norme assigne
        réellement à une lane), on complète avec :
          - tout le contenu imbriqué d'un sous-processus / call activity
            / transaction / ad-hoc affiché dans la lane : ses éléments
            internes n'ont pas de rattachement de lane propre (la norme
            ne le permet pas sans un laneSet imbriqué), mais ils doivent
            visuellement apparaître puisque le conteneur qui les
            contient est bien dans cette lane ;
          - les objets/entrepôts de données reliés par une association
            (dataInputAssociation/dataOutputAssociation) à un élément
            déjà retenu : ce ne sont pas des "flow nodes" au sens BPMN
            (une lane ne les référence donc jamais), mais les faire
            disparaître d'un export par lane romprait les liens
            visibles sur le diagramme complet.
        """

        lane = self.lanes.get(lane_id)

        if lane is None:
            return set()

        result = {lane_id}

        for ref in lane.findall(f".//{BPMN}flowNodeRef"):
            if not ref.text:
                continue

            node_id = ref.text.strip()
            result.add(node_id)

            node_element = self.elements.get(node_id)

            if node_element is not None:
                for descendant in node_element.iter():
                    descendant_id = descendant.get("id")

                    if descendant_id:
                        result.add(descendant_id)

        # Ne partir que des éléments propres à la lane (avant tout ajout
        # de données externes) : un seul saut lane -> objet de données,
        # jamais plus loin. Sans ce garde-fou, deux tâches de lanes
        # différentes reliées au même entrepôt de données finiraient
        # par s'importer mutuellement de proche en proche.
        base = set(result)

        for element in self.elements.values():
            if local_name(element.tag) not in (
                "dataInputAssociation", "dataOutputAssociation",
            ):
                continue

            source, target = self.resolve_endpoints(element)

            if source in base and target:
                result.add(target)
            elif target in base and source:
                result.add(source)

        return result

    # ---------------------------------------------------------------------

    def resolve_endpoints(self, element):
        """Retourne (source_id, target_id) pour un élément de flux.

        La plupart des flux (sequenceFlow, messageFlow, association)
        portent sourceRef/targetRef comme attributs XML. Les
        associations de données (dataInputAssociation /
        dataOutputAssociation) les portent en revanche comme éléments
        enfants, et omettent souvent l'extrémité implicite : l'activité
        qui contient l'association elle-même.
        """

        source = element.get("sourceRef")
        target = element.get("targetRef")

        if source is None:
            child = element.find(f"{BPMN}sourceRef")
            if child is not None and child.text:
                source = child.text.strip()

        if target is None:
            child = element.find(f"{BPMN}targetRef")
            if child is not None and child.text:
                target = child.text.strip()

        if local_name(element.tag) in ("dataInputAssociation", "dataOutputAssociation"):
            parent_id = self._parent_id.get(element.get("id"))

            if source is None:
                source = parent_id

            if target is None:
                target = parent_id

        return source, target

    # ---------------------------------------------------------------------

    def connected_edges(self, element_ids):
        """Retourne les flux connectés aux éléments sélectionnés."""

        result = []

        for edge in self.edges.values():
            element = self.elements.get(edge.bpmn_element)

            if element is None:
                continue

            source, target = self.resolve_endpoints(element)

            if source in element_ids and target in element_ids:
                result.append(edge)

        return result

    # ---------------------------------------------------------------------

    def all_shape_bounds(self, element_ids=None, plane_id=None):
        """Calcule le rectangle englobant des shapes (et de leurs libellés).

        `plane_id`, si fourni, restreint le calcul aux formes de ce plan
        BPMN-DI (voir _parse_plane) : indispensable quand le fichier
        contient un diagramme de détail pour un sous-processus replié,
        dont les coordonnées sont dans un repère différent du diagramme
        principal.
        """

        selected = []

        for shape in self.shapes.values():
            if element_ids is not None:
                if shape.bpmn_element not in element_ids:
                    continue

            if plane_id is not None and self._shape_plane.get(shape.id) != plane_id:
                continue

            selected.append(shape.bounds)

            if shape.label_bounds is not None:
                selected.append(shape.label_bounds)

        if not selected:
            return Bounds(0, 0, 100, 100)

        min_x = min(b.x for b in selected)
        min_y = min(b.y for b in selected)
        max_x = max(b.right for b in selected)
        max_y = max(b.bottom for b in selected)

        return Bounds(
            min_x,
            min_y,
            max_x - min_x,
            max_y - min_y,
        )


# ---------------------------------------------------------------------------
# SVG renderer
# ---------------------------------------------------------------------------

class SVGRenderer:
    def __init__(
        self,
        document,
        padding=30,
        scale=1.0,
    ):
        self.document = document
        self.padding = padding
        self.scale = scale

    # ---------------------------------------------------------------------

    def _visible_shapes(self, element_ids, plane_id):
        doc = self.document

        for shape in doc.shapes.values():
            if element_ids is not None and shape.bpmn_element not in element_ids:
                continue

            if doc._shape_plane.get(shape.id) != plane_id:
                continue

            element = doc.elements.get(shape.bpmn_element)

            if element is None:
                continue

            yield shape, element, local_name(element.tag)

    # ---------------------------------------------------------------------

    def _visible_edges(self, element_ids, plane_id):
        doc = self.document

        for edge in doc.edges.values():
            if doc._edge_plane.get(edge.id) != plane_id:
                continue

            element = doc.elements.get(edge.bpmn_element)

            if element is None:
                continue

            if element_ids is not None:
                source, target = doc.resolve_endpoints(element)

                if source not in element_ids or target not in element_ids:
                    continue

            yield edge, element

    # ---------------------------------------------------------------------

    def render(self, element_ids=None, highlight_ids=None, plane_id=None):
        """Génère le SVG correspondant à la sélection.

        Rendu en 3 passes pour garantir un empilement correct :
          1) fonds des conteneurs (pool / lane / sous-processus déplié)
          2) flèches (sequenceFlow, messageFlow, association)
          3) formes "feuilles" (tâches, événements, gateways, data...)

        `highlight_ids`, si fourni, est un ensemble d'ids d'éléments BPMN
        (bpmnElement) à entourer d'un halo visuel, dessiné juste après les
        flèches et avant les formes : cela permet de repérer un élément
        précis (une activité, un scénario) sur un diagramme complet, sans
        changer le rendu des autres éléments.

        `plane_id` sélectionne le diagramme BPMN-DI à dessiner (voir
        BPMNDocument._parse_plane) : par défaut, le diagramme principal du
        fichier (document.main_plane_id). Pour dessiner le diagramme de
        détail d'un sous-processus replié, passer son id (présent dans
        document.detail_plane_ids) — typiquement avec
        element_ids=document.subprocess_elements(plane_id). Mélanger les
        formes de deux plans différents n'aurait pas de sens : leurs
        coordonnées ne partagent pas le même repère.
        """

        doc = self.document
        plane_id = plane_id or doc.main_plane_id

        bounds = doc.all_shape_bounds(element_ids, plane_id=plane_id)

        width = (bounds.width + 2 * self.padding) * self.scale
        height = (bounds.height + 2 * self.padding) * self.scale

        if width <= 0:
            width = 100

        if height <= 0:
            height = 100

        offset_x = bounds.x - self.padding
        offset_y = bounds.y - self.padding

        svg = []

        svg.append(
            f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'width="{width:.2f}" '
            f'height="{height:.2f}" '
            f'viewBox="0 0 {width / self.scale:.2f} '
            f'{height / self.scale:.2f}">'
        )

        svg.append("""
<defs>
  <marker
      id="arrow"
      markerWidth="10"
      markerHeight="10"
      refX="9"
      refY="3"
      orient="auto"
      markerUnits="strokeWidth">
    <path d="M0,0 L0,6 L9,3 z" fill="#333"/>
  </marker>
  <marker
      id="openarrow"
      markerWidth="10"
      markerHeight="10"
      refX="8"
      refY="3"
      orient="auto"
      markerUnits="strokeWidth">
    <path d="M0,0 L8,3 L0,6" fill="none" stroke="#777" stroke-width="1"/>
  </marker>
</defs>
""")

        svg.append(
            '<rect x="0" y="0" '
            f'width="{width / self.scale:.2f}" '
            f'height="{height / self.scale:.2f}" '
            'fill="white"/>'
        )

        # Transformation des coordonnées BPMN vers le SVG local
        svg.append(
            f'<g transform="translate({-offset_x:.2f},{-offset_y:.2f}) '
            f'scale({self.scale:.4f})">'
        )

        # --- Passe 1 : fonds des conteneurs -------------------------------
        for shape, element, tag in self._visible_shapes(element_ids, plane_id):
            if tag in CONTAINER_TAGS:
                self._draw_shape(svg, shape, element, tag)
            elif tag in EXPANDABLE_TAGS and shape.is_expanded:
                self._draw_container_box(svg, shape, tag)

        # --- Passe 2 : flèches ---------------------------------------------
        for edge, element in self._visible_edges(element_ids, plane_id):
            self._draw_edge(svg, edge, element)

        # --- Passe 2bis : halos de mise en évidence -------------------------
        if highlight_ids:
            for shape, element, tag in self._visible_shapes(element_ids, plane_id):
                if shape.bpmn_element in highlight_ids:
                    self._draw_highlight(svg, shape.bounds)

        # --- Passe 3 : formes feuilles --------------------------------------
        for shape, element, tag in self._visible_shapes(element_ids, plane_id):
            if tag in CONTAINER_TAGS:
                continue

            if tag in EXPANDABLE_TAGS and shape.is_expanded:
                # Le rectangle a déjà été peint en passe 1 : on ne
                # rajoute ici que le petit marqueur +/- et le nom.
                self._draw_expand_marker(svg, shape.bounds)
                self._draw_activity_marker(svg, shape.bounds, element, tag)
                if element.get("name"):
                    self._draw_container_label(
                        svg, shape.bounds, element.get("name")
                    )
                continue

            self._draw_shape(svg, shape, element, tag)

        svg.append("</g>")
        svg.append("</svg>")

        return "\n".join(svg)

    # ---------------------------------------------------------------------
    # Mise en évidence (repérage d'un élément sur le diagramme complet)
    # ---------------------------------------------------------------------

    @staticmethod
    def _draw_highlight(svg, b, color="#fb8500"):
        """Halo visuel autour d'une forme, pour la repérer sur le diagramme.

        Dessiné après les flèches et avant les formes : la forme d'origine
        (fond blanc) reste peinte par-dessus, seul un liseré coloré dépasse
        tout autour, sans jamais masquer le texte ni l'icône de la forme.
        """

        pad = 6

        svg.append(
            f'<rect x="{b.x - pad:.2f}" y="{b.y - pad:.2f}" '
            f'width="{b.width + 2 * pad:.2f}" '
            f'height="{b.height + 2 * pad:.2f}" '
            'rx="10" ry="10" '
            f'fill="{color}" fill-opacity="0.16" '
            f'stroke="{color}" stroke-width="3" stroke-dasharray="6,3"/>'
        )

    # ---------------------------------------------------------------------
    # Conteneurs (pool / lane / sous-processus déplié)
    # ---------------------------------------------------------------------

    def _draw_container_box(self, svg, shape, tag):
        """Fond (uniquement) d'un sous-processus/call activity déplié.

        Norme BPMN 2.0 : une call activity se distingue par un contour
        épais ; une sub-process "transaction" par un double contour.
        """

        b = shape.bounds
        radius = 8
        stroke_width = 3 if tag == "callActivity" else 1.5

        svg.append(
            f'<rect x="{b.x}" y="{b.y}" '
            f'width="{b.width}" height="{b.height}" '
            f'rx="{radius}" ry="{radius}" '
            f'fill="white" stroke="#333" stroke-width="{stroke_width}"/>'
        )

        if tag == "transaction":
            inset = 4
            svg.append(
                f'<rect x="{b.x + inset}" y="{b.y + inset}" '
                f'width="{b.width - 2 * inset}" '
                f'height="{b.height - 2 * inset}" '
                f'rx="{max(radius - inset, 2)}" ry="{max(radius - inset, 2)}" '
                'fill="none" stroke="#333" stroke-width="1"/>'
            )

    # ---------------------------------------------------------------------

    def _draw_expand_marker(self, svg, b, is_expanded=True):
        """Marqueur +/- de sous-processus (norme BPMN 2.0) : un sous-processus
        déplié porte un "-", un sous-processus replié porte un "+"."""

        size = 14

        x = b.center_x - size / 2
        y = b.bottom - size - 5

        svg.append(
            f'<rect x="{x}" y="{y}" '
            f'width="{size}" height="{size}" '
            'fill="white" stroke="#333" stroke-width="1"/>'
        )

        svg.append(
            f'<line x1="{x + 3}" y1="{y + size / 2}" '
            f'x2="{x + size - 3}" y2="{y + size / 2}" '
            'stroke="#333" stroke-width="1"/>'
        )

        if not is_expanded:
            svg.append(
                f'<line x1="{x + size / 2}" y1="{y + 3}" '
                f'x2="{x + size / 2}" y2="{y + size - 3}" '
                'stroke="#333" stroke-width="1"/>'
            )

    # ---------------------------------------------------------------------

    def _draw_container_label(self, svg, b, name):
        """Nom d'un sous-processus déplié, en haut à gauche du cadre."""

        svg.append(
            f'<text x="{b.x + 10}" y="{b.y + 16}" '
            'font-family="Arial, sans-serif" '
            'font-size="12" '
            'text-anchor="start">'
            f'{esc(name)}</text>'
        )

    # ---------------------------------------------------------------------
    # Flèches
    # ---------------------------------------------------------------------

    def _draw_edge(self, svg, edge, element):
        if len(edge.points) < 2:
            return

        points = " ".join(f"{x:.2f},{y:.2f}" for x, y in edge.points)

        tag = local_name(element.tag)

        if tag == "messageFlow":
            svg.append(
                f'<polyline points="{points}" '
                'fill="none" stroke="#555" stroke-width="1.5" '
                'stroke-dasharray="6,4" marker-end="url(#arrow)"/>'
            )

        elif tag == "association":
            svg.append(
                f'<polyline points="{points}" '
                'fill="none" stroke="#777" stroke-width="1" '
                'stroke-dasharray="3,3"/>'
            )

        elif tag in ("dataInputAssociation", "dataOutputAssociation"):
            # Norme BPMN 2.0 : trait fin pointillé, flèche ouverte.
            svg.append(
                f'<polyline points="{points}" '
                'fill="none" stroke="#777" stroke-width="1" '
                'stroke-dasharray="3,3" marker-end="url(#openarrow)"/>'
            )

        else:
            svg.append(
                f'<polyline points="{points}" '
                'fill="none" stroke="#333" stroke-width="1.5" '
                'marker-end="url(#arrow)"/>'
            )

        name = element.get("name")

        if name:
            self._draw_edge_label(svg, edge, name)

    # ---------------------------------------------------------------------

    def _draw_edge_label(self, svg, edge, name):
        if edge.label_bounds is not None:
            lb = edge.label_bounds
            cx = lb.center_x
            top_y = lb.y + 11
            max_width = max(lb.width, 40)
        else:
            # Repli : milieu du tracé.
            mid_index = len(edge.points) // 2
            cx, mid_y = edge.points[min(mid_index, len(edge.points) - 1)]
            top_y = mid_y - 8
            max_width = 90

        self._draw_multiline_text(
            svg, cx, top_y, name, max_width, anchor_mode="top",
        )

    # ---------------------------------------------------------------------
    # Dispatch des formes
    # ---------------------------------------------------------------------

    def _draw_shape(self, svg, shape, element, tag):
        b = shape.bounds
        name = element.get("name", "")

        if tag == "textAnnotation":
            # Une textAnnotation ne porte pas d'attribut "name" : son texte
            # est le contenu de son enfant <bpmn:text>.
            text_child = element.find(f"{BPMN}text")
            name = clean_text_content(text_child.text) if text_child is not None else ""

        if tag == "participant":
            self._draw_participant(svg, b, name)

        elif tag == "lane":
            self._draw_lane(svg, b, name)

        elif tag.endswith("Event"):
            self._draw_event(svg, element, shape, tag, name)

        elif tag.endswith("Gateway"):
            self._draw_gateway(svg, shape, tag, name)

        elif (
            tag == "task"
            or tag.endswith("Task")
            or tag in ("subProcess", "callActivity", "adHocSubProcess", "transaction")
        ):
            # Ici uniquement les tâches "simples" ou les sous-processus
            # repliés (isExpanded=False) : les sous-processus dépliés
            # sont gérés séparément (voir render()).
            self._draw_task(svg, b, tag, name, element)

        elif tag in ("dataObjectReference", "dataStoreReference"):
            self._draw_data(svg, shape, name)

        elif tag == "textAnnotation":
            self._draw_annotation(svg, b, name)

        elif tag == "group":
            self._draw_group(svg, b)

        else:
            self._draw_generic(svg, b, name)

    # ---------------------------------------------------------------------

    def _draw_participant(self, svg, b, name):
        svg.append(
            f'<rect x="{b.x}" y="{b.y}" '
            f'width="{b.width}" height="{b.height}" '
            'fill="white" stroke="#333" stroke-width="1.5"/>'
        )

        label_width = min(30, b.width)

        svg.append(
            f'<rect x="{b.x}" y="{b.y}" '
            f'width="{label_width}" height="{b.height}" '
            'fill="#f4f4f4" stroke="#333" stroke-width="1"/>'
        )

        if name:
            svg.append(
                f'<text x="{b.x + label_width / 2}" '
                f'y="{b.center_y}" '
                'font-family="Arial, sans-serif" '
                'font-size="12" '
                'text-anchor="middle" '
                f'transform="rotate(-90 {b.x + label_width / 2} {b.center_y})">'
                f'{esc(name)}</text>'
            )

    # ---------------------------------------------------------------------

    def _draw_lane(self, svg, b, name):
        svg.append(
            f'<rect x="{b.x}" y="{b.y}" '
            f'width="{b.width}" height="{b.height}" '
            'fill="none" stroke="#777" stroke-width="1"/>'
        )

        label_width = min(30, b.width)

        if name:
            svg.append(
                f'<text x="{b.x + label_width / 2}" '
                f'y="{b.center_y}" '
                'font-family="Arial, sans-serif" '
                'font-size="11" '
                'text-anchor="middle" '
                f'transform="rotate(-90 {b.x + label_width / 2} {b.center_y})">'
                f'{esc(name)}</text>'
            )

    # ---------------------------------------------------------------------

    def _draw_event(self, svg, element, shape, tag, name):
        """Dessine un événement selon la norme BPMN 2.0.

        Épaisseur/nombre de traits du cercle :
          - start (et sous-catégories)         : 1 trait fin
          - intermediate (catch/throw) et
            boundary                           : 2 traits (double cercle)
          - end                                : 1 trait épais
        Un événement non interruptif (boundary cancelActivity="false"
        ou start d'un event sub-process isInterrupting="false") est
        tracé en pointillés.
        """

        b = shape.bounds
        radius = min(b.width, b.height) / 2
        cx, cy = b.center_x, b.center_y

        is_boundary = tag == "boundaryEvent"
        is_double_ring = tag in (
            "intermediateCatchEvent", "intermediateThrowEvent", "boundaryEvent",
        )
        is_throw = tag == "endEvent" or "Throw" in tag

        non_interrupting = (
            (is_boundary and element.get("cancelActivity", "true").lower() == "false")
            or (
                tag == "startEvent"
                and element.get("isInterrupting", "true").lower() == "false"
            )
        )
        dash = ' stroke-dasharray="4,3"' if non_interrupting else ""

        stroke_width = 3.5 if tag == "endEvent" else (1.3 if is_double_ring else 1.5)

        svg.append(
            f'<circle cx="{cx:.2f}" cy="{cy:.2f}" r="{radius:.2f}" fill="white" '
            f'stroke="#333" stroke-width="{stroke_width}"{dash}/>'
        )

        if is_double_ring:
            inner_r = max(radius - 4, radius * 0.7)
            svg.append(
                f'<circle cx="{cx:.2f}" cy="{cy:.2f}" r="{inner_r:.2f}" '
                f'fill="none" stroke="#333" stroke-width="{stroke_width}"{dash}/>'
            )

        kind = self._event_definition(element)

        if kind:
            self._draw_event_icon(svg, cx, cy, radius, kind, is_throw)

        label_text = self._event_label_text(element, tag, name)

        if label_text:
            self._draw_standalone_label(
                svg, shape, label_text,
                default_x=cx, default_top_y=b.bottom + 15,
                default_width=max(b.width * 2.2, 80),
            )

    # ---------------------------------------------------------------------

    @staticmethod
    def _event_definition(element):
        """Retourne le type de déclencheur BPMN de l'événement (message,
        timer, error, escalation, signal, conditional, compensate,
        terminate, cancel, link, multiple, parallelmultiple) ou None
        pour un événement "none" (sans déclencheur)."""

        kinds = [
            local_name(child.tag)[: -len("EventDefinition")].lower()
            for child in element
            if local_name(child.tag).endswith("EventDefinition")
        ]

        if not kinds:
            return None

        if len(kinds) > 1:
            is_parallel = element.get("parallelMultiple") == "true"
            return "parallelmultiple" if is_parallel else "multiple"

        return kinds[0]

    # ---------------------------------------------------------------------

    @staticmethod
    def _event_label_text(element, tag, name):
        """Ajoute le nom du link event au libellé de l'événement."""

        for child in element:
            if local_name(child.tag) != "linkEventDefinition":
                continue

            link_name = child.get("name")

            if name and link_name and link_name != name:
                return f"{name}  ({link_name})"

            break

        return name

    # ---------------------------------------------------------------------
    # Icônes des déclencheurs d'événements (norme BPMN 2.0)
    # ---------------------------------------------------------------------

    def _draw_event_icon(self, svg, cx, cy, radius, kind, is_throw):
        """Dessine le symbole de déclencheur au centre d'un événement.

        `is_throw` contrôle le remplissage pour les déclencheurs dont la
        norme distingue catch (contour) et throw (plein) : message,
        signal, multiple, link. Les autres (timer, error, escalation,
        conditional, compensate, terminate, cancel) ont un tracé fixe.
        """

        handler = {
            "message": self._icon_message,
            "timer": self._icon_timer,
            "error": self._icon_error,
            "escalation": self._icon_escalation,
            "signal": self._icon_signal,
            "conditional": self._icon_conditional,
            "compensate": self._icon_compensate,
            "terminate": self._icon_terminate,
            "cancel": self._icon_cancel,
            "link": self._icon_link,
            "multiple": self._icon_multiple,
            "parallelmultiple": self._icon_parallel_multiple,
        }.get(kind)

        if handler:
            handler(svg, cx, cy, radius, is_throw)

    # ---------------------------------------------------------------------

    @staticmethod
    def _icon_link(svg, cx, cy, r, is_throw):
        """Double chevron >> : plein pour throw, contour pour catch."""

        for x1, x2 in (
            (cx - r * 0.34, cx - r * 0.06),
            (cx - r * 0.06, cx + r * 0.22),
        ):
            fill = "#333" if is_throw else "none"
            svg.append(
                f'<polyline points="{x1:.2f},{cy - r * 0.16:.2f} '
                f'{x2:.2f},{cy:.2f} '
                f'{x1:.2f},{cy + r * 0.16:.2f}" '
                f'fill="{fill}" stroke="#333" stroke-width="1.5"/>'
            )

    # ---------------------------------------------------------------------

    @staticmethod
    def _icon_message(svg, cx, cy, r, is_throw):
        """Enveloppe : pleine (throw) ou blanche à contour (catch)."""

        w, h = r * 1.1, r * 0.72
        left, right = cx - w / 2, cx + w / 2
        top = cy - h / 2

        fill = "#333" if is_throw else "white"
        flap_stroke = "white" if is_throw else "#333"

        svg.append(
            f'<rect x="{left:.2f}" y="{top:.2f}" width="{w:.2f}" '
            f'height="{h:.2f}" fill="{fill}" stroke="#333" '
            'stroke-width="1.2"/>'
        )
        svg.append(
            f'<polyline points="{left:.2f},{top:.2f} '
            f'{cx:.2f},{cy + h * 0.12:.2f} {right:.2f},{top:.2f}" '
            f'fill="none" stroke="{flap_stroke}" stroke-width="1.2"/>'
        )

    # ---------------------------------------------------------------------

    @staticmethod
    def _icon_timer(svg, cx, cy, r, is_throw):
        """Horloge : cadran, 12 graduations, 2 aiguilles."""

        face_r = r * 0.62

        svg.append(
            f'<circle cx="{cx:.2f}" cy="{cy:.2f}" r="{face_r:.2f}" '
            'fill="white" stroke="#333" stroke-width="1.2"/>'
        )

        for i in range(12):
            angle = math.pi * 2 * i / 12
            inner, outer = face_r * 0.80, face_r * 0.98
            svg.append(
                f'<line x1="{cx + inner * math.sin(angle):.2f}" '
                f'y1="{cy - inner * math.cos(angle):.2f}" '
                f'x2="{cx + outer * math.sin(angle):.2f}" '
                f'y2="{cy - outer * math.cos(angle):.2f}" '
                'stroke="#333" stroke-width="1"/>'
            )

        svg.append(
            f'<line x1="{cx:.2f}" y1="{cy:.2f}" x2="{cx:.2f}" '
            f'y2="{cy - face_r * 0.62:.2f}" stroke="#333" '
            'stroke-width="1.3"/>'
        )
        svg.append(
            f'<line x1="{cx:.2f}" y1="{cy:.2f}" '
            f'x2="{cx + face_r * 0.46:.2f}" y2="{cy + face_r * 0.30:.2f}" '
            'stroke="#333" stroke-width="1.3"/>'
        )

    # ---------------------------------------------------------------------

    @staticmethod
    def _icon_error(svg, cx, cy, r, is_throw):
        """Éclair en zigzag, toujours plein (norme BPMN)."""

        s = r * 0.95
        points = [
            (cx - 0.45 * s, cy + 0.50 * s),
            (cx - 0.12 * s, cy - 0.10 * s),
            (cx + 0.08 * s, cy + 0.22 * s),
            (cx + 0.45 * s, cy - 0.50 * s),
            (cx + 0.14 * s, cy + 0.06 * s),
            (cx - 0.06 * s, cy - 0.26 * s),
        ]
        points_str = " ".join(f"{x:.2f},{y:.2f}" for x, y in points)
        svg.append(
            f'<polygon points="{points_str}" fill="#333" '
            'stroke="#333" stroke-width="0.5"/>'
        )

    # ---------------------------------------------------------------------

    @staticmethod
    def _icon_escalation(svg, cx, cy, r, is_throw):
        """Flèche vers le haut : pleine (throw) ou contour (catch)."""

        s = r * 0.62
        fill = "#333" if is_throw else "none"
        points = (
            f"{cx:.2f},{cy - s:.2f} "
            f"{cx + s * 0.85:.2f},{cy + s * 0.7:.2f} "
            f"{cx:.2f},{cy + s * 0.25:.2f} "
            f"{cx - s * 0.85:.2f},{cy + s * 0.7:.2f}"
        )
        svg.append(
            f'<polygon points="{points}" fill="{fill}" '
            'stroke="#333" stroke-width="1.2"/>'
        )

    # ---------------------------------------------------------------------

    @staticmethod
    def _icon_signal(svg, cx, cy, r, is_throw):
        """Triangle : plein (throw) ou contour (catch)."""

        s = r * 0.68
        fill = "#333" if is_throw else "none"
        points = (
            f"{cx:.2f},{cy - s:.2f} "
            f"{cx + s * 0.87:.2f},{cy + s * 0.62:.2f} "
            f"{cx - s * 0.87:.2f},{cy + s * 0.62:.2f}"
        )
        svg.append(
            f'<polygon points="{points}" fill="{fill}" '
            'stroke="#333" stroke-width="1.2"/>'
        )

    # ---------------------------------------------------------------------

    @staticmethod
    def _icon_conditional(svg, cx, cy, r, is_throw):
        """Petite liste (document à lignes horizontales)."""

        w, h = r * 0.62, r * 0.9
        left, top = cx - w / 2, cy - h / 2

        svg.append(
            f'<rect x="{left:.2f}" y="{top:.2f}" width="{w:.2f}" '
            f'height="{h:.2f}" fill="white" stroke="#333" '
            'stroke-width="1.1"/>'
        )

        for frac in (0.28, 0.5, 0.72):
            y = top + frac * h
            svg.append(
                f'<line x1="{left + w * 0.15:.2f}" y1="{y:.2f}" '
                f'x2="{left + w * 0.85:.2f}" y2="{y:.2f}" '
                'stroke="#333" stroke-width="1"/>'
            )

    # ---------------------------------------------------------------------

    @staticmethod
    def _icon_compensate(svg, cx, cy, r, is_throw):
        """Deux triangles ("rembobinage") : pleins pour throw."""

        s = r * 0.55
        fill = "#333" if is_throw else "white"

        p1 = (
            f"{cx + s * 0.15:.2f},{cy - s * 0.7:.2f} "
            f"{cx + s * 0.15:.2f},{cy + s * 0.7:.2f} "
            f"{cx - s * 0.55:.2f},{cy:.2f}"
        )
        p2 = (
            f"{cx + s * 0.95:.2f},{cy - s * 0.7:.2f} "
            f"{cx + s * 0.95:.2f},{cy + s * 0.7:.2f} "
            f"{cx + s * 0.25:.2f},{cy:.2f}"
        )

        for points in (p1, p2):
            svg.append(
                f'<polygon points="{points}" fill="{fill}" '
                'stroke="#333" stroke-width="1"/>'
            )

    # ---------------------------------------------------------------------

    @staticmethod
    def _icon_terminate(svg, cx, cy, r, is_throw):
        """Disque plein (uniquement valide sur un end event)."""

        svg.append(
            f'<circle cx="{cx:.2f}" cy="{cy:.2f}" r="{r * 0.55:.2f}" '
            'fill="#333"/>'
        )

    # ---------------------------------------------------------------------

    @staticmethod
    def _icon_cancel(svg, cx, cy, r, is_throw):
        """Grand X (événement d'annulation)."""

        s = r * 0.6

        for dx1, dy1, dx2, dy2 in ((-s, -s, s, s), (-s, s, s, -s)):
            svg.append(
                f'<line x1="{cx + dx1:.2f}" y1="{cy + dy1:.2f}" '
                f'x2="{cx + dx2:.2f}" y2="{cy + dy2:.2f}" '
                'stroke="#333" stroke-width="2"/>'
            )

    # ---------------------------------------------------------------------

    @staticmethod
    def _icon_multiple(svg, cx, cy, r, is_throw):
        """Pentagone : plein (throw) ou contour (catch)."""

        s = r * 0.62
        fill = "#333" if is_throw else "none"
        pts = []

        for i in range(5):
            angle = -math.pi / 2 + i * 2 * math.pi / 5
            pts.append((cx + s * math.cos(angle), cy + s * math.sin(angle)))

        points_str = " ".join(f"{x:.2f},{y:.2f}" for x, y in pts)
        svg.append(
            f'<polygon points="{points_str}" fill="{fill}" '
            'stroke="#333" stroke-width="1.2"/>'
        )

    # ---------------------------------------------------------------------

    @staticmethod
    def _icon_parallel_multiple(svg, cx, cy, r, is_throw):
        """Croix épaisse (événement multiple parallèle)."""

        s = r * 0.55
        svg.append(
            f'<line x1="{cx - s:.2f}" y1="{cy:.2f}" '
            f'x2="{cx + s:.2f}" y2="{cy:.2f}" '
            'stroke="#333" stroke-width="2.2"/>'
        )
        svg.append(
            f'<line x1="{cx:.2f}" y1="{cy - s:.2f}" '
            f'x2="{cx:.2f}" y2="{cy + s:.2f}" '
            'stroke="#333" stroke-width="2.2"/>'
        )

    # ---------------------------------------------------------------------

    def _draw_gateway(self, svg, shape, tag, name):
        b = shape.bounds
        cx, cy = b.center_x, b.center_y

        points = [(cx, b.y), (b.right, cy), (cx, b.bottom), (b.x, cy)]
        points_string = " ".join(f"{x:.2f},{y:.2f}" for x, y in points)

        svg.append(
            f'<polygon points="{points_string}" '
            'fill="white" stroke="#333" stroke-width="1.5"/>'
        )

        if tag == "exclusiveGateway":
            svg.append(
                f'<line x1="{cx - 7}" y1="{cy - 7}" '
                f'x2="{cx + 7}" y2="{cy + 7}" '
                'stroke="#333" stroke-width="1.5"/>'
            )
            svg.append(
                f'<line x1="{cx + 7}" y1="{cy - 7}" '
                f'x2="{cx - 7}" y2="{cy + 7}" '
                'stroke="#333" stroke-width="1.5"/>'
            )

        elif tag == "parallelGateway":
            svg.append(
                f'<line x1="{cx - 8}" y1="{cy}" x2="{cx + 8}" y2="{cy}" '
                'stroke="#333" stroke-width="2"/>'
            )
            svg.append(
                f'<line x1="{cx}" y1="{cy - 8}" x2="{cx}" y2="{cy + 8}" '
                'stroke="#333" stroke-width="2"/>'
            )

        elif tag == "inclusiveGateway":
            svg.append(
                f'<circle cx="{cx}" cy="{cy}" r="8" '
                'fill="none" stroke="#333" stroke-width="1.5"/>'
            )

        elif tag == "eventBasedGateway":
            # Cercle externe fin + petit pentagone intérieur (norme BPMN
            # pour la variante "exclusive event-based", la plus courante).
            svg.append(
                f'<circle cx="{cx}" cy="{cy}" r="9" '
                'fill="none" stroke="#333" stroke-width="1"/>'
            )
            pts = []
            for i in range(5):
                angle = -math.pi / 2 + i * 2 * math.pi / 5
                pts.append((cx + 5.5 * math.cos(angle), cy + 5.5 * math.sin(angle)))
            points_str = " ".join(f"{x:.2f},{y:.2f}" for x, y in pts)
            svg.append(
                f'<polygon points="{points_str}" fill="none" '
                'stroke="#333" stroke-width="1"/>'
            )

        elif tag == "complexGateway":
            # Astérisque : 3 traits croisés à 60°.
            for angle_deg in (90, 210, 330):
                angle = math.radians(angle_deg)
                dx, dy = 8 * math.cos(angle), 8 * math.sin(angle)
                svg.append(
                    f'<line x1="{cx - dx:.2f}" y1="{cy - dy:.2f}" '
                    f'x2="{cx + dx:.2f}" y2="{cy + dy:.2f}" '
                    'stroke="#333" stroke-width="1.6"/>'
                )

        if name:
            self._draw_standalone_label(
                svg, shape, name,
                default_x=cx, default_top_y=b.bottom + 15,
                default_width=max(b.width * 2.2, 80),
            )

    # ---------------------------------------------------------------------

    def _draw_task(self, svg, b, tag, name, element=None):
        radius = 8
        stroke_width = 3 if tag == "callActivity" else 1.5

        svg.append(
            f'<rect x="{b.x}" y="{b.y}" '
            f'width="{b.width}" height="{b.height}" '
            f'rx="{radius}" ry="{radius}" '
            f'fill="white" stroke="#333" stroke-width="{stroke_width}"/>'
        )

        if tag in ("subProcess", "callActivity", "adHocSubProcess", "transaction"):
            # Cette branche (_draw_task, appelée depuis _draw_shape en passe
            # feuille) n'est atteinte que pour un sous-processus/call activity
            # REPLIÉ : un sous-processus déplié est intercepté plus tôt dans
            # render() et dessiné via _draw_container_box + le marqueur "-"
            # (voir plus haut). Ici, c'est donc toujours un "+".
            self._draw_expand_marker(svg, b, is_expanded=False)

        marker = TASK_MARKERS.get(tag)

        if marker:
            self._draw_task_marker(svg, b, marker)

        if element is not None:
            self._draw_activity_marker(svg, b, element, tag)

        if name:
            if marker:
                self._draw_task_label_avoiding_icon(svg, b, name)
            else:
                self._draw_centered_multiline_text(svg, b, name)

    # ---------------------------------------------------------------------

    def _draw_task_label_avoiding_icon(self, svg, b, name):
        """Centre le libellé dans la boîte, en le décalant vers le bas
        du minimum nécessaire pour dégager l'icône de type (coin haut
        gauche) — jamais plus que ce que la hauteur de la boîte permet,
        pour éviter de faire déborder un libellé sur plusieurs lignes.
        """

        max_chars = max(10, int(b.width / 7))
        lines = wrap_text(name, max_chars)

        if not lines:
            return

        line_height = 14
        block_half = (len(lines) - 1) * line_height / 2

        icon_bottom = b.y + 5 + self._task_marker_size(b) * 1.1 + 3
        natural_center = b.center_y
        natural_top = natural_center - block_half - 8

        shift = max(0.0, icon_bottom - natural_top)
        max_shift = max(0.0, (b.bottom - 6) - (natural_center + block_half + 6))

        center_y = natural_center + min(shift, max_shift)

        self._draw_centered_multiline_text(svg, b, name, center_y=center_y)

    # ---------------------------------------------------------------------
    # Marqueurs de types de tâches (BPMN 2.0) : coin supérieur gauche
    # ---------------------------------------------------------------------

    @staticmethod
    def _task_marker_size(b):
        return max(12.0, min(min(b.width, b.height) * 0.35, 18.0))

    def _draw_task_marker(self, svg, b, kind):
        """Dessine le marqueur de type de tâche en haut à gauche, comme
        le font les outils de référence (Camunda Modeler, bpmn.io)."""

        size = self._task_marker_size(b)
        cx = b.x + 6 + size / 2
        top = b.y + 5
        stroke = "stroke=\"#333\" stroke-width=\"1\""

        if kind == "user":
            # Tête + épaules (forme simplifiée du type "User Task").
            svg.append(
                f'<circle cx="{cx:.2f}" cy="{top + 0.22 * size:.2f}" '
                f'r="{0.16 * size:.2f}" fill="white" {stroke}/>'
            )
            svg.append(
                f'<path d="M {cx - 0.25 * size:.2f} {top + 0.94 * size:.2f} '
                f'Q {cx:.2f} {top + 0.62 * size:.2f} '
                f'{cx + 0.25 * size:.2f} {top + 0.94 * size:.2f} '
                f'v {0.06 * size:.2f} '
                f'H {cx - 0.25 * size:.2f} z" fill="none" {stroke}/>'
            )

        elif kind == "manual":
            # Main simplifiée : paume + doigts.
            for i in range(4):
                fx = cx - 0.18 * size + i * 0.12 * size
                svg.append(
                    f'<line x1="{fx:.2f}" y1="{top + 0.22 * size:.2f}" '
                    f'x2="{fx:.2f}" y2="{top + 0.56 * size:.2f}" '
                    'stroke="#333" stroke-width="1.2" '
                    'stroke-linecap="round"/>'
                )
            svg.append(
                f'<rect x="{cx - 0.22 * size:.2f}" '
                f'y="{top + 0.56 * size:.2f}" '
                f'width="{0.44 * size:.2f}" '
                f'height="{0.42 * size:.2f}" '
                'rx="3" fill="white" stroke="#333" stroke-width="1"/>'
            )

        elif kind == "service":
            # Engrenage simplifié : moyeu + rayons.
            svg.append(
                f'<circle cx="{cx:.2f}" cy="{top + 0.5 * size:.2f}" '
                f'r="{0.16 * size:.2f}" fill="white" '
                'stroke="#333" stroke-width="1.2"/>'
            )
            for i in range(8):
                angle = i * math.pi / 4
                cos_a = math.cos(angle)
                sin_a = math.sin(angle)
                svg.append(
                    f'<line '
                    f'x1="{cx + 0.20 * size * cos_a:.2f}" '
                    f'y1="{top + 0.5 * size + 0.20 * size * sin_a:.2f}" '
                    f'x2="{cx + 0.34 * size * cos_a:.2f}" '
                    f'y2="{top + 0.5 * size + 0.34 * size * sin_a:.2f}" '
                    'stroke="#333" stroke-width="1.2"/>'
                )

        elif kind in ("send", "receive"):
            # Enveloppe + triangle orienté (send : vers l'extérieur,
            # receive : vers l'intérieur de l'enveloppe).
            left = cx - 0.25 * size
            right = cx + 0.25 * size
            envelope_top = top + 0.26 * size
            envelope_bottom = top + 0.72 * size

            svg.append(
                f'<rect x="{left:.2f}" y="{envelope_top:.2f}" '
                f'width="{0.50 * size:.2f}" '
                f'height="{envelope_bottom - envelope_top:.2f}" '
                'fill="white" stroke="#333" stroke-width="1"/>'
            )
            svg.append(
                f'<polyline points="{left:.2f},{envelope_top:.2f} '
                f'{cx:.2f},{envelope_top + 0.18 * size:.2f} '
                f'{right:.2f},{envelope_top:.2f}" '
                f'fill="none" {stroke}/>'
            )

            if kind == "send":
                points = (
                    f"{right:.2f},{envelope_top + 0.10 * size:.2f} "
                    f"{right:.2f},{envelope_bottom - 0.10 * size:.2f} "
                    f"{right + 0.16 * size:.2f},{top + 0.5 * size:.2f}"
                )
                fill = "#333"
            else:
                points = (
                    f"{left:.2f},{envelope_top + 0.10 * size:.2f} "
                    f"{left:.2f},{envelope_bottom - 0.10 * size:.2f} "
                    f"{left - 0.16 * size:.2f},{top + 0.5 * size:.2f}"
                )
                fill = "none"

            svg.append(
                f'<polygon points="{points}" fill="{fill}" '
                f'{"stroke=\"#333\" stroke-width=\"1\""
                   if kind == "receive" else ""}/>'
            )

        elif kind == "business":
            # Tableau de règles : grille simple.
            left = cx - 0.24 * size
            right = cx + 0.24 * size
            top_table = top + 0.26 * size
            bottom_table = top + 0.74 * size

            svg.append(
                f'<rect x="{left:.2f}" y="{top_table:.2f}" '
                f'width="{0.48 * size:.2f}" '
                f'height="{bottom_table - top_table:.2f}" '
                'fill="white" stroke="#333" stroke-width="1"/>'
            )
            svg.append(
                f'<line x1="{cx - 0.10 * size:.2f}" '
                f'y1="{top_table:.2f}" '
                f'x2="{cx - 0.10 * size:.2f}" '
                f'y2="{bottom_table:.2f}" '
                f'stroke="#333" stroke-width="1"/>'
            )
            for frac in (0.20, 0.40):
                y = top_table + frac * (bottom_table - top_table)
                svg.append(
                    f'<line x1="{left:.2f}" y1="{y:.2f}" '
                    f'x2="{right:.2f}" y2="{y:.2f}" '
                    f'stroke="#333" stroke-width="1"/>'
                )

        elif kind == "script":
            # Page pliée simplifiée : feuille lignée.
            left = cx - 0.20 * size
            top_page = top + 0.20 * size

            svg.append(
                f'<rect x="{left:.2f}" y="{top_page:.2f}" '
                f'width="{0.40 * size:.2f}" '
                f'height="{0.58 * size:.2f}" '
                'rx="1" fill="white" stroke="#333" stroke-width="1"/>'
            )
            svg.append(
                f'<line x1="{cx - 0.12 * size:.2f}" '
                f'y1="{top_page + 0.18 * size:.2f}" '
                f'x2="{cx + 0.12 * size:.2f}" '
                f'y2="{top_page + 0.18 * size:.2f}" '
                f'stroke="#333" stroke-width="1"/>'
            )
            svg.append(
                f'<line x1="{cx - 0.12 * size:.2f}" '
                f'y1="{top_page + 0.32 * size:.2f}" '
                f'x2="{cx + 0.12 * size:.2f}" '
                f'y2="{top_page + 0.32 * size:.2f}" '
                f'stroke="#333" stroke-width="1"/>'
            )
            svg.append(
                f'<line x1="{cx - 0.12 * size:.2f}" '
                f'y1="{top_page + 0.46 * size:.2f}" '
                f'x2="{cx - 0.02 * size:.2f}" '
                f'y2="{top_page + 0.46 * size:.2f}" '
                f'stroke="#333" stroke-width="1"/>'
            )

    # ---------------------------------------------------------------------
    # Marqueurs d'activité (BPMN 2.0) : bas et centré
    # ---------------------------------------------------------------------

    @staticmethod
    def _activity_marker_kind(element, tag):
        """Détermine le marqueur d'activité à afficher (norme BPMN 2.0) :
        boucle, multi-instance séquentielle/parallèle, ad-hoc ou
        compensation. Une seule icône de "cardinalité" (loop/MI/ad-hoc)
        est affichée, la compensation pouvant s'y ajouter."""

        if element is None:
            return []

        markers = []

        for child in element:
            child_tag = local_name(child.tag)

            if child_tag == "multiInstanceLoopCharacteristics":
                is_sequential = child.get("isSequential") == "true"
                markers.append(
                    "multi-sequential" if is_sequential else "multi-parallel"
                )
                break

            if child_tag == "standardLoopCharacteristics":
                markers.append("loop")
                break

        if tag == "adHocSubProcess":
            markers.append("adhoc")

        if element.get("isForCompensation") == "true":
            markers.append("compensation")

        return markers

    # ---------------------------------------------------------------------

    def _draw_activity_marker(self, svg, b, element, tag):
        kinds = self._activity_marker_kind(element, tag)

        if not kinds:
            return

        size = 13
        gap = 4
        total_width = len(kinds) * size + (len(kinds) - 1) * gap
        start_x = b.center_x - total_width / 2
        cy = b.bottom - 10

        for index, kind in enumerate(kinds):
            cx = start_x + index * (size + gap) + size / 2
            self._draw_single_activity_marker(svg, cx, cy, size, kind)

    # ---------------------------------------------------------------------

    @staticmethod
    def _draw_single_activity_marker(svg, cx, cy, size, kind):
        s = size / 2

        if kind == "loop":
            # Flèche circulaire (rebouclage).
            r = s * 0.75
            svg.append(
                f'<path d="M {cx - r:.2f} {cy:.2f} '
                f'A {r:.2f} {r:.2f} 0 1 1 {cx + r * 0.2:.2f} {cy + r * 0.97:.2f}" '
                'fill="none" stroke="#333" stroke-width="1.4"/>'
            )
            svg.append(
                f'<polygon points="'
                f'{cx + r * 0.2 - 4:.2f},{cy + r * 0.97 - 5:.2f} '
                f'{cx + r * 0.2 + 5:.2f},{cy + r * 0.97 - 2:.2f} '
                f'{cx + r * 0.2 - 1:.2f},{cy + r * 0.97 + 4:.2f}" '
                'fill="#333"/>'
            )

        elif kind == "multi-sequential":
            # Trois barres verticales.
            for i in range(3):
                x = cx - s * 0.6 + i * s * 0.6
                svg.append(
                    f'<line x1="{x:.2f}" y1="{cy - s:.2f}" '
                    f'x2="{x:.2f}" y2="{cy + s:.2f}" '
                    'stroke="#333" stroke-width="1.8"/>'
                )

        elif kind == "multi-parallel":
            # Trois barres horizontales.
            for i in range(3):
                y = cy - s * 0.6 + i * s * 0.6
                svg.append(
                    f'<line x1="{cx - s:.2f}" y1="{y:.2f}" '
                    f'x2="{cx + s:.2f}" y2="{y:.2f}" '
                    'stroke="#333" stroke-width="1.8"/>'
                )

        elif kind == "adhoc":
            # Tilde ~.
            svg.append(
                f'<path d="M {cx - s:.2f} {cy + s * 0.3:.2f} '
                f'Q {cx - s * 0.4:.2f} {cy - s * 0.8:.2f} '
                f'{cx:.2f} {cy + s * 0.3:.2f} '
                f'Q {cx + s * 0.4:.2f} {cy - s * 0.8:.2f} '
                f'{cx + s:.2f} {cy + s * 0.3:.2f}" '
                'fill="none" stroke="#333" stroke-width="1.8"/>'
            )

        elif kind == "compensation":
            # Deux petits triangles ("rembobinage").
            p1 = (
                f"{cx + s * 0.1:.2f},{cy - s * 0.8:.2f} "
                f"{cx + s * 0.1:.2f},{cy + s * 0.8:.2f} "
                f"{cx - s * 0.7:.2f},{cy:.2f}"
            )
            p2 = (
                f"{cx + s:.2f},{cy - s * 0.8:.2f} "
                f"{cx + s:.2f},{cy + s * 0.8:.2f} "
                f"{cx + s * 0.2:.2f},{cy:.2f}"
            )
            for points in (p1, p2):
                svg.append(
                    f'<polygon points="{points}" fill="white" '
                    'stroke="#333" stroke-width="1"/>'
                )

    # ---------------------------------------------------------------------

    def _draw_data(self, svg, shape, name):
        b = shape.bounds
        fold = min(12, b.width * 0.25)

        points = [
            (b.x, b.y),
            (b.right - fold, b.y),
            (b.right, b.y + fold),
            (b.right, b.bottom),
            (b.x, b.bottom),
        ]
        points_string = " ".join(f"{x:.2f},{y:.2f}" for x, y in points)

        svg.append(
            f'<polygon points="{points_string}" '
            'fill="white" stroke="#333" stroke-width="1.2"/>'
        )
        svg.append(
            f'<line x1="{b.right - fold}" y1="{b.y}" '
            f'x2="{b.right - fold}" y2="{b.y + fold}" '
            'stroke="#333" stroke-width="1"/>'
        )
        svg.append(
            f'<line x1="{b.right - fold}" y1="{b.y + fold}" '
            f'x2="{b.right}" y2="{b.y + fold}" '
            'stroke="#333" stroke-width="1"/>'
        )

        if name:
            self._draw_standalone_label(
                svg, shape, name,
                default_x=b.center_x, default_top_y=b.bottom + 12,
                default_width=max(b.width * 2.5, 80),
            )

    # ---------------------------------------------------------------------

    def _draw_annotation(self, svg, b, name):
        svg.append(
            f'<path d="M {b.x + 8} {b.y} L {b.right} {b.y} '
            f'L {b.right} {b.bottom} L {b.x + 8} {b.bottom}" '
            'fill="white" stroke="#555" stroke-width="1"/>'
        )
        svg.append(
            f'<line x1="{b.x + 8}" y1="{b.y}" x2="{b.x + 8}" y2="{b.bottom}" '
            'stroke="#555" stroke-width="1"/>'
        )

        if name:
            self._draw_centered_multiline_text(svg, b, name)

    # ---------------------------------------------------------------------

    @staticmethod
    def _draw_group(svg, b):
        """Groupe (artefact) : rectangle à grands coins arrondis, en
        pointillés, sans remplissage — norme BPMN 2.0."""

        radius = min(24, b.width / 4, b.height / 4)

        svg.append(
            f'<rect x="{b.x}" y="{b.y}" '
            f'width="{b.width}" height="{b.height}" '
            f'rx="{radius:.2f}" ry="{radius:.2f}" '
            'fill="none" stroke="#555" stroke-width="1.2" '
            'stroke-dasharray="6,3"/>'
        )

    # ---------------------------------------------------------------------

    def _draw_generic(self, svg, b, name):
        svg.append(
            f'<rect x="{b.x}" y="{b.y}" '
            f'width="{b.width}" height="{b.height}" '
            'fill="white" stroke="#555" stroke-width="1"/>'
        )

        if name:
            self._draw_centered_multiline_text(svg, b, name)

    # ---------------------------------------------------------------------
    # Texte
    # ---------------------------------------------------------------------

    def _draw_standalone_label(
        self, svg, shape, text, default_x, default_top_y, default_width,
    ):
        """Libellé externe (événement, gateway, data...).

        Utilise la position bpmndi:BPMNLabel du diagramme si disponible,
        sinon retombe sur une position par défaut sous la forme.
        """

        if shape.label_bounds is not None:
            lb = shape.label_bounds
            self._draw_multiline_text(
                svg, lb.center_x, lb.center_y, text,
                max(lb.width, 30), anchor_mode="center",
            )
        else:
            self._draw_multiline_text(
                svg, default_x, default_top_y, text,
                default_width, anchor_mode="top",
            )

    # ---------------------------------------------------------------------

    def _draw_multiline_text(self, svg, cx, y_ref, text, max_width_px,
                              anchor_mode="top"):
        """Dessine un texte multi-lignes centré horizontalement sur cx.

        anchor_mode="top"    -> y_ref est la ligne de base de la 1ère ligne
        anchor_mode="center" -> y_ref est le centre vertical du bloc
        """

        max_chars = max(8, int(max_width_px / 6.5))
        lines = wrap_text(text, max_chars)

        if not lines:
            return

        line_height = 13

        if anchor_mode == "center":
            start_y = y_ref - ((len(lines) - 1) * line_height) / 2 + 4
        else:
            start_y = y_ref

        for index, line in enumerate(lines):
            y = start_y + index * line_height
            svg.append(
                f'<text x="{cx:.2f}" y="{y:.2f}" '
                'font-family="Arial, sans-serif" '
                'font-size="12" '
                'text-anchor="middle">'
                f'{esc(line)}</text>'
            )

    # ---------------------------------------------------------------------

    def _draw_centered_multiline_text(self, svg, b, text, center_y=None):
        """Nom affiché au centre de la forme (tâches, data, annotations)."""

        if center_y is None:
            center_y = b.center_y

        max_chars = max(10, int(b.width / 7))
        lines = wrap_text(text, max_chars)

        if not lines:
            return

        line_height = 14
        start_y = center_y - ((len(lines) - 1) * line_height) / 2 + 4

        for index, line in enumerate(lines):
            y = start_y + index * line_height
            svg.append(
                f'<text x="{b.center_x}" y="{y:.2f}" '
                'font-family="Arial, sans-serif" '
                'font-size="12" '
                'text-anchor="middle">'
                f'{esc(line)}</text>'
            )


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def svg_to_png(svg, filename):
    cairosvg.svg2png(
        bytestring=svg.encode("utf-8"),
        write_to=str(filename),
    )


def svg_to_jpg(svg, filename):
    """
    CairoSVG produit du PNG avec transparence.
    Pillow est utilisé pour créer un JPG sur fond blanc.
    """

    png_data = cairosvg.svg2png(bytestring=svg.encode("utf-8"))

    import io

    image = Image.open(io.BytesIO(png_data)).convert("RGB")
    image.save(filename, format="JPEG", quality=95)


def export_selection(
    document,
    element_ids,
    output_base,
    formats,
    padding,
    scale,
    plane_id=None,
):
    renderer = SVGRenderer(document, padding=padding, scale=scale)
    svg = renderer.render(element_ids, plane_id=plane_id)

    output_base = Path(output_base)

    if "png" in formats:
        png_file = output_base.with_suffix(".png")
        svg_to_png(svg, png_file)
        print(f"PNG : {png_file}")

    if "jpg" in formats or "jpeg" in formats:
        jpg_file = output_base.with_suffix(".jpg")
        svg_to_jpg(svg, jpg_file)
        print(f"JPG : {jpg_file}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def print_list(document):
    print()
    print("Processes")
    print("---------")

    if not document.processes:
        print("  Aucun processus")
    else:
        for process_id, process in document.processes.items():
            name = process.get("name") or process_id
            print(f"  {process_id} : {name}")

    print()
    print("Lanes")
    print("-----")

    if not document.lanes:
        print("  Aucune lane")
    else:
        for lane_id, lane in document.lanes.items():
            name = lane.get("name") or lane_id
            print(f"  {lane_id} : {name}")

    print()
    print("Sous-processus (diagramme de détail séparé)")
    print("--------------------------------------------")

    if not document.subprocess_diagrams:
        print("  Aucun")
    else:
        for subprocess_id in document.detail_plane_ids:
            print(f"  {subprocess_id} : {document.get_subprocess_name(subprocess_id)}")

    print()


def find_process(document, value):
    if value in document.processes:
        return value

    for process_id, process in document.processes.items():
        if process.get("name") == value:
            return process_id

    raise ValueError(f"Processus introuvable : {value}")


def find_lane(document, value):
    if value in document.lanes:
        return value

    for lane_id, lane in document.lanes.items():
        if lane.get("name") == value:
            return lane_id

    raise ValueError(f"Lane introuvable : {value}")


def find_subprocess(document, value):
    if value in document.subprocess_diagrams:
        return value

    for subprocess_id in document.detail_plane_ids:
        if document.get_subprocess_name(subprocess_id) == value:
            return subprocess_id

    raise ValueError(f"Sous-processus introuvable : {value}")


def main():
    parser = argparse.ArgumentParser(
        description="Exporte un diagramme BPMN en PNG/JPG."
    )

    parser.add_argument("bpmn", help="Fichier BPMN 2.0")

    mode = parser.add_mutually_exclusive_group(required=True)

    mode.add_argument(
        "--list", action="store_true", help="Liste les processus et lanes",
    )
    mode.add_argument(
        "--full", action="store_true", help="Exporte le diagramme complet",
    )
    mode.add_argument(
        "--process", metavar="PROCESS",
        help="Exporte un processus par ID ou nom",
    )
    mode.add_argument(
        "--lane", metavar="LANE", help="Exporte une lane par ID ou nom",
    )
    mode.add_argument(
        "--subprocess", metavar="SUBPROCESS",
        help="Exporte le diagramme de détail d'un sous-processus replié, "
             "par ID ou nom (voir --list)",
    )
    mode.add_argument(
        "--all", action="store_true",
        help="Exporte le diagramme complet, tous les processus et toutes "
             "les lanes",
    )

    parser.add_argument(
        "--output", default="bpmn_exports", help="Répertoire de sortie",
    )
    parser.add_argument(
        "--format", choices=("png", "jpg", "both"), default="both",
        help="Format de sortie",
    )
    parser.add_argument(
        "--padding", type=float, default=30, help="Marge autour du diagramme",
    )
    parser.add_argument(
        "--scale", type=float, default=1.0, help="Facteur d'échelle",
    )

    args = parser.parse_args()

    try:
        document = BPMNDocument(args.bpmn)
    except Exception as exc:
        print(f"Erreur lors de la lecture du BPMN : {exc}", file=sys.stderr)
        return 1

    if args.list:
        print_list(document)
        return 0

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Préfixe tous les fichiers exportés par le nom du fichier BPMN
    # source (ex : "exemple_ref.bpmn" -> "exemple_ref_full.png").
    prefix = safe_filename(Path(args.bpmn).stem) + "_"

    formats = {"png", "jpg"} if args.format == "both" else {args.format}

    # ------------------------------------------------------------------
    # Diagramme complet
    # ------------------------------------------------------------------

    if args.full or args.all:
        print("Export du diagramme complet...")
        export_selection(
            document, None, output_dir / f"{prefix}full", formats,
            args.padding, args.scale,
        )

    # ------------------------------------------------------------------
    # Processus
    # ------------------------------------------------------------------

    if args.process:
        try:
            process_id = find_process(document, args.process)
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 1

        print(f"Export du processus : {document.get_process_name(process_id)}")

        elements = document.process_elements(process_id)

        export_selection(
            document, elements,
            output_dir / f"{prefix}process_{safe_filename(process_id)}",
            formats, args.padding, args.scale,
        )

    # ------------------------------------------------------------------
    # Lanes
    # ------------------------------------------------------------------

    if args.lane:
        try:
            lane_id = find_lane(document, args.lane)
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 1

        print(f"Export de la lane : {document.get_lane_name(lane_id)}")

        elements = document.lane_elements(lane_id)

        export_selection(
            document, elements,
            output_dir / (
                f"{prefix}lane_"
                + safe_filename(document.get_lane_name(lane_id))
            ),
            formats, args.padding, args.scale,
        )

    # ------------------------------------------------------------------
    # Sous-processus (diagramme de détail)
    # ------------------------------------------------------------------

    if args.subprocess:
        try:
            subprocess_id = find_subprocess(document, args.subprocess)
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 1

        print(f"Export du sous-processus : {document.get_subprocess_name(subprocess_id)}")

        elements = document.subprocess_elements(subprocess_id)

        export_selection(
            document, elements,
            output_dir / f"{prefix}subprocess_{safe_filename(subprocess_id)}",
            formats, args.padding, args.scale,
            plane_id=subprocess_id,
        )

    # ------------------------------------------------------------------
    # Tous les processus et lanes
    # ------------------------------------------------------------------

    if args.all:
        for process_id in document.processes:
            print(f"Export processus : {process_id}")
            elements = document.process_elements(process_id)
            export_selection(
                document, elements,
                output_dir / f"{prefix}process_{safe_filename(process_id)}",
                formats, args.padding, args.scale,
            )

        for lane_id in document.lanes:
            lane_name = document.get_lane_name(lane_id)
            print(f"Export lane : {lane_name}")
            elements = document.lane_elements(lane_id)
            export_selection(
                document, elements,
                output_dir / f"{prefix}lane_{safe_filename(lane_name)}",
                formats, args.padding, args.scale,
            )

        for subprocess_id in document.detail_plane_ids:
            subprocess_name = document.get_subprocess_name(subprocess_id)
            print(f"Export sous-processus : {subprocess_name}")
            elements = document.subprocess_elements(subprocess_id)
            export_selection(
                document, elements,
                output_dir / f"{prefix}subprocess_{safe_filename(subprocess_id)}",
                formats, args.padding, args.scale,
                plane_id=subprocess_id,
            )

    print()
    print("Export terminé.")

    return 0


if __name__ == "__main__":
    sys.exit(main())