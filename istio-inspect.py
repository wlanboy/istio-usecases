#!/usr/bin/env python3
"""Zeigt fuer einen Namespace, was per Istio exponiert ist: Gateways, Hosts,
VirtualServices, DestinationRules (inkl. TLS), ServiceEntries, PeerAuthentication
und den Sidecar-Sync-Status. Nutzt nur die Python-Standardbibliothek und ruft
`kubectl`/`istioctl` als externe Befehle auf.

Verwendung:
    python3 istio-inspect.py <namespace> [--context CONTEXT] [--no-analyze]
"""

import argparse
import json
import shutil
import subprocess
import sys


def run(cmd):
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    except FileNotFoundError:
        return None, f"Befehl nicht gefunden: {cmd[0]}"
    except subprocess.CalledProcessError as e:
        return None, (e.stderr or e.stdout or "").strip()
    return result.stdout, None


def kubectl_json(args, namespace, context):
    cmd = ["kubectl"]
    if context:
        cmd += ["--context", context]
    cmd += ["get", *args, "-n", namespace, "-o", "json"]
    out, err = run(cmd)
    if out is None:
        return [], err
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return [], "ungueltiges JSON von kubectl erhalten"
    return data.get("items", []), None


def print_header(title):
    print()
    print(f"--- {title} " + "-" * max(1, 50 - len(title)))


def print_table(headers, rows):
    if not rows:
        print("  (keine Eintraege)")
        return
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))
    fmt = "  ".join("{:<" + str(w) + "}" for w in widths)
    print("  " + fmt.format(*headers))
    for row in rows:
        print("  " + fmt.format(*[str(c) for c in row]))


def short_host(host):
    """Reduziert 'svc.ns.svc.cluster.local' auf 'svc.ns', laesst kurze Namen unveraendert."""
    parts = host.split(".")
    if len(parts) >= 2 and parts[-1] in ("local",):
        return ".".join(parts[:2])
    return host


# --- Sektionen ---------------------------------------------------------

def section_pods(namespace, context):
    print_header("Pods & Sidecar-Status")
    items, err = kubectl_json(["pods"], namespace, context)
    if err:
        print(f"  Fehler: {err}")
        return
    rows = []
    for pod in items:
        name = pod["metadata"]["name"]
        statuses = pod.get("status", {}).get("containerStatuses", [])
        ready = sum(1 for s in statuses if s.get("ready"))
        total = len(statuses)
        has_sidecar = any(c["name"] == "istio-proxy" for c in pod["spec"].get("containers", []))
        rows.append([name, f"{ready}/{total}", "ja" if has_sidecar else "nein"])
    print_table(["NAME", "READY", "SIDECAR"], rows)


def section_proxy_status(namespace, context):
    print_header("istioctl proxy-status (Sync mit istiod)")
    cmd = ["istioctl"]
    if context:
        cmd += ["--context", context]
    cmd += ["proxy-status"]
    out, err = run(cmd)
    if err:
        print(f"  Fehler: {err}")
        return
    lines = [l for l in out.splitlines() if f".{namespace} " in l or l.startswith("NAME")]
    print("\n".join(f"  {l}" for l in lines) if len(lines) > 1 else "  (keine Proxys in diesem Namespace gefunden)")


def section_services(namespace, context):
    print_header("Services")
    items, err = kubectl_json(["svc"], namespace, context)
    if err:
        print(f"  Fehler: {err}")
        return [], []
    rows = []
    exposed_directly = []
    for svc in items:
        name = svc["metadata"]["name"]
        spec = svc["spec"]
        svc_type = spec.get("type", "ClusterIP")
        ports = ",".join(f"{p.get('port')}/{p.get('protocol', 'TCP')}" for p in spec.get("ports", []))
        selector = ",".join(f"{k}={v}" for k, v in (spec.get("selector") or {}).items())
        rows.append([name, svc_type, spec.get("clusterIP", "-"), ports, selector or "-"])
        if svc_type in ("LoadBalancer", "NodePort"):
            exposed_directly.append((name, svc_type, ports))
    print_table(["NAME", "TYPE", "CLUSTER-IP", "PORTS", "SELECTOR"], rows)
    return items, exposed_directly


def section_gateways(namespace, context):
    print_header("Gateways")
    items, err = kubectl_json(["gateway"], namespace, context)
    if err:
        print(f"  Fehler: {err}")
        return []
    rows = []
    for gw in items:
        name = gw["metadata"]["name"]
        for server in gw["spec"].get("servers", []):
            port = server.get("port", {})
            tls = server.get("tls", {})
            rows.append([
                name,
                ",".join(server.get("hosts", [])),
                f"{port.get('number')}/{port.get('protocol')}",
                tls.get("mode", "-"),
                tls.get("credentialName", "-"),
            ])
    print_table(["GATEWAY", "HOSTS", "PORT/PROTOKOLL", "TLS-MODE", "CREDENTIAL"], rows)
    return items


def extract_destinations(vs_spec):
    dests = []
    for route_kind in ("http", "tls", "tcp"):
        for route in vs_spec.get(route_kind, []):
            for r in route.get("route", []):
                dest = r.get("destination", {})
                dests.append(dest.get("host", "?") + (f"/{dest['subset']}" if "subset" in dest else ""))
    return dests


def section_virtualservices(namespace, context):
    print_header("VirtualServices")
    items, err = kubectl_json(["virtualservice"], namespace, context)
    if err:
        print(f"  Fehler: {err}")
        return []
    rows = []
    for vs in items:
        name = vs["metadata"]["name"]
        spec = vs["spec"]
        hosts = ",".join(spec.get("hosts", []))
        gateways = ",".join(spec.get("gateways", [])) or "-"
        dests = ",".join(extract_destinations(spec)) or "-"
        rows.append([name, hosts, gateways, dests])
    print_table(["NAME", "HOSTS", "GATEWAYS", "ZIEL(E)"], rows)
    return items


def section_destinationrules(namespace, context):
    print_header("DestinationRules (inkl. TLS-Origination/mTLS)")
    items, err = kubectl_json(["destinationrule"], namespace, context)
    if err:
        print(f"  Fehler: {err}")
        return []
    rows = []
    for dr in items:
        name = dr["metadata"]["name"]
        spec = dr["spec"]
        host = spec.get("host", "-")
        subsets = ",".join(s.get("name", "?") for s in spec.get("subsets", [])) or "-"
        tls_mode = spec.get("trafficPolicy", {}).get("tls", {}).get("mode", "-")
        rows.append([name, host, subsets, tls_mode])
    print_table(["NAME", "HOST", "SUBSETS", "TLS-MODE"], rows)
    return items


def section_serviceentries(namespace, context):
    print_header("ServiceEntries (Egress / externe Hosts)")
    items, err = kubectl_json(["serviceentry"], namespace, context)
    if err:
        print(f"  Fehler: {err}")
        return
    rows = []
    for se in items:
        name = se["metadata"]["name"]
        spec = se["spec"]
        hosts = ",".join(spec.get("hosts", []))
        ports = ",".join(f"{p.get('number')}/{p.get('protocol')}" for p in spec.get("ports", []))
        rows.append([name, hosts, ports, spec.get("location", "-"), spec.get("resolution", "-")])
    print_table(["NAME", "HOSTS", "PORTS", "LOCATION", "RESOLUTION"], rows)


def section_peerauthentication(namespace, context):
    print_header("PeerAuthentication (mTLS-Modus)")
    items, err = kubectl_json(["peerauthentication"], namespace, context)
    if err:
        print(f"  Fehler: {err}")
        return
    rows = []
    for pa in items:
        name = pa["metadata"]["name"]
        spec = pa["spec"]
        mode = spec.get("mtls", {}).get("mode", "(vererbt von mesh/ns-default)")
        scope = "workload" if spec.get("selector") else "namespace"
        rows.append([name, mode, scope])
    print_table(["NAME", "MTLS-MODE", "SCOPE"], rows)


def section_authorizationpolicy(namespace, context):
    print_header("AuthorizationPolicy")
    items, err = kubectl_json(["authorizationpolicy"], namespace, context)
    if err:
        print(f"  Fehler: {err}")
        return
    rows = []
    for ap in items:
        name = ap["metadata"]["name"]
        spec = ap["spec"]
        rows.append([name, spec.get("action", "ALLOW"), str(len(spec.get("rules", [])))])
    print_table(["NAME", "ACTION", "RULES"], rows)


def section_exposure_summary(gateways, virtualservices, exposed_directly):
    print_header("Exposure-Zusammenfassung")
    printed = False
    for gw in gateways:
        gw_name = gw["metadata"]["name"]
        for server in gw["spec"].get("servers", []):
            port = server.get("port", {})
            hosts = ",".join(server.get("hosts", []))
            tls_mode = server.get("tls", {}).get("mode", "-")
            matching_vs = [
                vs["metadata"]["name"] for vs in virtualservices
                if gw_name in vs["spec"].get("gateways", [])
                or f"{vs['metadata']['namespace']}/{gw_name}" in vs["spec"].get("gateways", [])
            ]
            vs_str = ", ".join(matching_vs) if matching_vs else "(keine VirtualService gebunden)"
            print(f"  Gateway '{gw_name}': {port.get('protocol')}:{port.get('number')} "
                  f"host={hosts} tls={tls_mode} -> VS: {vs_str}")
            printed = True
    for name, svc_type, ports in exposed_directly:
        print(f"  Service '{name}' ({svc_type}) direkt exponiert, ohne Istio-Gateway: {ports}")
        printed = True
    if not printed:
        print("  (kein Ingress-Gateway und kein LoadBalancer/NodePort-Service gefunden)")


def section_analyze(namespace, context):
    print_header("istioctl analyze")
    cmd = ["istioctl"]
    if context:
        cmd += ["--context", context]
    cmd += ["analyze", "-n", namespace]
    out, err = run(cmd)
    text = (out or "") + (err or "")
    print("\n".join(f"  {l}" for l in text.strip().splitlines()) if text.strip() else "  keine Probleme gefunden")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("namespace", help="zu untersuchender Kubernetes-Namespace")
    parser.add_argument("--context", help="kubectl/istioctl Context (optional)")
    parser.add_argument("--no-analyze", action="store_true", help="istioctl analyze ueberspringen (kann dauern)")
    args = parser.parse_args()

    for binary in ("kubectl", "istioctl"):
        if not shutil.which(binary):
            print(f"Warnung: '{binary}' nicht im PATH gefunden.", file=sys.stderr)

    print("=" * 60)
    print(f"Namespace: {args.namespace}")
    print("=" * 60)

    section_pods(args.namespace, args.context)
    section_proxy_status(args.namespace, args.context)
    _, exposed_directly = section_services(args.namespace, args.context)
    gateways = section_gateways(args.namespace, args.context)
    virtualservices = section_virtualservices(args.namespace, args.context)
    section_destinationrules(args.namespace, args.context)
    section_serviceentries(args.namespace, args.context)
    section_peerauthentication(args.namespace, args.context)
    section_authorizationpolicy(args.namespace, args.context)
    section_exposure_summary(gateways, virtualservices, exposed_directly)
    if not args.no_analyze:
        section_analyze(args.namespace, args.context)


if __name__ == "__main__":
    main()
