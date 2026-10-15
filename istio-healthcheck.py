#!/usr/bin/env python3
"""Healthcheck fuer eine Istio-Installation: istiod, CRDs, Webhooks, Gateways,
istio-cni/ztunnel, Zertifikatslaufzeiten, Proxy-Sync und Sidecar-Injection.
Nutzt nur die Python-Standardbibliothek (ab 3.9) und `oc` bzw. `kubectl`; istioctl
wird nicht benoetigt, das Skript laeuft daher auch unter Windows mit oc.exe.
Der Proxy-Sync-Status wird per `exec` in istiod ueber
`pilot-discovery request GET /debug/syncz` gelesen (RBAC-Recht `pods/exec` im
Istio-Namespace). Aus Secrets werden nur Zertifikats-Keys gelesen, keine
privaten Schluessel.

Exit-Code: 0 = alles OK, 1 = Warnungen, 2 = Fehler.

Verwendung:
    python istio-healthcheck.py [--context CONTEXT] [--cli oc|kubectl]
                                [--istio-namespace istio-system] [-n NAMESPACE ...]
                                [--cert-warn-days 30] [--restart-warn 5]
                                [--no-sync] [--problems-only]
"""

import argparse
import base64
import io
import json
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Optional

OK, INFO, WARN, FAIL = "OK", "INFO", "WARN", "FAIL"
SEVERITY = {OK: 0, INFO: 0, WARN: 1, FAIL: 2}

# (items, fehlermeldung) - bei Fehler ist items leer
Result = tuple[list, Optional[str]]
# (status, text)
Finding = tuple[str, str]

# CRDs, ohne die istiod bzw. die gaengigen Usecases nicht funktionieren
REQUIRED_CRDS = (
    "virtualservices.networking.istio.io",
    "destinationrules.networking.istio.io",
    "gateways.networking.istio.io",
    "serviceentries.networking.istio.io",
    "sidecars.networking.istio.io",
    "envoyfilters.networking.istio.io",
    "peerauthentications.security.istio.io",
    "requestauthentications.security.istio.io",
    "authorizationpolicies.security.istio.io",
)
# Existiert nur auf OpenShift
OPENSHIFT_CRD = "clusterversions.config.openshift.io"

# Keys in Gateway-Credential-Secrets: Serverzertifikat bzw. CA fuer mTLS
SERVER_CERT_KEYS = ("tls.crt", "cert")
CA_CERT_KEYS = ("ca.crt", "cacert")

XDS_TYPES = (("CDS", "cluster"), ("LDS", "listener"), ("EDS", "endpoint"), ("RDS", "route"))
MAX_LISTED = 5
TIMEOUT = 120


def run(cmd: list[str]) -> tuple[int, str, str]:
    """Fuehrt cmd aus und liefert (returncode, stdout, stderr)."""
    try:
        # oc liefert UTF-8; ohne explizites encoding nimmt Windows cp1252
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
    """oc bevorzugen, sonst kubectl. which() liefert unter Windows den Pfad zu oc.exe."""
    for binary in [preferred] if preferred else ["oc", "kubectl"]:
        path = shutil.which(binary)
        if path:
            return path
    return preferred or "oc"


def get_json(kubectl: list[str], *args: str) -> Result:
    rc, out, err = run(kubectl + ["get", *args, "-o", "json"])
    if rc != 0:
        return [], (err or out).strip()
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return [], "ungueltiges JSON erhalten"
    return data["items"] if "items" in data else [data], None


def get_scoped(kubectl: list[str], resource: str, namespaces: Optional[list[str]]) -> Result:
    """Ohne Namespace-Liste clusterweit (-A), sonst je Namespace einzeln und zusammengefuehrt."""
    if not namespaces:
        return get_json(kubectl, resource, "-A")
    results = [get_json(kubectl, resource, "-n", ns) for ns in namespaces]
    errors = [err for _, err in results if err]
    return [item for items, _ in results for item in items], "; ".join(errors) or None


def get_names(kubectl: list[str], resource: str) -> Result:
    """Nur Namen abfragen; CRDs als JSON waeren durch die Schemas sehr gross."""
    rc, out, err = run(kubectl + ["get", resource, "-o", "name"])
    if rc != 0:
        return [], (err or out).strip()
    return [line.split("/", 1)[-1] for line in out.split()], None


def get_pem_fields(kubectl: list[str], kind: str, namespace: str, name: str,
                   keys: tuple[str, ...]) -> tuple[dict, Optional[str]]:
    """Liest per jsonpath nur die angegebenen Keys als {key: pem}, damit private
    Schluessel im Secret gar nicht erst abgefragt werden. Fehlt das Objekt, ist das Ergebnis leer."""
    template = '{"|"}'.join("{.data.%s}" % key.replace(".", "\\.") for key in keys)
    rc, out, err = run(kubectl + ["get", kind, name, "-n", namespace, "--ignore-not-found",
                                  "-o", f"jsonpath={template}"])
    if rc != 0:
        return {}, (err or out).strip()
    fields = {}
    for key, value in zip(keys, out.split("|")):
        if value.strip():
            fields[key] = base64.b64decode(value).decode("utf-8", "replace") if kind == "secret" else value
    return fields, None


def fetch_syncz(kubectl: list[str], istio_namespace: str) -> tuple[list, list[str]]:
    """Fragt jeden laufenden istiod-Pod ab - jeder kennt nur die bei ihm verbundenen Proxys.
    Liefert (eintraege, fehler je istiod-Pod)."""
    pods, err = get_json(kubectl, "pods", "-n", istio_namespace, "-l", "app=istiod")
    if err:
        return [], [err]
    names = [meta(p)["name"] for p in pods if p.get("status", {}).get("phase") == "Running"]
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


# --- Hilfsfunktionen fuer Kubernetes-Objekte ---------------------------

def meta(obj: dict) -> dict:
    return obj.get("metadata", {})


def ref(obj: dict) -> str:
    return f"{meta(obj).get('namespace')}/{meta(obj).get('name')}"


def template_meta(workload: dict) -> dict:
    return workload.get("spec", {}).get("template", {}).get("metadata", {})


def image_tag(image: str) -> str:
    tail = image.split("@")[0].rsplit("/", 1)[-1]
    return tail.split(":", 1)[1] if ":" in tail else "?"


def minor_version(version: str) -> str:
    return ".".join(version.split(".")[:2])


def all_containers(pod: dict) -> list:
    # Native Sidecars (Istio >= 1.19) laufen als initContainer mit restartPolicy=Always
    spec = pod.get("spec", {})
    return spec.get("containers", []) + spec.get("initContainers", [])


def all_statuses(pod: dict) -> list:
    status = pod.get("status", {})
    return status.get("containerStatuses", []) + status.get("initContainerStatuses", [])


def has_sidecar(pod: dict) -> bool:
    return any(c.get("name") == "istio-proxy" for c in all_containers(pod))


def is_running(pod: dict) -> bool:
    return pod.get("status", {}).get("phase") == "Running" and not meta(pod).get("deletionTimestamp")


def injection_disabled(pod: dict) -> bool:
    m = meta(pod)
    return "false" in ((m.get("labels") or {}).get("sidecar.istio.io/inject"),
                       (m.get("annotations") or {}).get("sidecar.istio.io/inject"))


def selector_matches(selector: dict, labels: dict) -> bool:
    return bool(selector) and all(labels.get(k) == v for k, v in selector.items())


def replica_finding(kind: str, workload: dict, ready: int, desired: int, extra: str = "") -> Finding:
    text = f"{kind} {ref(workload)}{extra}: {ready}/{desired} bereit"
    if desired == 0:
        return WARN, text + " (auf 0 skaliert)"
    if ready == 0:
        return FAIL, text
    return (OK if ready >= desired else WARN), text


def short_list(names: list[str]) -> str:
    more = f" (+{len(names) - MAX_LISTED} weitere)" if len(names) > MAX_LISTED else ""
    return ", ".join(names[:MAX_LISTED]) + more


def not_checkable(what: str, err: Optional[str]) -> Finding:
    return WARN, f"{what} nicht pruefbar: {err}"


# --- Zertifikate ohne externe Bibliothek ------------------------------

def der_read(data: bytes, pos: int) -> tuple[int, int, int]:
    """Liest ein DER-Element ab pos und liefert (tag, inhalt_start, inhalt_ende)."""
    tag, length = data[pos], data[pos + 1]
    pos += 2
    if length & 0x80:
        size = length & 0x7F
        length = int.from_bytes(data[pos:pos + size], "big")
        pos += size
    return tag, pos, pos + length


def cert_not_after(der: bytes) -> datetime:
    """notAfter aus einem X.509-Zertifikat (RFC 5280, Abschnitt 4.1)."""
    _, pos, _ = der_read(der, 0)            # Certificate
    _, pos, _ = der_read(der, pos)          # tbsCertificate
    tag, _, end = der_read(der, pos)
    if tag == 0xA0:                         # optionale Version [0]
        pos = end
    for _ in range(3):                      # serialNumber, signature, issuer
        _, _, pos = der_read(der, pos)
    _, pos, _ = der_read(der, pos)          # validity
    _, _, pos = der_read(der, pos)          # notBefore
    tag, start, end = der_read(der, pos)    # notAfter
    fmt = "%y%m%d%H%M%SZ" if tag == 0x17 else "%Y%m%d%H%M%SZ"  # UTCTime / GeneralizedTime
    return datetime.strptime(der[start:end].decode("ascii"), fmt).replace(tzinfo=timezone.utc)


def pem_certs(pem: str) -> list[bytes]:
    blocks = pem.split("-----BEGIN CERTIFICATE-----")[1:]
    return [base64.b64decode(b.split("-----END CERTIFICATE-----")[0]) for b in blocks]


def cert_finding(label: str, pem: str, warn_days: int) -> Finding:
    """Bewertet das am fruehesten ablaufende Zertifikat im PEM (Kette oder mehrere Roots)."""
    try:
        expiries = [cert_not_after(der) for der in pem_certs(pem)]
    except (ValueError, IndexError) as e:
        return WARN, f"{label}: Zertifikat nicht lesbar ({e})"
    if not expiries:
        return WARN, f"{label}: kein PEM-Zertifikat gefunden"
    not_after = min(expiries)
    days = (not_after - datetime.now(timezone.utc)).days
    if days < 0:
        return FAIL, f"{label}: abgelaufen seit {not_after:%Y-%m-%d}"
    return (WARN if days < warn_days else OK), f"{label}: gueltig bis {not_after:%Y-%m-%d} (noch {days} Tage)"


# --- Checks ------------------------------------------------------------

def istiod_deployments(deployments: list, istio_ns: str) -> list:
    return [d for d in deployments if meta(d).get("namespace") == istio_ns
            and (template_meta(d).get("labels") or {}).get("app") == "istiod"]


def istiod_revisions(deployments: list, istio_ns: str) -> set[str]:
    return {(template_meta(d).get("labels") or {}).get("istio.io/rev", "default")
            for d in istiod_deployments(deployments, istio_ns)}


def revision_tags(mutating: list) -> set[str]:
    tags = ((meta(m).get("labels") or {}).get("istio.io/tag") for m in mutating)
    return {t for t in tags if t}


def check_control_plane(deployments: Result, pods: Result, istio_ns: str, restart_warn: int) -> list[Finding]:
    if deployments[1]:
        return [not_checkable("istiod", deployments[1])]
    istiods = istiod_deployments(deployments[0], istio_ns)
    if not istiods:
        return [(FAIL, f"kein istiod-Deployment (app=istiod) in Namespace {istio_ns}")]
    findings = []
    for d in istiods:
        rev = (template_meta(d).get("labels") or {}).get("istio.io/rev", "default")
        containers = d["spec"]["template"]["spec"].get("containers", [])
        version = next((image_tag(c.get("image", "")) for c in containers if c.get("name") == "discovery"), "?")
        desired = d.get("spec", {}).get("replicas", 1)
        ready = d.get("status", {}).get("readyReplicas", 0)
        findings.append(replica_finding("istiod", d, ready, desired, f" (Revision {rev}, Version {version})"))
        if desired == 1:
            findings.append((INFO, f"istiod {ref(d)} laeuft mit 1 Replica (kein HA)"))
    for pod in pods[0]:
        if meta(pod).get("namespace") == istio_ns and (meta(pod).get("labels") or {}).get("app") == "istiod":
            restarts = sum(s.get("restartCount", 0) for s in all_statuses(pod))
            if restarts >= restart_warn:
                findings.append((WARN, f"istiod-Pod {meta(pod)['name']}: {restarts} Restarts"))
    return findings


def check_crds(crds: Result, is_openshift: bool) -> list[Finding]:
    names, err = crds
    if err:
        return [not_checkable("CRDs", err)]
    istio_crds = [n for n in names if n.endswith(".istio.io")]
    missing = [c for c in REQUIRED_CRDS if c not in names]
    findings = [(FAIL, f"fehlende CRDs: {', '.join(missing)}")] if missing else \
        [(OK, f"{len(istio_crds)} Istio-CRDs vorhanden, alle {len(REQUIRED_CRDS)} Kern-CRDs da")]
    if is_openshift:
        findings.append((INFO, "OpenShift erkannt"))
    return findings


def check_webhooks(mutating: Result, validating: Result, services: Result, istio_ns: str) -> list[Finding]:
    if mutating[1] or validating[1]:
        return [not_checkable("Webhooks", mutating[1] or validating[1])]
    known_services = {ref(s) for s in services[0]}
    injectors = [m for m in mutating[0]
                 if meta(m)["name"].startswith(("istio-sidecar-injector", "istio-revision-tag"))]
    validators = [v for v in validating[0] if "istio" in meta(v)["name"]]
    findings = []
    if not injectors:
        findings.append((FAIL, "kein Sidecar-Injector-Webhook (istio-sidecar-injector*): "
                               "Sidecars werden nicht injiziert"))
    if not validators:
        findings.append((WARN, "kein Istio-Validierungs-Webhook: fehlerhafte Istio-Ressourcen werden nicht abgewiesen"))
    for cfg in injectors + validators:
        problems = []
        for hook in cfg.get("webhooks", []):
            client = hook.get("clientConfig", {})
            if not client.get("caBundle"):
                problems.append(f"{hook.get('name')}: caBundle leer (von istiod noch nicht gepatcht)")
            svc = client.get("service")
            if svc and svc.get("namespace") == istio_ns and f"{istio_ns}/{svc.get('name')}" not in known_services:
                problems.append(f"{hook.get('name')}: Service {istio_ns}/{svc.get('name')} existiert nicht")
        if problems:
            findings += [(FAIL, f"{meta(cfg)['name']} -> {p}") for p in problems]
        else:
            findings.append((OK, f"{meta(cfg)['name']}: {len(cfg.get('webhooks', []))} Webhooks, caBundle gesetzt"))
    return findings


def is_gateway_deployment(deployment: dict) -> bool:
    """Helm-Gateway-Chart: Injection-Template 'gateway'; klassisch (istioctl): istio-proxy mit Arg 'router'."""
    if "gateway" in (template_meta(deployment).get("annotations") or {}).get("inject.istio.io/templates", ""):
        return True
    containers = deployment["spec"]["template"]["spec"].get("containers", [])
    return any(c.get("name") == "istio-proxy" and "router" in (c.get("args") or []) for c in containers)


def gateway_service_finding(svc: dict) -> Finding:
    spec = svc.get("spec", {})
    svc_type = spec.get("type", "ClusterIP")
    ports = ",".join(str(p.get("port")) for p in spec.get("ports", []))
    if svc_type != "LoadBalancer":
        return OK, f"Service {ref(svc)} ({svc_type}, Ports {ports})"
    ingress = svc.get("status", {}).get("loadBalancer", {}).get("ingress") or []
    if not ingress:
        return WARN, f"Service {ref(svc)} (LoadBalancer): keine externe Adresse zugewiesen (pending)"
    addresses = ",".join(i.get("ip") or i.get("hostname", "?") for i in ingress)
    return OK, f"Service {ref(svc)} (LoadBalancer {addresses}, Ports {ports})"


def check_gateways(deployments: Result, services: Result, gateways: Result, pods: Result) -> list[Finding]:
    if deployments[1]:
        return [not_checkable("Gateway-Deployments", deployments[1])]
    findings = []
    gw_deployments = [d for d in deployments[0] if is_gateway_deployment(d)]
    if not gw_deployments:
        findings.append((INFO, "keine Gateway-Deployments gefunden"))
    for d in gw_deployments:
        desired = d.get("spec", {}).get("replicas", 1)
        findings.append(replica_finding("Gateway", d, d.get("status", {}).get("readyReplicas", 0), desired))
        labels = template_meta(d).get("labels") or {}
        matching = [s for s in services[0] if meta(s).get("namespace") == meta(d).get("namespace")
                    and selector_matches(s.get("spec", {}).get("selector") or {}, labels)]
        if not matching and not services[1]:
            findings.append((WARN, f"Gateway {ref(d)}: kein Service waehlt die Gateway-Pods aus"))
        findings += [gateway_service_finding(s) for s in matching]

    if gateways[1]:
        findings.append(not_checkable("Istio-Gateway-Ressourcen", gateways[1]))
    for gw in gateways[0]:
        selector = gw.get("spec", {}).get("selector") or {}
        if not any(is_running(p) and selector_matches(selector, meta(p).get("labels") or {}) for p in pods[0]):
            findings.append((WARN, f"Gateway-Ressource {ref(gw)}: kein laufender Pod passt zum Selector {selector}"))
    return findings


def gateway_credentials(gateways: list, pods: list) -> list[tuple[str, str, str]]:
    """(gateway-ref, namespace, secret-name) je credentialName. Das Secret muss im Namespace
    der Gateway-Pods liegen, nicht in dem der Gateway-Ressource."""
    targets = set()
    for gw in gateways:
        selector = gw.get("spec", {}).get("selector") or {}
        gw_namespaces = {meta(p)["namespace"] for p in pods if selector_matches(selector, meta(p).get("labels") or {})}
        for server in gw.get("spec", {}).get("servers", []):
            name = (server.get("tls") or {}).get("credentialName")
            if not name:
                continue
            if "/" in name:
                ns, name = name.split("/", 1)
                targets.add((ref(gw), ns, name))
            else:
                targets.update((ref(gw), ns, name) for ns in gw_namespaces)
    return sorted(targets)


def check_node_agents(daemonsets: Result, namespaces: Result, is_openshift: bool) -> list[Finding]:
    if daemonsets[1]:
        return [not_checkable("DaemonSets", daemonsets[1])]

    def by_label(key: str, value: str) -> list:
        return [d for d in daemonsets[0] if (template_meta(d).get("labels") or {}).get(key) == value]

    findings = []
    cni, ztunnel = by_label("k8s-app", "istio-cni-node"), by_label("app", "ztunnel")
    for name, sets in (("istio-cni", cni), ("ztunnel", ztunnel)):
        for d in sets:
            status = d.get("status", {})
            findings.append(replica_finding(f"{name}-DaemonSet", d, status.get("numberReady", 0),
                                            status.get("desiredNumberScheduled", 0)))
    if not cni:
        if is_openshift:
            findings.append((WARN, "istio-cni nicht gefunden: auf OpenShift wird istio-cni benoetigt "
                                   "(Helm: --set platform=openshift)"))
        else:
            findings.append((INFO, "istio-cni nicht installiert (Sidecars nutzen istio-init mit NET_ADMIN)"))
    ambient = [meta(n)["name"] for n in namespaces[0]
               if (meta(n).get("labels") or {}).get("istio.io/dataplane-mode") == "ambient"]
    if ambient and not ztunnel:
        findings.append((FAIL, f"Ambient-Namespaces ({short_list(ambient)}) ohne ztunnel-DaemonSet"))
    return findings


def check_certificates(root_cert: tuple[dict, Optional[str]], ca_secrets: list[tuple[str, tuple[dict, Optional[str]]]],
                       credentials: list[tuple[str, str, str, tuple[dict, Optional[str]]]],
                       istio_ns: str, warn_days: int) -> list[Finding]:
    findings = []
    fields, err = root_cert
    if err:
        findings.append(not_checkable("Root-Zertifikat", err))
    elif "root-cert.pem" not in fields:
        findings.append((FAIL, f"ConfigMap {istio_ns}/istio-ca-root-cert fehlt (Mesh-Root-CA)"))
    else:
        findings.append(cert_finding("Mesh-Root-CA (istio-ca-root-cert)", fields["root-cert.pem"], warn_days))

    # cacerts = eingebundene CA, istio-ca-secret = selbstsignierte CA von istiod
    found_ca = False
    for name, (fields, err) in ca_secrets:
        if err:
            findings.append(not_checkable(f"Secret {istio_ns}/{name}", err))
        elif "ca-cert.pem" in fields:
            found_ca = True
            findings.append(cert_finding(f"Signing-CA ({name})", fields["ca-cert.pem"], warn_days))
    if not found_ca and not any(err for _, (_, err) in ca_secrets):
        findings.append((INFO, "weder cacerts noch istio-ca-secret gefunden (externe CA?)"))

    for gateway, ns, name, (fields, err) in credentials:
        label = f"Gateway {gateway} -> Secret {ns}/{name}"
        if err:
            findings.append(not_checkable(label, err))
            continue
        server = next((fields[k] for k in SERVER_CERT_KEYS if k in fields), None)
        if server is None:
            findings.append((FAIL, f"{label}: Secret fehlt oder enthaelt kein Zertifikat ({'/'.join(SERVER_CERT_KEYS)})"))
            continue
        findings.append(cert_finding(label, server, warn_days))
        ca = next((fields[k] for k in CA_CERT_KEYS if k in fields), None)
        if ca is not None:
            findings.append(cert_finding(f"{label} (Client-CA)", ca, warn_days))
    return findings


def xds_status(entry: dict, prefix: str) -> str:
    """Gleiche Logik wie istioctl proxy-status: gesendet == bestaetigt -> SYNCED."""
    sent = entry.get(f"{prefix}_sent", "")
    if not sent:
        return "NOT SENT"
    return "SYNCED" if sent == entry.get(f"{prefix}_acked", "") else "STALE"


def check_sync(syncz: tuple[list, list[str]], pods: Result, istio_ns: str) -> list[Finding]:
    entries, errors = syncz
    findings = [(WARN, f"Sync-Status nicht lesbar (benoetigt pods/exec in {istio_ns}): {e}") for e in errors]
    if not entries:
        return findings or [(INFO, "keine Proxys mit istiod verbunden")]

    # NOT SENT ist normal (z.B. RDS ohne HTTP-Routen), nur STALE deutet auf ein Problem
    stale = [f"{e.get('proxy')} ({','.join(t for t, p in XDS_TYPES if xds_status(e, p) == 'STALE')})"
             for e in entries if any(xds_status(e, p) == "STALE" for _, p in XDS_TYPES)]
    if stale:
        findings.append((WARN, f"{len(stale)} Proxys STALE: {short_list(stale)}"))
    else:
        findings.append((OK, f"{len(entries)} Proxys verbunden, keiner STALE"))

    istiod_versions = {meta(p)["name"]: image_tag(c.get("image", ""))
                       for p in pods[0] if meta(p).get("namespace") == istio_ns
                       for c in p.get("spec", {}).get("containers", []) if c.get("name") == "discovery"}
    skew: dict[tuple[str, str], list[str]] = {}
    for e in entries:
        proxy_version, cp_version = e.get("istio_version", ""), istiod_versions.get(e["istiod"], "")
        if proxy_version and cp_version and proxy_version != cp_version:
            skew.setdefault((proxy_version, cp_version), []).append(e.get("proxy", "?"))
    # Nur Patch-Abweichung ist unkritisch, andere Minor-Version heisst: Pods nach Upgrade neu starten
    for (proxy_version, cp_version), proxies in sorted(skew.items()):
        status = WARN if minor_version(proxy_version) != minor_version(cp_version) else INFO
        findings.append((status, f"{len(proxies)} Proxys mit Version {proxy_version}, istiod hat {cp_version}: "
                                 f"{short_list(proxies)}"))

    # Ohne Fehler kennen wir alle istiod-Pods und koennen fehlende Verbindungen erkennen
    if not errors and not pods[1]:
        connected = {e.get("proxy") for e in entries}
        missing = [f"{meta(p)['name']}.{meta(p)['namespace']}" for p in pods[0]
                   if is_running(p) and has_sidecar(p)
                   and f"{meta(p)['name']}.{meta(p)['namespace']}" not in connected]
        if missing:
            findings.append((WARN, f"{len(missing)} Pods mit Sidecar nicht mit istiod verbunden: {short_list(missing)}"))
    return findings


def mesh_namespaces(namespaces: list) -> dict[str, str]:
    """{namespace: revision} fuer alle Namespaces mit Sidecar-Injection.
    istio-injection hat Vorrang vor istio.io/rev (wie im Injector-Webhook)."""
    result = {}
    for ns in namespaces:
        labels = meta(ns).get("labels") or {}
        injection = labels.get("istio-injection")
        if injection == "enabled":
            result[meta(ns)["name"]] = "default"
        elif injection is None and "istio.io/rev" in labels:
            result[meta(ns)["name"]] = labels["istio.io/rev"]
    return result


def check_dataplane(namespaces: Result, pods: Result, deployments: Result, valid_revisions: set[str],
                    restart_warn: int) -> list[Finding]:
    if namespaces[1] or pods[1]:
        return [not_checkable("Data Plane", namespaces[1] or pods[1])]
    mesh = mesh_namespaces(namespaces[0])
    findings = []
    if not mesh:
        findings.append((INFO, "kein Namespace mit istio-injection=enabled oder istio.io/rev"))

    for ns, rev in sorted(mesh.items()):
        if rev not in valid_revisions:
            findings.append((FAIL, f"Namespace {ns}: istio.io/rev={rev}, aber keine istiod-Revision "
                                   f"oder Revision-Tag dieses Namens ({', '.join(sorted(valid_revisions))})"))
        ns_pods = [p for p in pods[0] if meta(p).get("namespace") == ns and is_running(p)]
        without = [meta(p)["name"] for p in ns_pods if not has_sidecar(p) and not injection_disabled(p)
                   and not p.get("spec", {}).get("hostNetwork")]
        if without:
            findings.append((WARN, f"Namespace {ns}: {len(without)} Pods ohne Sidecar "
                                   f"(vor Aktivierung der Injection gestartet?): {short_list(without)}"))
        else:
            findings.append((OK, f"Namespace {ns} (Revision {rev}): {len(ns_pods)} laufende Pods, alle mit Sidecar"))

    # Deployments, deren Pods gar nicht erst angelegt werden (Webhook, SCC, Quota)
    for d in deployments[0]:
        if meta(d).get("namespace") not in mesh:
            continue
        for cond in d.get("status", {}).get("conditions", []):
            if cond.get("type") == "ReplicaFailure" and cond.get("status") == "True":
                message = cond.get("message", "")
                hint = ""
                if "security context constraint" in message:
                    hint = " -> OpenShift: SCC anyuid fuer system:serviceaccounts:" + meta(d)["namespace"] + " noetig"
                elif "sidecar-injector.istio.io" in message:
                    hint = " -> Injector-Webhook (istiod) nicht erreichbar"
                findings.append((FAIL, f"Deployment {ref(d)}: {message[:200]}{hint}"))

    not_ready, restarting = [], []
    for pod in pods[0]:
        if not is_running(pod):
            continue
        for status in all_statuses(pod):
            if status.get("name") != "istio-proxy":
                continue
            if not status.get("ready"):
                not_ready.append(ref(pod))
            if status.get("restartCount", 0) >= restart_warn:
                restarting.append(f"{ref(pod)} ({status['restartCount']}x)")
    if not_ready:
        findings.append((WARN, f"{len(not_ready)} Pods mit nicht bereitem istio-proxy: {short_list(not_ready)}"))
    if restarting:
        findings.append((WARN, f"{len(restarting)} istio-proxy mit >= {restart_warn} Restarts: {short_list(restarting)}"))
    return findings


# --- Ausgabe -----------------------------------------------------------

def print_header(title: str) -> None:
    print()
    print(f"--- {title} " + "-" * max(1, 50 - len(title)))


def print_findings(title: str, findings: list[Finding], problems_only: bool) -> None:
    shown = [f for f in findings if not problems_only or SEVERITY[f[0]] > 0]
    if problems_only and not shown:
        return
    print_header(title)
    for status, text in shown or [(INFO, "nichts zu pruefen")]:
        print(f"  [{status:<4}] {text}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--context", help="oc/kubectl Context (optional)")
    parser.add_argument("--cli", choices=("oc", "kubectl"), help="Kubernetes-CLI (Default: oc, sonst kubectl)")
    parser.add_argument("--istio-namespace", default="istio-system", help="Namespace von istiod (Default: istio-system)")
    parser.add_argument("-n", "--namespace", action="append",
                        help="Pods/Deployments/Services/Gateways nur in diesem Namespace pruefen (mehrfach "
                             "angebbar, Istio-Namespace immer dabei). Fuer Benutzer ohne clusterweite Leserechte.")
    parser.add_argument("--cert-warn-days", type=int, default=30, help="Warnung, wenn Zertifikat frueher ablaeuft (Default: 30)")
    parser.add_argument("--restart-warn", type=int, default=5, help="Warnung ab so vielen Container-Restarts (Default: 5)")
    parser.add_argument("--no-sync", action="store_true", help="Proxy-Sync-Status (exec in istiod) ueberspringen")
    parser.add_argument("--problems-only", action="store_true", help="nur WARN/FAIL ausgeben")
    args = parser.parse_args()
    # Windows-Konsole (cp1252/cp850) darf an Sonderzeichen aus oc-Fehlermeldungen nicht abbrechen
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(errors="replace")

    istio_ns = args.istio_namespace
    kubectl = with_context(detect_cli(args.cli), args.context)
    scope = list(dict.fromkeys([istio_ns, *args.namespace])) if args.namespace else None

    # Erst Verbindung/Login pruefen, sonst kommt derselbe Fehler aus jedem Check
    _, err = get_json(kubectl, "namespace", istio_ns)
    if err:
        print(f"[FAIL] Namespace {istio_ns} nicht lesbar ({kubectl[0]}): {err}")
        sys.exit(2)

    with ThreadPoolExecutor(max_workers=14) as pool:
        jobs = {
            "namespaces": pool.submit(get_json, kubectl, "namespaces", *(args.namespace or [])),
            "crds": pool.submit(get_names, kubectl, "customresourcedefinitions"),
            "mutating": pool.submit(get_json, kubectl, "mutatingwebhookconfigurations"),
            "validating": pool.submit(get_json, kubectl, "validatingwebhookconfigurations"),
            "daemonsets": pool.submit(get_json, kubectl, "daemonsets", "-A"),
            "deployments": pool.submit(get_scoped, kubectl, "deployments", scope),
            "pods": pool.submit(get_scoped, kubectl, "pods", scope),
            "services": pool.submit(get_scoped, kubectl, "services", scope),
            "gateways": pool.submit(get_scoped, kubectl, "gateways.networking.istio.io", scope),
            "root_cert": pool.submit(get_pem_fields, kubectl, "configmap", istio_ns, "istio-ca-root-cert",
                                     ("root-cert.pem",)),
        }
        ca_jobs = [(name, pool.submit(get_pem_fields, kubectl, "secret", istio_ns, name, ("ca-cert.pem",)))
                   for name in ("cacerts", "istio-ca-secret")]
        syncz_job = None if args.no_sync else pool.submit(fetch_syncz, kubectl, istio_ns)
        data = {key: job.result() for key, job in jobs.items()}

        credential_jobs = [(gw, ns, name, pool.submit(get_pem_fields, kubectl, "secret", ns, name,
                                                      SERVER_CERT_KEYS + CA_CERT_KEYS))
                           for gw, ns, name in gateway_credentials(data["gateways"][0], data["pods"][0])]
        credentials = [(gw, ns, name, job.result()) for gw, ns, name, job in credential_jobs]
        ca_secrets = [(name, job.result()) for name, job in ca_jobs]
        syncz = syncz_job.result() if syncz_job else None

    is_openshift = OPENSHIFT_CRD in data["crds"][0]
    valid_revisions = istiod_revisions(data["deployments"][0], istio_ns) | revision_tags(data["mutating"][0])

    sections = [
        ("Control Plane (istiod)", check_control_plane(data["deployments"], data["pods"], istio_ns, args.restart_warn)),
        ("CRDs", check_crds(data["crds"], is_openshift)),
        ("Webhooks (Injection/Validierung)",
         check_webhooks(data["mutating"], data["validating"], data["services"], istio_ns)),
        ("Gateways", check_gateways(data["deployments"], data["services"], data["gateways"], data["pods"])),
        ("Node-Agents (istio-cni/ztunnel)", check_node_agents(data["daemonsets"], data["namespaces"], is_openshift)),
        ("Zertifikate", check_certificates(data["root_cert"], ca_secrets, credentials, istio_ns, args.cert_warn_days)),
    ]
    if syncz is not None:
        sections.append(("Proxy-Sync (istiod /debug/syncz)", check_sync(syncz, data["pods"], istio_ns)))
    sections.append(("Data Plane (Injection/Sidecars)",
                     check_dataplane(data["namespaces"], data["pods"], data["deployments"], valid_revisions,
                                     args.restart_warn)))

    print("=" * 60)
    print(f"Istio-Healthcheck  Namespace: {istio_ns}  CLI: {kubectl[0]}")
    print("=" * 60)
    for title, findings in sections:
        print_findings(title, findings, args.problems_only)

    all_findings = [f for _, findings in sections for f in findings]
    counts = {s: sum(1 for f in all_findings if f[0] == s) for s in (OK, WARN, FAIL)}
    worst = max((SEVERITY[s] for s, _ in all_findings), default=0)
    print()
    print("=" * 60)
    print(f"Ergebnis: {(OK, WARN, FAIL)[worst]}  ({counts[OK]} OK, {counts[WARN]} WARN, {counts[FAIL]} FAIL)")
    sys.exit(worst)


if __name__ == "__main__":
    main()
