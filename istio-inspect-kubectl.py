#!/usr/bin/env python3
"""Zeigt fuer einen Namespace, was per Istio exponiert ist: Gateways, Hosts,
VirtualServices, DestinationRules (inkl. TLS), ServiceEntries, PeerAuthentication
und den Sidecar-Sync-Status. Variante ohne istioctl: nutzt nur die
Python-Standardbibliothek (ab 3.9) und `kubectl` oder `oc`. Der Sync-Status wird per
`exec` in jedem istiod-Pod ueber `pilot-discovery request GET /debug/syncz` gelesen
(benoetigt RBAC-Recht `pods/exec` im istiod-Namespace).

Verwendung:
    python3 istio-inspect-kubectl.py <namespace> [--context CONTEXT] [--cli kubectl|oc]
                                     [--istio-namespace istio-system]
"""

import argparse
import json
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Iterable, Optional

# Voll qualifizierte Ressourcennamen, damit z.B. "gateway" nicht mit der
# Kubernetes Gateway API (gateway.networking.k8s.io) verwechselt wird.
RESOURCES = {
    "pods": "pods",
    "services": "services",
    "gateways": "gateways.networking.istio.io",
    "virtualservices": "virtualservices.networking.istio.io",
    "destinationrules": "destinationrules.networking.istio.io",
    "serviceentries": "serviceentries.networking.istio.io",
    "peerauthentications": "peerauthentications.security.istio.io",
    "authorizationpolicies": "authorizationpolicies.security.istio.io",
    # nur auf OpenShift vorhanden, sonst wird die Sektion ausgeblendet
    "routes": "routes.route.openshift.io",
}

# (items, fehlermeldung) - bei Fehler ist items leer
Result = tuple[list, Optional[str]]
RowFn = Callable[[dict], Iterable[list]]


def run(cmd: list[str]) -> tuple[int, str, str]:
    """Fuehrt cmd aus und liefert (returncode, stdout, stderr)."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        return 127, "", f"Befehl nicht gefunden: {cmd[0]}"
    return proc.returncode, proc.stdout, proc.stderr


def with_context(binary: str, context: Optional[str]) -> list[str]:
    return [binary, "--context", context] if context else [binary]


def detect_cli(preferred: Optional[str]) -> str:
    """kubectl bevorzugen, sonst oc; beide verstehen dieselben get/exec-Aufrufe."""
    if preferred:
        return preferred
    return next((b for b in ("kubectl", "oc") if shutil.which(b)), "kubectl")


def is_missing_api(err: Optional[str]) -> bool:
    return bool(err) and "doesn't have a resource type" in err


def kubectl_json(kubectl: list[str], resource: str, namespace: str, *extra: str) -> Result:
    rc, out, err = run(kubectl + ["get", resource, "-n", namespace, "-o", "json", *extra])
    if rc != 0:
        return [], (err or out).strip()
    try:
        return json.loads(out).get("items", []), None
    except json.JSONDecodeError:
        return [], "ungueltiges JSON von kubectl erhalten"


# --- Ausgabe -----------------------------------------------------------

def print_header(title: str) -> None:
    print()
    print(f"--- {title} " + "-" * max(1, 50 - len(title)))


def print_indented(text: str) -> None:
    print("\n".join(f"  {line}" for line in text.splitlines()))


def print_table(headers: list[str], rows: list[list]) -> None:
    if not rows:
        print("  (keine Eintraege)")
        return
    rows = [[str(c) for c in row] for row in rows]
    widths = [max(len(h), *(len(row[i]) for row in rows)) for i, h in enumerate(headers)]
    fmt = "  ".join("{:<" + str(w) + "}" for w in widths)
    print("  " + fmt.format(*headers))
    for row in rows:
        print("  " + fmt.format(*row))


def print_section(title: str, result: Result, headers: list[str], row_fn: RowFn) -> None:
    print_header(title)
    items, err = result
    if err:
        print(f"  Fehler: {err}")
        return
    print_table(headers, [row for item in items for row in row_fn(item)])


# --- Zeilen je Ressource -----------------------------------------------

def pod_rows(pod: dict):
    spec = pod.get("spec", {})
    statuses = pod.get("status", {}).get("containerStatuses", [])
    ready = sum(1 for s in statuses if s.get("ready"))
    # Native Sidecars (Istio >= 1.19) laufen als initContainer mit restartPolicy=Always
    containers = spec.get("containers", []) + spec.get("initContainers", [])
    has_sidecar = any(c.get("name") == "istio-proxy" for c in containers)
    yield [pod["metadata"]["name"], f"{ready}/{len(statuses)}", "ja" if has_sidecar else "nein"]


def service_ports(svc: dict) -> str:
    return ",".join(f"{p.get('port')}/{p.get('protocol', 'TCP')}" for p in svc.get("spec", {}).get("ports", []))


def service_rows(svc: dict):
    spec = svc.get("spec", {})
    selector = ",".join(f"{k}={v}" for k, v in (spec.get("selector") or {}).items())
    yield [svc["metadata"]["name"], spec.get("type", "ClusterIP"), spec.get("clusterIP", "-"),
           service_ports(svc), selector or "-"]


def gateway_rows(gw: dict):
    for server in gw.get("spec", {}).get("servers", []):
        port = server.get("port", {})
        tls = server.get("tls", {})
        yield [gw["metadata"]["name"], ",".join(server.get("hosts", [])),
               f"{port.get('number')}/{port.get('protocol')}",
               tls.get("mode", "-"), tls.get("credentialName", "-")]


def extract_destinations(vs_spec: dict) -> list[str]:
    dests = []
    for route_kind in ("http", "tls", "tcp"):
        for route in vs_spec.get(route_kind, []):
            for r in route.get("route", []):
                dest = r.get("destination", {})
                subset = f"/{dest['subset']}" if "subset" in dest else ""
                dests.append(dest.get("host", "?") + subset)
    return dests


def virtualservice_rows(vs: dict):
    spec = vs.get("spec", {})
    yield [vs["metadata"]["name"], ",".join(spec.get("hosts", [])),
           ",".join(spec.get("gateways", [])) or "-",
           ",".join(extract_destinations(spec)) or "-"]


def destinationrule_rows(dr: dict):
    spec = dr.get("spec", {})
    subsets = ",".join(s.get("name", "?") for s in spec.get("subsets", [])) or "-"
    tls_mode = spec.get("trafficPolicy", {}).get("tls", {}).get("mode", "-")
    yield [dr["metadata"]["name"], spec.get("host", "-"), subsets, tls_mode]


def serviceentry_rows(se: dict):
    spec = se.get("spec", {})
    ports = ",".join(f"{p.get('number')}/{p.get('protocol')}" for p in spec.get("ports", []))
    yield [se["metadata"]["name"], ",".join(spec.get("hosts", [])), ports,
           spec.get("location", "-"), spec.get("resolution", "-")]


def peerauthentication_rows(pa: dict):
    spec = pa.get("spec", {})
    mode = spec.get("mtls", {}).get("mode", "(vererbt von mesh/ns-default)")
    yield [pa["metadata"]["name"], mode, "workload" if spec.get("selector") else "namespace"]


def authorizationpolicy_rows(ap: dict):
    spec = ap.get("spec", {})
    yield [ap["metadata"]["name"], spec.get("action", "ALLOW"), len(spec.get("rules", []))]


def route_target(rt: dict) -> str:
    spec = rt.get("spec", {})
    port = (spec.get("port") or {}).get("targetPort")
    return spec.get("to", {}).get("name", "?") + (f":{port}" if port else "")


def route_rows(rt: dict):
    spec = rt.get("spec", {})
    tls = spec.get("tls") or {}
    yield [rt["metadata"]["name"], spec.get("host", "-"), spec.get("path", "/"),
           route_target(rt), tls.get("termination", "-")]


# --- Sektionen ohne Tabelle --------------------------------------------

XDS_TYPES = (("CDS", "cluster"), ("LDS", "listener"), ("EDS", "endpoint"), ("RDS", "route"))


def xds_status(entry: dict, prefix: str) -> str:
    """Gleiche Logik wie istioctl proxy-status: gesendet == bestaetigt -> SYNCED."""
    sent = entry.get(f"{prefix}_sent", "")
    if not sent:
        return "NOT SENT"
    return "SYNCED" if sent == entry.get(f"{prefix}_acked", "") else "STALE"


def fetch_syncz(kubectl: list[str], istio_namespace: str) -> Result:
    """Fragt jeden laufenden istiod-Pod ab - jeder kennt nur die bei ihm verbundenen Proxys.
    Der Service-Port 15014 verlangt in neueren Istio-Versionen Auth, daher per exec."""
    pods, err = kubectl_json(kubectl, "pods", istio_namespace, "-l", "app=istiod")
    if err:
        return [], err
    names = [p["metadata"]["name"] for p in pods if p.get("status", {}).get("phase") == "Running"]
    if not names:
        return [], f"kein laufender istiod-Pod (app=istiod) in Namespace {istio_namespace}"
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
    return entries, "; ".join(errors) if errors and not entries else None


def section_proxy_status(result: Result, namespace: str) -> None:
    print_header("Proxy-Sync-Status (istiod /debug/syncz)")
    items, err = result
    if err:
        print(f"  Fehler: {err}")
        return
    rows = []
    for entry in items:
        proxy = entry.get("proxy", "")
        if not proxy.endswith(f".{namespace}"):
            continue
        rows.append([proxy, entry.get("cluster_id", "-"), entry["istiod"],
                     *(xds_status(entry, prefix) for _, prefix in XDS_TYPES),
                     entry.get("istio_version", "-")])
    if rows:
        print_table(["NAME", "CLUSTER", "ISTIOD", *(t for t, _ in XDS_TYPES), "VERSION"], rows)
    else:
        print("  (keine Proxys in diesem Namespace gefunden)")


def section_exposure_summary(namespace: str, gateways: list, virtualservices: list, services: list,
                             routes: list) -> None:
    print_header("Exposure-Zusammenfassung")
    printed = False
    for gw in gateways:
        gw_name = gw["metadata"]["name"]
        gw_refs = {gw_name, f"{namespace}/{gw_name}"}
        matching_vs = [vs["metadata"]["name"] for vs in virtualservices
                       if gw_refs.intersection(vs.get("spec", {}).get("gateways", []))]
        vs_str = ", ".join(matching_vs) if matching_vs else "(keine VirtualService gebunden)"
        for server in gw.get("spec", {}).get("servers", []):
            port = server.get("port", {})
            print(f"  Gateway '{gw_name}': {port.get('protocol')}:{port.get('number')} "
                  f"host={','.join(server.get('hosts', []))} "
                  f"tls={server.get('tls', {}).get('mode', '-')} -> VS: {vs_str}")
            printed = True
    for svc in services:
        svc_type = svc.get("spec", {}).get("type", "ClusterIP")
        if svc_type in ("LoadBalancer", "NodePort"):
            print(f"  Service '{svc['metadata']['name']}' ({svc_type}) direkt exponiert, "
                  f"ohne Istio-Gateway: {service_ports(svc)}")
            printed = True
    for rt in routes:
        spec = rt.get("spec", {})
        scheme = "https" if spec.get("tls") else "http"
        print(f"  Route '{rt['metadata']['name']}': {scheme}://{spec.get('host', '?')}{spec.get('path', '')} "
              f"-> Service {route_target(rt)}")
        printed = True
    if not printed:
        print("  (kein Ingress-Gateway, keine Route und kein LoadBalancer/NodePort-Service gefunden)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("namespace", help="zu untersuchender Kubernetes-Namespace")
    parser.add_argument("--context", help="kubectl/oc Context (optional)")
    parser.add_argument("--cli", choices=("kubectl", "oc"), help="Kubernetes-CLI (Default: kubectl, sonst oc)")
    parser.add_argument("--istio-namespace", default="istio-system", help="Namespace von istiod (Default: istio-system)")
    args = parser.parse_args()
    ns = args.namespace

    cli = detect_cli(args.cli)
    if not shutil.which(cli):
        print(f"Warnung: '{cli}' nicht im PATH gefunden.", file=sys.stderr)

    kubectl = with_context(cli, args.context)

    # Alle Abfragen parallel starten, Ausgabe erfolgt danach in fester Reihenfolge
    with ThreadPoolExecutor(max_workers=len(RESOURCES) + 1) as pool:
        futures = {key: pool.submit(kubectl_json, kubectl, res, ns) for key, res in RESOURCES.items()}
        syncz_future = pool.submit(fetch_syncz, kubectl, args.istio_namespace)
        data = {key: f.result() for key, f in futures.items()}

        print("=" * 60)
        print(f"Namespace: {ns}")
        print("=" * 60)

        print_section("Pods & Sidecar-Status", data["pods"],
                      ["NAME", "READY", "SIDECAR"], pod_rows)
        section_proxy_status(syncz_future.result(), ns)
        print_section("Services", data["services"],
                      ["NAME", "TYPE", "CLUSTER-IP", "PORTS", "SELECTOR"], service_rows)
        print_section("Gateways", data["gateways"],
                      ["GATEWAY", "HOSTS", "PORT/PROTOKOLL", "TLS-MODE", "CREDENTIAL"], gateway_rows)
        print_section("VirtualServices", data["virtualservices"],
                      ["NAME", "HOSTS", "GATEWAYS", "ZIEL(E)"], virtualservice_rows)
        print_section("DestinationRules (inkl. TLS-Origination/mTLS)", data["destinationrules"],
                      ["NAME", "HOST", "SUBSETS", "TLS-MODE"], destinationrule_rows)
        print_section("ServiceEntries (Egress / externe Hosts)", data["serviceentries"],
                      ["NAME", "HOSTS", "PORTS", "LOCATION", "RESOLUTION"], serviceentry_rows)
        print_section("PeerAuthentication (mTLS-Modus)", data["peerauthentications"],
                      ["NAME", "MTLS-MODE", "SCOPE"], peerauthentication_rows)
        print_section("AuthorizationPolicy", data["authorizationpolicies"],
                      ["NAME", "ACTION", "RULES"], authorizationpolicy_rows)
        if not is_missing_api(data["routes"][1]):
            print_section("OpenShift Routes", data["routes"],
                          ["NAME", "HOST", "PATH", "ZIEL", "TLS"], route_rows)
        section_exposure_summary(ns, data["gateways"][0], data["virtualservices"][0], data["services"][0],
                                 data["routes"][0])


if __name__ == "__main__":
    main()
