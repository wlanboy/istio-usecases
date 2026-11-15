#!/usr/bin/env python3
"""Zeigt fuer einen Namespace, was per Istio exponiert ist: Gateways, Hosts,
VirtualServices, DestinationRules (inkl. TLS), ServiceEntries, PeerAuthentication
und den Sidecar-Sync-Status. Nutzt nur die Python-Standardbibliothek (ab 3.9) und
`oc` oder `kubectl`; istiocli.py muss im selben Verzeichnis liegen.

Ist `istioctl` im PATH, kommt der Sync-Status aus `istioctl proxy-status` und am Ende
laeuft `istioctl analyze`. Ohne istioctl (oder mit --no-istioctl) liest das Skript den
Sync-Status per `exec` in jedem istiod-Pod ueber `pilot-discovery request GET /debug/syncz`
(RBAC-Recht `pods/exec` im istiod-Namespace), analyze entfaellt dann.

Verwendung:
    python3 istio-inspect.py <namespace> [--context CONTEXT] [--cli oc|kubectl]
                             [--istio-namespace istio-system] [--no-istioctl] [--no-analyze]
"""

import argparse
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Iterable, Optional

from istiocli import (XDS_TYPES, Result, detect_cli, fetch_syncz, get_json, print_header, run,
                      with_context, xds_status)

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

RowFn = Callable[[dict], Iterable[list]]


def is_missing_api(err: Optional[str]) -> bool:
    return bool(err) and "doesn't have a resource type" in err


# --- Ausgabe -----------------------------------------------------------

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

def section_proxy_status_istioctl(result: tuple[int, str, str], namespace: str) -> None:
    print_header("istioctl proxy-status (Sync mit istiod)")
    rc, out, err = result
    if rc != 0:
        print(f"  Fehler: {(err or out).strip()}")
        return
    lines = [l for l in out.splitlines() if f".{namespace} " in l or l.startswith("NAME")]
    if len(lines) > 1:
        print_indented("\n".join(lines))
    else:
        print("  (keine Proxys in diesem Namespace gefunden)")


def section_proxy_status_syncz(result: tuple[list, list[str]], namespace: str) -> None:
    print_header("Proxy-Sync-Status (istiod /debug/syncz)")
    items, errors = result
    # Fehler einzelner istiod-Pods immer zeigen: deren Proxys fehlen sonst unbemerkt in der Tabelle
    for err in errors:
        print(f"  Fehler: {err}")
    if not items and errors:
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


def section_analyze(result: tuple[int, str, str]) -> None:
    print_header("istioctl analyze")
    _, out, err = result
    # analyze schreibt Meldungen teils nach stderr und endet bei Befunden mit rc != 0
    text = (out + err).strip()
    if text:
        print_indented(text)
    else:
        print("  keine Probleme gefunden")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("namespace", help="zu untersuchender Kubernetes-Namespace")
    parser.add_argument("--context", help="oc/kubectl/istioctl Context (optional)")
    parser.add_argument("--cli", choices=("oc", "kubectl"), help="Kubernetes-CLI (Default: oc, sonst kubectl)")
    parser.add_argument("--istio-namespace", default="istio-system", help="Namespace von istiod (Default: istio-system)")
    parser.add_argument("--no-istioctl", action="store_true",
                        help="istioctl nicht verwenden, Sync-Status per exec in istiod lesen")
    parser.add_argument("--no-analyze", action="store_true", help="istioctl analyze ueberspringen (kann dauern)")
    args = parser.parse_args()
    ns = args.namespace

    cli = detect_cli(args.cli)
    if not shutil.which(cli):
        print(f"Warnung: '{cli}' nicht im PATH gefunden.", file=sys.stderr)
    kubectl = with_context(cli, args.context)
    use_istioctl = not args.no_istioctl and shutil.which("istioctl") is not None
    istioctl = with_context("istioctl", args.context)

    # Alle Abfragen parallel starten, Ausgabe erfolgt danach in fester Reihenfolge
    with ThreadPoolExecutor(max_workers=len(RESOURCES) + 2) as pool:
        futures = {key: pool.submit(get_json, kubectl, res, "-n", ns) for key, res in RESOURCES.items()}
        proxy_future = pool.submit(run, istioctl + ["proxy-status", "--istioNamespace", args.istio_namespace]) \
            if use_istioctl else None
        syncz_future = None if use_istioctl else pool.submit(fetch_syncz, kubectl, args.istio_namespace)
        analyze_future = pool.submit(run, istioctl + ["analyze", "-n", ns]) \
            if use_istioctl and not args.no_analyze else None
        data = {key: f.result() for key, f in futures.items()}

        print("=" * 60)
        print(f"Namespace: {ns}  CLI: {kubectl[0]}  Sync-Status: {'istioctl' if use_istioctl else 'exec in istiod'}")
        print("=" * 60)

        print_section("Pods & Sidecar-Status", data["pods"],
                      ["NAME", "READY", "SIDECAR"], pod_rows)
        if proxy_future:
            section_proxy_status_istioctl(proxy_future.result(), ns)
        if syncz_future:
            section_proxy_status_syncz(syncz_future.result(), ns)
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
        if analyze_future:
            section_analyze(analyze_future.result())


if __name__ == "__main__":
    main()
