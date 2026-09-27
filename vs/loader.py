"""Laedt die Kubernetes-Objekte eines Namespace aus dem Cluster (kubectl) oder
aus lokalen YAML-Manifesten (--file, benoetigt PyYAML)."""

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from typing import NoReturn

from model import KIND_SHORT

# Workloads, deren Pod-Template bei --file als Pod zaehlt (fuer DR-SUBSET-NOPODS).
WORKLOAD_KINDS = ("Pod", "Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "Job", "CronJob")
ISTIO_RESOURCES = ",".join(f"{r}.networking.istio.io" for r in
                           ("virtualservices", "destinationrules", "gateways", "serviceentries"))


def die(message) -> NoReturn:
    """Beendet mit Exit-Code 2, damit Ladefehler nicht wie ERROR-Befunde (1) aussehen."""
    print(message, file=sys.stderr)
    sys.exit(2)


def hint(message):
    print(f"Hinweis: {message}", file=sys.stderr)


@dataclass
class Source:
    """Die geladenen Objekte und welche Arten vollstaendig bekannt sind. Pruefungen,
    die ein fehlendes Objekt melden, laufen nur fuer tatsaechlich gelesene Arten."""
    items: list
    services_known: bool = False      # Services und ServiceEntries des Namespace
    pods_known: bool = False
    gateway_namespaces: set = field(default_factory=set)


def run(cmd):
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    except FileNotFoundError:
        return None, f"Befehl nicht gefunden: {cmd[0]}"
    except subprocess.CalledProcessError as e:
        return None, (e.stderr or e.stdout or "").strip()
    return result.stdout, None


def kubectl(context, *args):
    return run(["kubectl"] + (["--context", context] if context else []) + list(args))


def kubectl_items(context, resources, namespace):
    out, err = kubectl(context, "get", resources, "-n", namespace, "-o", "json")
    if out is None:
        return None, err
    try:
        return json.loads(out).get("items", []), None
    except json.JSONDecodeError as e:
        return None, f"ungueltige JSON-Ausgabe von kubectl: {e}"


def is_vs_or_dr(item):
    return item.get("kind") in ("VirtualService", "DestinationRule")


def gateway_namespaces_of(items, namespace):
    """Die Namespaces aller Gateways, auf die VirtualServices verweisen."""
    for item in items:
        if item.get("kind") == "VirtualService":
            for gw in item.get("spec", {}).get("gateways") or []:
                if gw != "mesh":
                    yield gw.split("/", 1)[0] if "/" in gw else namespace


def load_from_cluster(namespace, context):
    items, err = kubectl_items(context, ISTIO_RESOURCES, namespace)
    if items is None:
        die(f"Fehler beim Lesen aus dem Cluster: {err}")
    if not any(is_vs_or_dr(i) for i in items):
        # kubectl meldet fuer einen unbekannten Namespace keinen Fehler, sondern eine
        # leere Liste. Ohne diese Pruefung waere das hier ein gruener CI-Lauf.
        _, err = kubectl(context, "get", "namespace", namespace, "-o", "name")
        if err and "notfound" in err.replace(" ", "").lower():
            die(f"Namespace {namespace} existiert nicht")
    source = Source(items, gateway_namespaces={namespace})

    # Services und Pods sind optional: fehlen die Rechte, entfallen nur die Pruefungen darauf.
    services, err = kubectl_items(context, "services", namespace)
    if services is None:
        hint(f"Services nicht lesbar: {err}")
    else:
        items += services
        source.services_known = True
    pods, err = kubectl_items(context, "pods", namespace)
    if pods is None:
        hint(f"Pods nicht lesbar: {err}")
    else:
        # Beendete Pods (z.B. von Jobs) liefern keine Endpunkte mehr.
        items += [p for p in pods
                  if p.get("status", {}).get("phase") not in ("Succeeded", "Failed")]
        source.pods_known = True

    # Gateways aus anderen Namespaces (z.B. istio-system/ingress), auf die verwiesen wird.
    for ns in sorted(set(gateway_namespaces_of(items, namespace)) - {namespace}):
        gateways, err = kubectl_items(context, "gateways.networking.istio.io", ns)
        if gateways is None:
            hint(f"Gateways in Namespace {ns} nicht lesbar: {err}")
        else:
            items += gateways
            source.gateway_namespaces.add(ns)
    return source


def yaml_files(paths):
    files = []
    for path in paths:
        if os.path.isdir(path):
            files += sorted(os.path.join(path, f) for f in os.listdir(path)
                            if f.endswith((".yaml", ".yml")))
        elif os.path.isfile(path):
            files.append(path)
        else:
            die(f"Datei oder Verzeichnis nicht gefunden: {path}")
    return files


def k8s_objects(doc):
    """Die Kubernetes-Objekte eines YAML-Dokuments; `kind: List` (z.B. aus
    `kubectl get -o yaml`) wird aufgeloest, andere Dokumente werden ignoriert."""
    if not isinstance(doc, dict):
        return
    if doc.get("kind") == "List":
        for item in doc.get("items") or []:
            yield from k8s_objects(item)
    elif isinstance(doc.get("kind"), str):
        yield doc


def pod_template_labels(obj):
    spec = obj.get("spec") or {}
    if obj["kind"] == "Pod":
        return obj["metadata"].get("labels") or {}
    if obj["kind"] == "CronJob":
        spec = (spec.get("jobTemplate") or {}).get("spec") or {}
    return ((spec.get("template") or {}).get("metadata") or {}).get("labels") or {}


def add_file_object(source, obj, namespace, path):
    kind = obj["kind"]
    # Nur core/v1-Services; z.B. Knative-Services haben ebenfalls kind: Service.
    if kind == "Service" and obj.get("apiVersion") != "v1":
        return
    if kind not in KIND_SHORT and kind not in WORKLOAD_KINDS:
        return
    meta = obj.get("metadata")
    if not isinstance(meta, dict) or not meta.get("name"):
        die(f"{path}: {kind} ohne metadata.name")
    # Manifeste ohne Namespace landen wie bei `kubectl apply -n` im Ziel-Namespace.
    meta.setdefault("namespace", namespace)
    if kind == "Gateway":
        # Gateways werden auch aus anderen Namespaces gebraucht (Verweis <ns>/<gateway>).
        source.items.append(obj)
        source.gateway_namespaces.add(meta["namespace"])
    elif meta["namespace"] != namespace:
        return
    elif kind in WORKLOAD_KINDS:
        source.items.append({"kind": "Pod", "metadata": {
            "name": meta["name"], "namespace": namespace, "labels": pod_template_labels(obj)}})
        source.pods_known = True
    else:
        source.services_known |= kind in ("Service", "ServiceEntry")
        source.items.append(obj)


def load_from_files(paths, namespace):
    try:
        import yaml
    except ImportError:
        die("--file benoetigt PyYAML (pip install pyyaml)")
    source = Source([])
    for path in yaml_files(paths):
        try:
            with open(path) as fh:
                # Eine Datei kann mehrere Dokumente (---) enthalten.
                docs = list(yaml.safe_load_all(fh))
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as e:
            die(f"Fehler beim Lesen von {path}: {e}")
        for doc in docs:
            for obj in k8s_objects(doc):
                add_file_object(source, obj, namespace, path)
    # Wie im Cluster: gibt es Services, aber keine Workloads dazu, hat der Service
    # keine Pods. Ohne Services entfaellt DR-SUBSET-NOPODS ohnehin.
    source.pods_known |= source.services_known
    if not any(is_vs_or_dr(i) for i in source.items):
        die(f"Keine VirtualServices oder DestinationRules fuer Namespace {namespace} "
            f"in {', '.join(paths)} gefunden")
    return source
