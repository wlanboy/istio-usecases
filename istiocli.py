"""Gemeinsame Hilfsfunktionen fuer istio-healthcheck.py und istio-inspect.py.

Nur Python-Standardbibliothek (ab 3.9). Externe Befehle (oc, kubectl, istioctl) laufen
mit Timeout und UTF-8-Dekodierung, damit das unter Windows mit oc.exe genauso funktioniert.
"""

import json
import shutil
import subprocess
from typing import Optional

# (items, fehlermeldung) - bei Fehler ist items leer
Result = tuple[list, Optional[str]]

XDS_TYPES = (("CDS", "cluster"), ("LDS", "listener"), ("EDS", "endpoint"), ("RDS", "route"))
TIMEOUT = 120


def run(cmd: list[str]) -> tuple[int, str, str]:
    """Fuehrt cmd aus und liefert (returncode, stdout, stderr)."""
    try:
        # oc/kubectl liefern UTF-8; ohne explizites encoding nimmt Windows cp1252
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=TIMEOUT)
    except FileNotFoundError:
        return 127, "", f"Befehl nicht gefunden: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, "", f"Timeout nach {TIMEOUT}s: {' '.join(cmd[1:4])} ..."
    return proc.returncode, proc.stdout, proc.stderr


def with_context(binary: str, context: Optional[str]) -> list[str]:
    return [binary, "--context", context] if context else [binary]


def detect_cli(preferred: Optional[str]) -> str:
    """oc bevorzugen, sonst kubectl; beide verstehen dieselben get/exec-Aufrufe.
    which() liefert unter Windows den Pfad zu oc.exe."""
    for binary in [preferred] if preferred else ["oc", "kubectl"]:
        path = shutil.which(binary)
        if path:
            return path
    return preferred or "oc"


def get_json(kubectl: list[str], *args: str) -> Result:
    """`get ... -o json`; Listen liefern ihre items, Einzelobjekte eine Liste mit einem Element."""
    rc, out, err = run(kubectl + ["get", *args, "-o", "json"])
    if rc != 0:
        return [], (err or out).strip()
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return [], "ungueltiges JSON erhalten"
    return data["items"] if "items" in data else [data], None


def fetch_syncz(kubectl: list[str], istio_namespace: str) -> tuple[list, list[str]]:
    """Fragt jeden laufenden istiod-Pod ab, denn jeder kennt nur die bei ihm verbundenen Proxys.
    Der Service-Port 15014 verlangt in neueren Istio-Versionen Auth, daher per exec.
    Liefert (eintraege, fehler je istiod-Pod); Eintraege tragen den Pod-Namen unter 'istiod'."""
    pods, err = get_json(kubectl, "pods", "-n", istio_namespace, "-l", "app=istiod")
    if err:
        return [], [err]
    names = [p["metadata"]["name"] for p in pods if p.get("status", {}).get("phase") == "Running"]
    if not names:
        return [], [f"kein laufender istiod-Pod (app=istiod) in Namespace {istio_namespace}"]
    entries, errors = [], []
    for name in names:
        rc, out, err = run(kubectl + ["exec", "-n", istio_namespace, name, "--",
                                      "pilot-discovery", "request", "GET", "/debug/syncz"])
        try:
            if rc != 0:
                raise ValueError((err or out).strip())
            entries += [dict(e, istiod=name) for e in json.loads(out) or []]
        except ValueError as e:  # JSONDecodeError ist eine Unterklasse
            errors.append(f"{name}: {e}")
    return entries, errors


def xds_status(entry: dict, prefix: str) -> str:
    """Gleiche Logik wie istioctl proxy-status: gesendet == bestaetigt -> SYNCED."""
    sent = entry.get(f"{prefix}_sent", "")
    if not sent:
        return "NOT SENT"
    return "SYNCED" if sent == entry.get(f"{prefix}_acked", "") else "STALE"


def print_header(title: str) -> None:
    print()
    print(f"--- {title} " + "-" * max(1, 50 - len(title)))
