#!/usr/bin/env python3
"""
bpmn_to_markdown.py

Convertit un ou plusieurs fichiers BPMN 2.0 XML en documents Markdown
compatibles Obsidian.

Production :
- Vue métier : scénarios / user stories regroupées, construits à partir des
  chemins réels du processus (start -> ... -> fin), et non plus par simple
  agrégation "rôle + résultat le plus proche"
- Scénarios alternatifs issus des boundary events (timers, erreurs,
  messages, escalades, etc.)
- Vue atomique : une fiche par activité BPMN
- Critères d'acceptation issus des transitions BPMN, avec distinction
  explicite entre gateways de décision (split) et de synchronisation (join)
- Règles de gestion candidates issues des gateways
- Matrice de traçabilité BPMN -> scénario -> user story

Aucune dépendance externe.
Testé avec Python 3.10+.

Utilisation :
    python3 bpmn_to_markdown.py processus.bpmn -o ./Exigences
    python3 bpmn_to_markdown.py ./bpmn -o ./Exigences
"""

from __future__ import annotations

import argparse
import re
import sys
import unicodedata
import xml.etree.ElementTree as ET

from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


# ---------------------------------------------------------------------------
# BPMN
# ---------------------------------------------------------------------------

BPMN_NS = "http://www.omg.org/spec/BPMN/20100524/MODEL"
CAMUNDA_NS = "http://camunda.org/schema/1.0/bpmn"
NS = {"bpmn": BPMN_NS}

TASK_TAGS = {
    "task",
    "userTask",
    "manualTask",
    "serviceTask",
    "businessRuleTask",
    "sendTask",
    "receiveTask",
    "scriptTask",
    "callActivity",
    "subProcess",
}

HUMAN_TASK_TAGS = {
    "task",
    "userTask",
    "manualTask",
}

TECHNICAL_TASK_TAGS = {
    "serviceTask",
    "scriptTask",
    "businessRuleTask",
    "sendTask",
    "receiveTask",
    "callActivity",
}

GATEWAY_TAGS = {
    "exclusiveGateway",
    "inclusiveGateway",
    "parallelGateway",
    "eventBasedGateway",
    "complexGateway",
}

EVENT_TAGS = {
    "startEvent",
    "endEvent",
    "intermediateCatchEvent",
    "intermediateThrowEvent",
    "boundaryEvent",
}

START_EVENT_TAG = "startEvent"
END_EVENT_TAG = "endEvent"

# Les mots-clés servent uniquement à reconnaître les résultats visibles métier.
RESULT_KEYWORDS = (
    "créé",
    "créée",
    "préparé",
    "préparée",
    "disponible",
    "confirmé",
    "confirmée",
    "terminé",
    "terminée",
    "départ",
    "livré",
    "livrée",
    "expédié",
    "expédiée",
    "validé",
    "validée",
)

# Libellés lisibles pour les définitions d'événement BPMN (timer, erreur, ...)
EVENT_DEFINITION_LABELS = {
    "timer": "un délai (timer)",
    "error": "une erreur",
    "message": "la réception d'un message",
    "escalation": "une escalade",
    "signal": "un signal",
    "compensation": "une compensation",
    "cancel": "une annulation",
    "conditional": "une condition",
    "terminate": "une terminaison forcée",
}

# Libellés lisibles pour les events intermédiaires non nommés.
CATCH_EVENT_LABELS = {
    "message": "Attente de la réception d'un message",
    "timer": "Attente d'un délai (timer)",
    "signal": "Attente d'un signal",
    "conditional": "Attente d'une condition",
    "escalation": "Attente d'une escalade",
    "error": "Attente d'une erreur",
    "cancel": "Attente d'une annulation",
    "compensation": "Attente d'une compensation",
    "link": "Point de reprise du processus (lien)",
}

THROW_EVENT_LABELS = {
    "message": "Envoi d'un message",
    "timer": "Envoi d'un délai (timer)",
    "signal": "Envoi d'un signal",
    "conditional": "Envoi d'une condition",
    "escalation": "Envoi d'une escalade",
    "error": "Envoi d'une erreur",
    "cancel": "Envoi d'une annulation",
    "compensation": "Envoi d'une compensation",
    "link": "Point de bascule du processus (lien)",
}

BOUNDARY_EVENT_LABELS = {
    "message": "Réception d'un message",
    "timer": "Dépassement d'un délai (timer)",
    "signal": "Réception d'un signal",
    "error": "Survenue d'une erreur",
    "escalation": "Survenue d'une escalade",
    "cancel": "Survenue d'une annulation",
    "compensation": "Survenue d'une compensation",
}

# Verbe décrivant la mise en attente du processus après une tâche qui
# débouche sur un intermediateCatchEvent.
WAIT_VERBS = {
    "message": "attend la réception d'un message",
    "timer": "attend l'échéance d'un délai (timer)",
    "signal": "attend la réception d'un signal",
    "conditional": "attend la réalisation d'une condition",
    "escalation": "attend la survenue d'une escalade",
    "error": "attend la survenue d'une erreur",
    "cancel": "attend une annulation",
    "compensation": "attend une compensation",
    "link": "est suspendu puis reprend à partir du lien",
}


# ---------------------------------------------------------------------------
# Modèle interne
# ---------------------------------------------------------------------------

@dataclass
class BpmnNode:
    id: str
    name: str
    tag: str
    lane: str | None = None
    lane_is_inferred: bool = False
    incoming: list[str] = field(default_factory=list)
    outgoing: list[str] = field(default_factory=list)
    attached_to: str | None = None
    cancel_activity: bool = True
    event_definition: str | None = None
    is_multi_instance: bool = False
    is_container: bool = False
    data_inputs: list[str] = field(default_factory=list)
    data_outputs: list[str] = field(default_factory=list)
    applications: list[str] = field(default_factory=list)
    business_rule_refs: list[str] = field(default_factory=list)


@dataclass
class SequenceFlow:
    id: str
    source_ref: str
    target_ref: str
    name: str = ""
    condition: str = ""
    is_default: bool = False


@dataclass
class ProcessModel:
    id: str
    name: str
    nodes: dict[str, BpmnNode] = field(default_factory=dict)
    flows: dict[str, SequenceFlow] = field(default_factory=dict)


@dataclass
class Scenario:
    id: str
    title: str
    role: str
    tasks: list[BpmnNode] = field(default_factory=list)
    start_events: list[BpmnNode] = field(default_factory=list)
    end_events: list[BpmnNode] = field(default_factory=list)
    business_rules: list[str] = field(default_factory=list)
    # Rôle suivant si le chemin métier continue vers un autre acteur.
    handoff_to: str | None = None
    # Tâches déjà parcourues vers lesquelles le scénario reboucle (reprise).
    loops_to: list[BpmnNode] = field(default_factory=list)


@dataclass
class AlternativeScenario:
    """Scénario dérivé d'un boundary event (exception, timeout, erreur...)."""
    id: str
    boundary_event: BpmnNode
    attached_task: BpmnNode | None
    role: str
    targets: list[BpmnNode] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Utilitaires texte
# ---------------------------------------------------------------------------

def clean_text(value: str | None) -> str:
    """Réduit les espaces et sauts de ligne à une chaîne lisible."""
    if not value:
        return ""
    return " ".join(value.split()).strip()


def normalize_text(value: str | None) -> str:
    """Normalise une chaîne pour les comparaisons non sensibles aux accents."""
    value = clean_text(value).lower()
    value = unicodedata.normalize("NFKD", value)
    value = "".join(char for char in value if not unicodedata.combining(char))
    return value


def normalize_lane_name(name: str | None) -> str:
    """Nom du couloir (lane) tel que saisi dans le BPMN, espaces nettoyés.

    Aucune table d'alias : le rôle affiché est exactement le nom du couloir
    (ex. « Contributeur », « Mainteneur / CI »). Sans nom, repli sur
    « Acteur métier non défini ».
    """
    return clean_text(name) or "Acteur métier non défini"


def slugify(value: str) -> str:
    """Construit un nom de fichier stable et lisible."""
    value = normalize_text(value)
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-") or "processus"


def markdown_escape(value: str) -> str:
    """Évite la casse des tableaux Markdown."""
    return clean_text(value).replace("|", r"\|")


def local_name(xml_tag: str) -> str:
    """Extrait le nom local d'une balise XML, avec ou sans namespace."""
    return xml_tag.split("}", 1)[-1]


def collect_data_association_labels(
    activity_element: ET.Element,
    labels: dict[str, str],
    assoc_tag: str,
    ref_tag: str,
) -> list[str]:
    """
    Extrait les libellés des éléments de données liés à une activité via les
    associations de données BPMN. Seules les références présentes dans la
    cartographie `labels` sont retenues (les propriétés internes et les
    références inconnues sont ignorées).
    """
    result: list[str] = []

    for assoc in activity_element.findall(f"bpmn:{assoc_tag}", NS):
        for ref in assoc.findall(f"bpmn:{ref_tag}", NS):
            ref_id = clean_text(ref.text or "")
            if not ref_id or ref_id.startswith("Property_"):
                continue
            label = labels.get(ref_id)
            if label and label not in result:
                result.append(label)

    return result


def readable_event_label(node: BpmnNode) -> str:
    """Libellé lisible d'un événement BPMN non nommé, selon sa définition."""
    tags = {
        "intermediateCatchEvent": CATCH_EVENT_LABELS,
        "intermediateThrowEvent": THROW_EVENT_LABELS,
        "boundaryEvent": BOUNDARY_EVENT_LABELS,
    }
    return tags.get(node.tag, {}).get(node.event_definition or "", "")


def node_label(node: BpmnNode | None) -> str:
    """Retourne le libellé métier d'un nœud BPMN."""
    if node is None:
        return "Élément BPMN inconnu"

    if node.name:
        return clean_text(node.name)

    if node.tag in EVENT_TAGS:
        label = readable_event_label(node)
        if label:
            return label

    return f"{node.tag} ({node.id})"


def quote(value: str) -> str:
    return f"« {value} »"


def resolve_lane(
    element_id: str,
    lane_by_node_id: dict[str, str],
    container_of: dict[str, str],
) -> tuple[str | None, bool]:
    """Lane d'un nœud : directe, sinon héritée de son subprocess contenant.

    Retourne (nom de lane, False) : une lane héritée d'un subprocess est
    une lane réelle du diagramme, pas une déduction technique.
    """
    seen: set[str] = set()
    container = element_id
    while container and container not in seen:
        lane = lane_by_node_id.get(container)
        if lane:
            return lane, False
        seen.add(container)
        container = container_of.get(container)

    return None, False


# ---------------------------------------------------------------------------
# Parsing BPMN
# ---------------------------------------------------------------------------

def parse_bpmn(file_path: Path) -> list[ProcessModel]:
    """
    Lit un fichier BPMN 2.0 et retourne les processus qu'il contient.

    Le script récupère :
    - les lanes et leurs flowNodeRef (avec repli sur les attributs vendor
      camunda:assignee / camunda:candidateGroups si aucune lane n'existe) ;
    - les tâches, en distinguant les subProcess "conteneurs" (qui ont des
      enfants déjà exploités individuellement) des activités atomiques ;
    - les gateways ;
    - les événements, y compris les boundary events (rattachement,
      comportement interruptif, type de déclencheur) ;
    - les sequence flows et leurs conditions ;
    - les activités multi-instances.
    """
    tree = ET.parse(file_path)
    root = tree.getroot()

    lane_by_node_id: dict[str, str] = {}

    for lane in root.findall(".//bpmn:lane", NS):
        lane_name = normalize_lane_name(
            lane.get("name") or lane.get("id") or ""
        )

        for node_ref in lane.findall("bpmn:flowNodeRef", NS):
            if node_ref.text:
                lane_by_node_id[node_ref.text.strip()] = lane_name

    # Un processus n'a souvent pas de nom propre : celui-ci est porté par le
    # participant (pool) qui le référence via processRef.
    participant_names: dict[str, str] = {}
    for participant in root.findall(".//bpmn:participant", NS):
        process_ref = participant.get("processRef")
        participant_name = clean_text(participant.get("name"))
        if process_ref and participant_name:
            participant_names[process_ref] = participant_name

    models: list[ProcessModel] = []

    for process_element in root.findall(".//bpmn:process", NS):
        process_id = process_element.get("id", "process")
        process_name = (
            clean_text(process_element.get("name"))
            or participant_names.get(process_id)
            or process_id
        )

        model = ProcessModel(
            id=process_id,
            name=process_name,
        )

        # Cartographie des objets de données (dataObject / dataObjectReference)
        # et des magasins de données (DataStoreReference = application métier).
        data_labels: dict[str, str] = {}
        store_labels: dict[str, str] = {}
        for element in process_element.iter():
            element_id = element.get("id")
            tag = local_name(element.tag)
            if not element_id:
                continue
            label = clean_text(element.get("name") or element_id)
            if tag in {"dataObject", "dataObjectReference"}:
                data_labels[element_id] = label
            elif tag == "dataStoreReference":
                store_labels[element_id] = label

        # Cartographie nœud -> subprocess contenant, pour hériter de sa lane.
        container_of: dict[str, str] = {}

        def mark_containers(element: ET.Element, stack: list[str]) -> None:
            element_id = element.get("id")
            tag = local_name(element.tag)
            is_flow_node = (
                tag in TASK_TAGS
                or tag in GATEWAY_TAGS
                or tag in EVENT_TAGS
                or tag == "subProcess"
            )
            if element_id and stack and is_flow_node:
                container_of[element_id] = stack[-1]
            if tag == "subProcess" and element_id:
                stack = [*stack, element_id]
            for child in element:
                mark_containers(child, stack)

        mark_containers(process_element, [])

        # Première passe : nœuds BPMN et flux.
        for element in process_element.iter():
            element_id = element.get("id")
            tag = local_name(element.tag)

            if not element_id:
                continue

            if tag in TASK_TAGS or tag in GATEWAY_TAGS or tag in EVENT_TAGS:
                lane, lane_is_inferred = resolve_lane(
                    element_id, lane_by_node_id, container_of
                )

                if not lane:
                    vendor_role = (
                        element.get(f"{{{CAMUNDA_NS}}}assignee")
                        or element.get(f"{{{CAMUNDA_NS}}}candidateGroups")
                    )
                    if vendor_role:
                        lane = clean_text(vendor_role)
                        lane_is_inferred = True

                event_definition = None
                if tag in EVENT_TAGS:
                    for child in element:
                        child_tag = local_name(child.tag)
                        if child_tag.endswith("EventDefinition"):
                            event_definition = child_tag[: -len("EventDefinition")]
                            break

                is_multi_instance = any(
                    local_name(child.tag) == "multiInstanceLoopCharacteristics"
                    for child in element
                )

                data_inputs: list[str] = []
                data_outputs: list[str] = []
                applications: list[str] = []
                if tag in TASK_TAGS:
                    data_inputs = collect_data_association_labels(
                        element, data_labels, "dataInputAssociation", "sourceRef"
                    )
                    data_outputs = collect_data_association_labels(
                        element, data_labels, "dataOutputAssociation", "targetRef"
                    )
                    applications = collect_data_association_labels(
                        element, store_labels, "dataInputAssociation", "sourceRef"
                    )
                    applications.extend(
                        collect_data_association_labels(
                            element, store_labels, "dataOutputAssociation", "targetRef"
                        )
                    )
                    applications = list(dict.fromkeys(applications))

                business_rule_refs: list[str] = []
                if tag == "businessRuleTask":
                    implementation = clean_text(element.get("implementation"))
                    if implementation and implementation != "##unspecified":
                        business_rule_refs.append(f"Implémentation : {implementation}")
                    decision_ref = clean_text(
                        element.get(f"{{{CAMUNDA_NS}}}decisionRef")
                    )
                    if decision_ref:
                        business_rule_refs.append(f"Décision : {decision_ref}")
                    result_var = clean_text(
                        element.get(f"{{{CAMUNDA_NS}}}resultVariable")
                    )
                    if result_var:
                        business_rule_refs.append(f"Variable de résultat : {result_var}")
                    rule_language = clean_text(element.get("ruleLanguage"))
                    if rule_language:
                        business_rule_refs.append(f"Langage de règle : {rule_language}")
                    operation_ref = clean_text(element.findtext("bpmn:operationRef", default="", namespaces=NS))
                    if operation_ref:
                        business_rule_refs.append(f"Opération : {operation_ref}")

                model.nodes[element_id] = BpmnNode(
                    id=element_id,
                    name=clean_text(element.get("name")),
                    tag=tag,
                    lane=lane,
                    lane_is_inferred=lane_is_inferred,
                    attached_to=(
                        element.get("attachedToRef") if tag == "boundaryEvent" else None
                    ),
                    cancel_activity=element.get("cancelActivity", "true") != "false",
                    event_definition=event_definition,
                    is_multi_instance=is_multi_instance,
                    data_inputs=data_inputs,
                    data_outputs=data_outputs,
                    applications=applications,
                    business_rule_refs=business_rule_refs,
                )

            if tag == "sequenceFlow":
                condition_element = element.find(
                    "bpmn:conditionExpression",
                    NS,
                )

                condition = clean_text(
                    condition_element.text
                    if condition_element is not None
                    else ""
                )

                model.flows[element_id] = SequenceFlow(
                    id=element_id,
                    source_ref=element.get("sourceRef", ""),
                    target_ref=element.get("targetRef", ""),
                    name=clean_text(element.get("name")),
                    condition=condition,
                    is_default=False,
                )

        # Un subProcess qui contient lui-même des activités est un simple
        # conteneur : ses enfants génèrent déjà des user stories, on évite
        # donc de dupliquer l'information avec une fiche pour le conteneur.
        for subprocess_element in process_element.iter():
            if local_name(subprocess_element.tag) != "subProcess":
                continue

            subprocess_id = subprocess_element.get("id")
            if not subprocess_id or subprocess_id not in model.nodes:
                continue

            has_inner_task = any(
                child is not subprocess_element and local_name(child.tag) in TASK_TAGS
                for child in subprocess_element.iter()
            )

            if has_inner_task:
                model.nodes[subprocess_id].is_container = True

        # Les gateways peuvent désigner explicitement leur flux par défaut.
        default_flow_ids: set[str] = set()

        for element in process_element.iter():
            tag = local_name(element.tag)

            if tag in GATEWAY_TAGS:
                default_flow = element.get("default")
                if default_flow:
                    default_flow_ids.add(default_flow)

        for flow_id in default_flow_ids:
            if flow_id in model.flows:
                model.flows[flow_id].is_default = True

        # Deuxième passe : rattacher les flux entrants et sortants aux nœuds.
        for flow in model.flows.values():
            source = model.nodes.get(flow.source_ref)
            target = model.nodes.get(flow.target_ref)

            if source:
                source.outgoing.append(flow.id)

            if target:
                target.incoming.append(flow.id)

        models.append(model)

    return models


# ---------------------------------------------------------------------------
# Navigation dans le graphe BPMN
# ---------------------------------------------------------------------------

def outgoing_flows(
    model: ProcessModel,
    node: BpmnNode,
) -> list[SequenceFlow]:
    return [
        model.flows[flow_id]
        for flow_id in node.outgoing
        if flow_id in model.flows
    ]


def incoming_flows(
    model: ProcessModel,
    node: BpmnNode,
) -> list[SequenceFlow]:
    return [
        model.flows[flow_id]
        for flow_id in node.incoming
        if flow_id in model.flows
    ]


def target_node(model: ProcessModel, flow: SequenceFlow) -> BpmnNode | None:
    return model.nodes.get(flow.target_ref)


def source_node(model: ProcessModel, flow: SequenceFlow) -> BpmnNode | None:
    return model.nodes.get(flow.source_ref)


def is_task(node: BpmnNode | None) -> bool:
    return node is not None and node.tag in TASK_TAGS


def is_human_task(node: BpmnNode | None) -> bool:
    return node is not None and node.tag in HUMAN_TASK_TAGS


def is_technical_task(node: BpmnNode | None) -> bool:
    return node is not None and node.tag in TECHNICAL_TASK_TAGS


def is_gateway(node: BpmnNode | None) -> bool:
    return node is not None and node.tag in GATEWAY_TAGS


def is_event(node: BpmnNode | None) -> bool:
    return node is not None and node.tag in EVENT_TAGS


def is_split_gateway(node: BpmnNode | None) -> bool:
    """Une gateway de décision : plus d'un flux sortant."""
    return is_gateway(node) and len(node.outgoing) > 1


def is_join_gateway(node: BpmnNode | None) -> bool:
    """Une gateway de synchronisation : plusieurs flux entrants, un seul (ou aucun) sortant.

    Ce n'est pas une décision et ne doit donc pas générer de règle de type
    "si condition X alors...".
    """
    return is_gateway(node) and len(node.incoming) > 1 and len(node.outgoing) <= 1


def is_named_business_result(node: BpmnNode | None) -> bool:
    """
    Identifie un résultat métier visible.

    Un endEvent est toujours un résultat.
    Un événement intermédiaire nommé avec un mot-clé de résultat est
    aussi considéré comme tel.
    """
    if node is None:
        return False

    if node.tag == END_EVENT_TAG:
        return True

    normalized_name = normalize_text(node.name)

    return (
        node.tag == "intermediateThrowEvent"
        and any(keyword in normalized_name for keyword in RESULT_KEYWORDS)
    )


def find_next_nodes(
    model: ProcessModel,
    start_node_id: str,
    max_depth: int = 40,
) -> list[BpmnNode]:
    """
    Explore le graphe depuis un nœud donné.

    Usage général pour diagnostic, règles et recherche de résultats.
    """
    found: list[BpmnNode] = []
    visited: set[str] = set()
    queue: deque[tuple[str, int]] = deque([(start_node_id, 0)])

    while queue:
        node_id, depth = queue.popleft()

        if node_id in visited or depth > max_depth:
            continue

        visited.add(node_id)
        node = model.nodes.get(node_id)

        if node is None:
            continue

        found.append(node)

        for flow in outgoing_flows(model, node):
            queue.append((flow.target_ref, depth + 1))

    return found


def find_preceding_named_events(
    model: ProcessModel,
    task: BpmnNode,
    max_depth: int = 10,
) -> list[BpmnNode]:
    """
    Recherche des événements amont significatifs.

    Utile pour rattacher les scénarios aux événements tels que :
    - Articles urgents à acheminer ;
    - Arrivée de la navette ;
    - Carton créé ;
    - UM créée.
    """
    found: list[BpmnNode] = []
    visited: set[str] = set()
    queue: deque[tuple[str, int]] = deque([(task.id, 0)])

    while queue:
        node_id, depth = queue.popleft()

        if node_id in visited or depth > max_depth:
            continue

        visited.add(node_id)
        node = model.nodes.get(node_id)

        if node is None:
            continue

        if node.id != task.id and is_event(node) and node.name:
            found.append(node)

        for flow in incoming_flows(model, node):
            queue.append((flow.source_ref, depth + 1))

    return list({event.id: event for event in found}.values())


def find_direct_result_after_task(
    model: ProcessModel,
    task: BpmnNode,
    max_depth: int = 12,
) -> list[BpmnNode]:
    """
    Recherche uniquement le prochain résultat métier proche.

    - on traverse les gateways et les événements techniques ;
    - on s'arrête dès que l'on atteint une autre tâche ;
    - on ne remonte donc pas artificiellement jusqu'à tous les endEvent
      du processus entier.
    """
    results: list[BpmnNode] = []
    visited: set[str] = set()
    queue: deque[tuple[str, int]] = deque([(task.id, 0)])

    while queue:
        node_id, depth = queue.popleft()

        if node_id in visited or depth > max_depth:
            continue

        visited.add(node_id)
        node = model.nodes.get(node_id)

        if node is None:
            continue

        if node.id != task.id and is_named_business_result(node):
            results.append(node)
            continue
        if node.id != task.id and is_task(node):
            continue
        for flow in outgoing_flows(model, node):
            queue.append((flow.target_ref, depth + 1))
    uniq = {}
    for r in results:
        uniq[r.id] = r
    return list(uniq.values())


def find_next_tasks(
    model: ProcessModel,
    task: BpmnNode,
    max_depth: int = 12,
) -> list[BpmnNode]:
    """Prochaines tâches atteintes depuis `task` (gateways/événements traversés)."""
    results: list[BpmnNode] = []
    visited: set[str] = set()
    queue: deque[tuple[str, int]] = deque([(task.id, 0)])

    while queue:
        node_id, depth = queue.popleft()

        if node_id in visited or depth > max_depth:
            continue

        visited.add(node_id)
        node = model.nodes.get(node_id)

        if node is None:
            continue

        if node.id != task.id and is_task(node):
            results.append(node)
            continue

        for flow in outgoing_flows(model, node):
            queue.append((flow.target_ref, depth + 1))

    return list({n.id: n for n in results}.values())


def story_goal(model: ProcessModel, task: BpmnNode) -> str:
    results = find_direct_result_after_task(model, task)
    named = [r.name for r in results if r.name]
    if named:
        return 'atteindre le résultat : ' + ' / '.join(named)
    return 'faire progresser le processus métier'


def criteria_for_task(model: ProcessModel, task: BpmnNode) -> list[str]:
    criteria: list[str] = []

    if task.is_multi_instance:
        criteria.append(
            "Cette action est répétée pour chaque élément de la collection "
            "traitée (comportement multi-instance)."
        )

    for data in task.data_outputs:
        criteria.append(
            f'La donnée de sortie « {data} » est produite à l\'issue de l\'action.'
        )

    flows = outgoing_flows(model, task)
    if not flows:
        criteria.append("La réalisation de l'action est tracée dans le processus.")
        return criteria

    for flow in flows:
        target = model.nodes.get(flow.target_ref)
        condition = flow.condition or flow.name

        if target and target.tag in GATEWAY_TAGS:
            gw_name = node_label(target)
            gw_flows = outgoing_flows(model, target)

            if not gw_flows:
                criteria.append(
                    f'Lorsque « {node_label(task)} » est terminé, '
                    f'la décision « {gw_name} » est évaluée.'
                )
                continue

            if is_join_gateway(target):
                # Point de synchronisation, pas de décision : on ne parle
                # pas de "branche applicable" ou de condition.
                only_target = model.nodes.get(gw_flows[0].target_ref)
                criteria.append(
                    f'Lorsque « {node_label(task)} » est terminé, le processus '
                    f'est synchronisé au niveau de « {gw_name} » puis poursuit '
                    f'vers « {node_label(only_target)} ».'
                )
                continue

            for gf in gw_flows:
                nxt = model.nodes.get(gf.target_ref)
                branch_cond = gf.condition or gf.name
                if branch_cond:
                    criteria.append(
                        f'Étant donné la condition « {branch_cond} », lorsque '
                        f'« {node_label(task)} » est terminé, alors le processus '
                        f'poursuit vers « {node_label(nxt)} ».'
                    )
                else:
                    criteria.append(
                        f'Lorsque « {node_label(task)} » est terminé et que la '
                        f'branche applicable de « {gw_name} » est sélectionnée, '
                        f'alors le processus poursuit vers « {node_label(nxt)} ».'
                    )
        else:
            if target and target.tag == "intermediateCatchEvent":
                verb = WAIT_VERBS.get(target.event_definition or "", "se met en attente")
                if target.name:
                    criteria.append(
                        f'Lorsque « {node_label(task)} » est terminé, le processus '
                        f'{verb} (« {clean_text(target.name)} »).'
                    )
                else:
                    criteria.append(
                        f'Lorsque « {node_label(task)} » est terminé, le processus '
                        f'{verb}.'
                    )
                continue
            if condition:
                criteria.append(
                    f'Lorsque « {node_label(task)} » est terminé et que '
                    f'« {condition} » est vérifié, alors le processus poursuit '
                    f'vers « {node_label(target)} ».'
                )
            else:
                criteria.append(
                    f'Lorsque « {node_label(task)} » est terminé, alors le '
                    f'processus poursuit vers « {node_label(target)} ».'
                )

    return list(dict.fromkeys(criteria))


def rules_for_gateway(model: ProcessModel, gateway: BpmnNode) -> list[str]:
    """
    Génère les règles de gestion candidates d'une gateway.

    Distingue explicitement :
    - les gateways de jointure/synchronisation (join), qui ne sont pas des
      décisions et ne doivent donc pas produire de règle "si condition..." ;
    - les gateways de décision (split), qui produisent une règle par branche
      sortante, plus une règle d'exclusivité ou d'inclusivité selon le type.
    """
    rules: list[str] = []
    gw_name = node_label(gateway)
    flows = outgoing_flows(model, gateway)
    incoming = incoming_flows(model, gateway)

    split = len(flows) > 1
    join = len(incoming) > 1 and len(flows) <= 1

    if join:
        if gateway.tag == 'parallelGateway':
            rules.append(
                f'RG-{gateway.id}: « {gw_name} » synchronise les '
                f'{len(incoming)} branches parallèles entrantes avant de '
                f'poursuivre le processus.'
            )
        elif gateway.tag == 'inclusiveGateway':
            rules.append(
                f'RG-{gateway.id}: « {gw_name} » attend la fin de toutes les '
                f'branches actives avant de poursuivre le processus.'
            )
        # Une exclusiveGateway en jointure n'est qu'une fusion de flux :
        # aucune règle de gestion à documenter.
        return rules

    if gateway.tag == 'parallelGateway':
        if split:
            targets = ', '.join(
                quote(node_label(model.nodes.get(f.target_ref))) for f in flows
            )
            rules.append(
                f'RG-{gateway.id}: après « {gw_name} », toutes les branches '
                f'parallèles suivantes doivent être exécutées : {targets}.'
            )
        return rules

    if not flows:
        rules.append(
            f'RG-{gateway.id}: la décision « {gw_name} » doit être évaluée '
            f'avant la poursuite du processus.'
        )
        return rules

    for idx, flow in enumerate(flows, start=1):
        target = model.nodes.get(flow.target_ref)
        cond = flow.condition or flow.name
        suffix = ' (chemin par défaut)' if flow.is_default else ''
        if cond:
            rules.append(
                f'RG-{gateway.id}-{idx}: si « {cond} »{suffix}, alors orienter '
                f'le processus vers « {node_label(target)} ».'
            )
        else:
            rules.append(
                f'RG-{gateway.id}-{idx}: lorsque la branche applicable de '
                f'« {gw_name} » est retenue, orienter le processus vers '
                f'« {node_label(target)} »{suffix}.'
            )

    if gateway.tag == 'exclusiveGateway' and split:
        rules.append(
            f'RG-{gateway.id}-EXCLUSIVITÉ: une seule branche sortante de '
            f'« {gw_name} » peut être sélectionnée.'
        )
    elif gateway.tag == 'inclusiveGateway' and split:
        rules.append(
            f'RG-{gateway.id}-INCLUSIVITÉ: une ou plusieurs branches de '
            f'« {gw_name} » peuvent être sélectionnées simultanément.'
        )

    return rules


# ---------------------------------------------------------------------------
# Construction des scénarios métier (chemins réels du processus)
# ---------------------------------------------------------------------------

def trace_paths(
    model: ProcessModel,
    max_paths: int = 300,
    max_depth: int = 200,
) -> list[list[str]]:
    """
    Énumère les chemins du processus, des startEvents vers les impasses
    (endEvent, nœud sans flux sortant, ou boucle détectée).

    Le nombre et la profondeur des chemins sont bornés pour éviter une
    explosion combinatoire sur les diagrammes fortement parallèles ou
    comportant des boucles de reprise.
    """
    starts = [n for n in model.nodes.values() if n.tag == START_EVENT_TAG]
    if not starts:
        return []

    paths: list[list[str]] = []
    stack: list[tuple[str, tuple[str, ...]]] = [
        (start.id, (start.id,)) for start in starts
    ]

    while stack and len(paths) < max_paths:
        node_id, path = stack.pop()
        node = model.nodes.get(node_id)

        if node is None or len(path) > max_depth:
            paths.append(list(path))
            continue

        flows = outgoing_flows(model, node)

        if not flows:
            paths.append(list(path))
            continue

        for flow in flows:
            target_id = flow.target_ref
            if target_id in path:
                # Boucle de reprise/retour arrière détectée : on clôt le
                # chemin ici plutôt que de tourner indéfiniment.
                paths.append(list(path))
                continue
            stack.append((target_id, path + (target_id,)))

    return paths


def segment_path_by_role(model: ProcessModel, path: list[str]) -> list[list[BpmnNode]]:
    """
    Découpe un chemin de nœuds en segments consécutifs de tâches
    partageant le même rôle/lane. Les gateways et événements ne coupent
    pas un segment ; seul un changement de rôle entre deux tâches le fait.
    """
    segments: list[list[BpmnNode]] = []
    current: list[BpmnNode] = []
    current_role: str | None = None

    for node_id in path:
        node = model.nodes.get(node_id)
        if not is_task(node) or node.is_container:
            continue

        role = node.lane or 'Acteur métier non défini'
        if current and role != current_role:
            segments.append(current)
            current = []

        current_role = role
        current.append(node)

    if current:
        segments.append(current)

    return segments


def _contains_subsequence(big: tuple[str, ...], small: tuple[str, ...]) -> bool:
    """True si `small` apparaît comme sous-séquence contiguë de `big`."""
    n = len(small)
    return any(big[i:i + n] == small for i in range(len(big) - n + 1))


def build_scenarios(model: ProcessModel) -> list[Scenario]:
    """
    Construit les scénarios métier à partir des chemins réels du processus
    (start -> ... -> fin), découpés à chaque changement de rôle.

    Deux dédoublonnages évitent la redondance :
    - deux segments identiques (même rôle, mêmes tâches) issus de chemins
      différents sont fusionnés (leurs relais éventuels sont cumulés) ;
    - un segment entièrement contenu dans un segment plus long du même rôle
      (cas typique : chaque branche d'une gateway ou d'une boucle de reprise
      prolonge le même enchaînement jusqu'à sa propre fin) est absorbé par
      ce segment plus long. Les branches elles-mêmes restent documentées par
      les règles de gestion des gateways.
    """
    paths = trace_paths(model)
    seen: dict[tuple[str, tuple[str, ...]], Scenario] = {}
    handoffs: dict[tuple[str, tuple[str, ...]], list[str]] = {}
    loops: dict[tuple[str, tuple[str, ...]], list[BpmnNode]] = {}
    order: list[tuple[str, tuple[str, ...]]] = []

    for path in paths:
        segments = segment_path_by_role(model, path)

        for idx, segment in enumerate(segments):
            role = segment[0].lane or 'Acteur métier non défini'
            key = (role, tuple(t.id for t in segment))
            next_role = (
                segments[idx + 1][0].lane or 'Acteur métier non défini'
                if idx + 1 < len(segments) else None
            )

            # Reprise : le dernier segment d'un chemin reboucle vers une
            # tâche déjà parcourue (boucle de correction/retour arrière).
            looped = []
            if idx == len(segments) - 1:
                on_path = set(path)
                looped = [t for t in find_next_tasks(model, segment[-1]) if t.id in on_path]

            if key in seen:
                if next_role and next_role not in handoffs[key]:
                    handoffs[key].append(next_role)
                for t in looped:
                    if t not in loops[key]:
                        loops[key].append(t)
                continue

            sc = Scenario(id='', title=role, role=role, tasks=segment)
            sc.end_events = find_direct_result_after_task(model, segment[-1])
            sc.start_events = find_preceding_named_events(model, segment[0])

            for t in segment:
                for f in outgoing_flows(model, t):
                    tgt = model.nodes.get(f.target_ref)
                    if tgt and tgt.tag in GATEWAY_TAGS:
                        sc.business_rules.extend(rules_for_gateway(model, tgt))
            sc.business_rules = list(dict.fromkeys(sc.business_rules))

            seen[key] = sc
            handoffs[key] = [next_role] if next_role else []
            loops[key] = list(looped)
            order.append(key)

    # Absorption des segments contenus dans un segment plus long du même rôle.
    survivors: list[tuple[str, tuple[str, ...]]] = []
    for key in order:
        role, ids = key
        containers = [
            other for other in order
            if other[0] == role and len(other[1]) > len(ids)
            and _contains_subsequence(other[1], ids)
        ]
        if not containers:
            survivors.append(key)
            continue

        parent = max(containers, key=lambda k: len(k[1]))

        # Les relais vers un autre acteur sont cumulés (ils restent vrais
        # même s'ils partent d'un point intermédiaire du scénario plus long).
        for nxt in handoffs[key]:
            if nxt not in handoffs[parent]:
                handoffs[parent].append(nxt)
        for t in loops[key]:
            if t not in loops[parent]:
                loops[parent].append(t)

        # Un résultat n'est repris que si le segment absorbé finit au même
        # endroit que le parent ; sinon c'est un résultat intermédiaire.
        if parent[1][-len(ids):] == ids:
            for event in seen[key].end_events:
                if event not in seen[parent].end_events:
                    seen[parent].end_events.append(event)

    scenarios: list[Scenario] = []
    for idx, key in enumerate(survivors, start=1):
        sc = seen[key]
        sc.id = f'SC-{idx:02d}'
        sc.handoff_to = ', '.join(handoffs[key]) or None
        sc.loops_to = loops[key]
        scenarios.append(sc)

    if not scenarios:
        # Aucun startEvent exploitable (fichier incomplet, process fragment...) :
        # repli sur un regroupement simple par rôle pour ne rien perdre.
        by_role: dict[str, list[BpmnNode]] = {}
        for task in model.nodes.values():
            if is_task(task) and not task.is_container:
                by_role.setdefault(task.lane or 'Acteur métier non défini', []).append(task)

        for idx, (role, tasks) in enumerate(sorted(by_role.items()), start=1):
            scenarios.append(Scenario(id=f'SC-{idx:02d}', title=role, role=role, tasks=tasks))

    return scenarios


# ---------------------------------------------------------------------------
# Scénarios alternatifs (boundary events)
# ---------------------------------------------------------------------------

def alternative_trigger_label(event: BpmnNode) -> str:
    if event.event_definition and event.event_definition in EVENT_DEFINITION_LABELS:
        return EVENT_DEFINITION_LABELS[event.event_definition]
    if event.name:
        return f"l'événement « {event.name} »"
    return "un événement interruptif"


def build_alternative_scenarios(model: ProcessModel) -> list[AlternativeScenario]:
    """
    Construit un scénario alternatif pour chaque boundary event du
    processus (timeout, erreur, message, escalade...). Ces événements ne
    sont pas atteints par une traversée normale du graphe puisqu'ils sont
    rattachés via `attachedToRef` et non via un flux entrant.
    """
    alternatives: list[AlternativeScenario] = []
    boundary_events = [n for n in model.nodes.values() if n.tag == 'boundaryEvent']

    for idx, event in enumerate(sorted(boundary_events, key=lambda n: n.id), start=1):
        attached_task = model.nodes.get(event.attached_to) if event.attached_to else None
        role = (attached_task.lane if attached_task else None) or 'Acteur métier non défini'
        targets = [
            model.nodes[f.target_ref]
            for f in outgoing_flows(model, event)
            if f.target_ref in model.nodes
        ]

        alternatives.append(AlternativeScenario(
            id=f'SC-ALT-{idx:02d}',
            boundary_event=event,
            attached_task=attached_task,
            role=role,
            targets=targets,
        ))

    return alternatives


# ---------------------------------------------------------------------------
# Rendu Markdown
# ---------------------------------------------------------------------------

def generate_images(
    bpmn_file: Path,
    models: list[ProcessModel],
    images_dir: Path,
    scale: float = 1.0,
    lane_images: bool = False,
    scenario_images: bool = False,
) -> dict[str, dict]:
    """
    Génère les PNG du diagramme avec exporter.py (cairosvg + Pillow requis).

    Retourne {id_processus: {"full": chemin_relatif, "process": chemin|None,
    "lanes": [(nom, chemin)]}}. Les chemins sont relatifs au dossier de sortie
    (ex. "images/fichier--full.png") pour que le Markdown reste portable.
    En cas d'échec (dépendance manquante, pas de BPMNDI), retourne {} et le
    Markdown est généré sans images.
    """
    try:
        import exporter
        document = exporter.BPMNDocument(bpmn_file)
    except Exception as exc:
        print(f'AVERTISSEMENT : images non générées pour {bpmn_file.name} ({exc})', file=sys.stderr)
        return {}

    if not document.shapes:
        print(f'AVERTISSEMENT : {bpmn_file.name} ne contient aucun diagramme (BPMNDI)', file=sys.stderr)
        return {}

    images_dir.mkdir(parents=True, exist_ok=True)
    stem = slugify(bpmn_file.stem)

    def render(element_ids, name: str, highlight=None) -> str:
        path = images_dir / f'{name}.png'
        svg = exporter.SVGRenderer(document, padding=30, scale=scale).render(element_ids, highlight)
        exporter.svg_to_png(svg, path)
        print(f'Image : {path}')
        return f'{images_dir.name}/{path.name}'

    full = render(None, f'{stem}--full')
    result: dict[str, dict] = {}

    for model in models:
        entry: dict = {'full': full, 'process': None, 'lanes': [], 'scenarios': {}}
        process_ids = document.process_elements(model.id)

        if len(models) > 1:
            entry['process'] = render(process_ids, f'{stem}--process-{slugify(model.id)}')

        if lane_images:
            for lane_id in document.lanes:
                if lane_id not in process_ids:
                    continue
                lane_name = document.get_lane_name(lane_id)
                entry['lanes'].append((
                    normalize_lane_name(lane_name),
                    render(document.lane_elements(lane_id), f'{stem}--lane-{slugify(lane_name)}'),
                ))

        if scenario_images:
            # Diagramme complet où seules les tâches du scénario sont en évidence.
            for sc in build_scenarios(model):
                entry['scenarios'][sc.id] = render(
                    None,
                    f'{stem}--{slugify(model.id)}--{sc.id.lower()}',
                    highlight={t.id for t in sc.tasks},
                )

        result[model.id] = entry

    return result


def render_process_markdown(
    model: ProcessModel,
    source_file: Path,
    images: dict | None = None,
) -> str:
    tasks = [n for n in model.nodes.values() if n.tag in TASK_TAGS and not n.is_container]
    gateways = [n for n in model.nodes.values() if n.tag in GATEWAY_TAGS]
    scenarios = build_scenarios(model)
    alternatives = build_alternative_scenarios(model)

    lines = [
        '---',
        'title: "' + model.name.replace('"', '\\"') + '"',
        'type: exigences-bpmn',
        f'bpmn_process_id: {model.id}',
        f'source_bpmn: {source_file.name}',
        'tags:',
        '  - bpmn',
        '  - user-story',
        '  - regle-gestion',
        '---',
        '',
        f'# {model.name}',
        '',
        '## Métadonnées',
        '',
        f'- **Processus BPMN** : `{model.id}`',
        f'- **Fichier source** : `{source_file.name}`',
        f'- **Tâches détectées** : {len(tasks)}',
        f'- **Gateways détectés** : {len(gateways)}',
        f'- **Scénarios** : {len(scenarios)}',
        f'- **Scénarios alternatifs (événements limites)** : {len(alternatives)}',
        '',
        '## Scénarios métier',
        '',
    ]

    if images:
        def alt(text: str) -> str:
            return text.replace('[', '(').replace(']', ')')

        diagram = ['## Diagramme', '', f'![Diagramme BPMN — {alt(model.name)}]({images["full"]})', '']
        if images.get('process'):
            diagram += ['### Processus seul', '',
                        f'![Processus — {alt(model.name)}]({images["process"]})', '']
        if images.get('lanes'):
            diagram += ['### Diagrammes par lane', '']
            for lane_name, rel_path in images['lanes']:
                diagram += [f'#### {lane_name}', '', f'![Lane — {alt(lane_name)}]({rel_path})', '']
        index = lines.index('## Scénarios métier')
        lines[index:index] = diagram

    first_scenario_of: dict[str, str] = {}
    for sc in scenarios:
        for t in sc.tasks:
            first_scenario_of.setdefault(t.id, sc.id)

    if scenarios:
        for sc in scenarios:
            lines.append(f'### {sc.id} — {sc.title} ({len(sc.tasks)} tâche(s))')
            lines.append('')
            if sc.start_events:
                lines.append(
                    '- **Déclenché par** : '
                    + ', '.join(quote(node_label(e)) for e in sc.start_events)
                )
            if sc.end_events:
                lines.append(
                    '- **Résultat(s)** : '
                    + ', '.join(quote(node_label(e)) for e in sc.end_events)
                )
            lines.append(f'- **Rôle** : {sc.role}')
            lines.append('- **Tâches** :')
            for t in sc.tasks:
                lines.append(f'    - `{t.id}` {markdown_escape(node_label(t))}')
            if sc.handoff_to:
                lines.append(f'- **Relais vers** : {sc.handoff_to}')
            if sc.loops_to:
                lines.append(
                    '- **Reprise (retour vers)** : '
                    + ', '.join(
                        f'{quote(node_label(t))} ({first_scenario_of.get(t.id, "-")})'
                        for t in sc.loops_to
                    )
                )
            scenario_image = (images or {}).get('scenarios', {}).get(sc.id)
            if scenario_image:
                lines.extend(['', f'![Scénario {sc.id} — tâches mises en évidence]({scenario_image})'])
            lines.append('')
    else:
        lines.append('_Aucun scénario détecté._')
        lines.append('')

    lines.extend(['## Scénarios alternatifs (événements limites)', ''])
    if alternatives:
        for alt in alternatives:
            trigger = alternative_trigger_label(alt.boundary_event)
            task_label = node_label(alt.attached_task) if alt.attached_task else 'une activité non identifiée'
            interrupt = 'interrompt' if alt.boundary_event.cancel_activity else "n'interrompt pas"
            targets_label = (
                ', '.join(quote(node_label(t)) for t in alt.targets)
                or 'une suite non déterminée'
            )
            lines.extend([
                f'### {alt.id} — {node_label(alt.boundary_event)}',
                '',
                f'**Étant donné** que « {task_label} » est en cours,',
                '',
                f'**quand** {trigger} survient,',
                '',
                f'**alors** le processus {interrupt} l\'activité en cours et '
                f'bascule vers {targets_label}.',
                '',
                f'- Rôle concerné : {alt.role}',
                f'- Élément BPMN : `{alt.boundary_event.id}`',
                '',
            ])
    else:
        lines.append('_Aucun événement limite (boundary event) détecté._')
        lines.append('')

    lines.extend(['## User stories', ''])
    if not tasks:
        lines.append("_Aucune tâche BPMN exploitable n'a été détectée._")
        lines.append('')

    for task in tasks:
        role = task.lane or 'utilisateur métier'
        title = node_label(task)
        goal = story_goal(model, task)
        criteria = criteria_for_task(model, task)
        role_note = ' (rôle déduit d\'une assignation technique)' if task.lane_is_inferred else ''
        lines.extend([
            f'### US-{task.id} — {title}',
            '',
            f'**En tant que** {role}{role_note},',
            '',
            f'**je veux** {title.lower()},',
            '',
            f'**afin de** {goal}.',
            '',
        ])
        if task.data_inputs or task.data_outputs or task.applications:
            lines.append('#### Données manipulées')
            lines.append('')
            if task.data_inputs:
                lines.append(
                    '- **Données d\'entrée** : ' + ', '.join(task.data_inputs)
                )
            if task.data_outputs:
                lines.append(
                    '- **Données de sortie** : ' + ', '.join(task.data_outputs)
                )
            if task.applications:
                lines.append(
                    '- **Application(s)** : ' + ', '.join(task.applications)
                )
            lines.append('')
        if task.tag == "businessRuleTask":
            lines.append('#### Règle de gestion appliquée')
            lines.append('')
            if task.business_rule_refs:
                lines.extend(f'- {r}' for r in task.business_rule_refs)
            else:
                lines.append('_À compléter._')
            lines.append('')
        lines.extend([
            "#### Critères d'acceptation",
            '',
        ])
        for c in criteria:
            lines.append(f'- {c}')
        lines.extend([
            '',
            '#### Traçabilité',
            '',
            f'- Élément BPMN : `{task.id}`',
            f'- Type BPMN : `{task.tag}`',
            f'- Lane / rôle : {role}',
            '',
        ])

    lines.extend(['## Règles de gestion candidates', ''])
    all_rules: list[str] = []
    for gw in gateways:
        all_rules.extend(rules_for_gateway(model, gw))
    if all_rules:
        lines.extend(f'- {r}' for r in all_rules)
    else:
        lines.append(
            "_Aucune règle explicite n'a été extraite : ajoutez des conditions "
            "sur les sequence flows ou des noms explicites sur les gateways._"
        )

    business_rule_tasks = [t for t in tasks if t.tag == "businessRuleTask"]
    if business_rule_tasks:
        lines.extend([
            '',
            '## Règles de gestion appliquées (Business Rule Task)',
            '',
        ])
        for br_task in business_rule_tasks:
            lines.append(f'### {br_task.id} — {markdown_escape(node_label(br_task))}')
            lines.append('')
            details = list(br_task.business_rule_refs)
            if br_task.data_inputs:
                details.append("Données d'entrée : " + ', '.join(br_task.data_inputs))
            if br_task.data_outputs:
                details.append("Données de sortie : " + ', '.join(br_task.data_outputs))
            if br_task.applications:
                details.append("Application(s) : " + ', '.join(br_task.applications))
            if details:
                lines.extend(f'- {d}' for d in details)
            else:
                lines.append('_Règle de gestion : à compléter._')
            lines.append('')

    lines.extend([
        '',
        '## Matrice de traçabilité',
        '',
        '| ID BPMN | Type | Nom | Lane / rôle | User story | Scénario |',
        '|---|---|---|---|---|---|',
    ])
    task_to_sc: dict[str, str] = {}
    for sc in scenarios:
        for t in sc.tasks:
            task_to_sc.setdefault(t.id, sc.id)
    for task in tasks:
        role = task.lane or 'Utilisateur métier'
        sc_id = task_to_sc.get(task.id, '-')
        lines.append(
            f'| `{task.id}` | {task.tag} | {markdown_escape(node_label(task))} | '
            f'{markdown_escape(role)} | `US-{task.id}` | {sc_id} |'
        )
    lines.append('')

    lines.extend(['## Vue atomique (par activité)', ''])
    for task in tasks:
        lines.append(f'### {task.id} — {markdown_escape(node_label(task))}')
        extra = ['multi-instance'] if task.is_multi_instance else []
        extra_label = f' ({", ".join(extra)})' if extra else ''
        lines.append(
            f'- Type : `{task.tag}`{extra_label} | Rôle : {markdown_escape(task.lane or "-")}'
        )
        if task.data_inputs:
            lines.append('- Données d\'entrée : ' + ', '.join(task.data_inputs))
        if task.data_outputs:
            lines.append('- Données de sortie : ' + ', '.join(task.data_outputs))
        if task.applications:
            lines.append('- Application(s) : ' + ', '.join(task.applications))
        lines.append('')

    return '\n'.join(lines)


def find_bpmn_files(input_path: Path) -> Iterable[Path]:
    if input_path.is_file():
        if input_path.suffix.lower() != '.bpmn':
            raise ValueError("Le fichier d'entrée doit avoir l'extension .bpmn")
        yield input_path
        return
    if input_path.is_dir():
        yield from sorted(input_path.rglob('*.bpmn'))
        return
    raise FileNotFoundError(f'Chemin introuvable : {input_path}')


def convert(
    input_path: Path,
    output_dir: Path,
    images: bool = False,
    image_scale: float = 1.0,
    lane_images: bool = False,
    scenario_images: bool = False,
) -> int:
    output_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    used_names: set[str] = set()

    for bpmn_file in find_bpmn_files(input_path):
        try:
            models = parse_bpmn(bpmn_file)
        except ET.ParseError as exc:
            print(f'ERREUR XML : {bpmn_file} — {exc}', file=sys.stderr)
            continue
        if not models:
            print(f'AVERTISSEMENT : aucun processus BPMN trouvé dans {bpmn_file}')
            continue
        image_set: dict[str, dict] = {}
        if images:
            image_set = generate_images(
                bpmn_file, models, output_dir / 'images', image_scale, lane_images,
                scenario_images,
            )

        for model in models:
            base_name = f'{slugify(bpmn_file.stem)}--{slugify(model.name)}'
            output_name = f'{base_name}.md'
            suffix = 2
            while output_name in used_names:
                # Évite d'écraser un fichier si deux process partagent le
                # même nom une fois "slugifié" (même fichier ou fichiers
                # différents).
                output_name = f'{base_name}-{suffix}.md'
                suffix += 1
            used_names.add(output_name)

            output_file = output_dir / output_name
            output_file.write_text(render_process_markdown(model, bpmn_file, image_set.get(model.id)), encoding='utf-8')
            print(f'Généré : {output_file}')
            written += 1

    return written


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Convertit des BPMN 2.0 en user stories et règles Markdown.'
    )
    parser.add_argument('input', type=Path, help='Fichier .bpmn ou répertoire contenant des BPMN')
    parser.add_argument(
        '-o', '--output', type=Path, default=Path('./output-bpmn'),
        help='Répertoire Markdown cible (défaut : ./output-bpmn)',
    )
    parser.add_argument('--no-images', action='store_true',
                        help="Ne génère pas les images PNG du diagramme")
    parser.add_argument('--no-scenario-images', action='store_true',
                        help='Pas d\'image avec les tâches du scénario en évidence')
    parser.add_argument('--lane-images', action='store_true',
                        help='Ajoute une image par lane')
    parser.add_argument('--image-scale', type=float, default=1.0,
                        help="Échelle des images (défaut : 1.0)")
    args = parser.parse_args()
    count = convert(args.input, args.output, images=not args.no_images,
                    image_scale=args.image_scale, lane_images=args.lane_images,
                    scenario_images=not args.no_scenario_images)
    if count == 0:
        print('Aucun document généré.', file=sys.stderr)
        sys.exit(1)
    print(f'{count} document(s) Markdown généré(s).')


if __name__ == '__main__':
    main()