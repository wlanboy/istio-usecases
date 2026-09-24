#!/usr/bin/env python3
"""Findet Konflikte und Ueberlagerungen zwischen VirtualServices und
DestinationRules eines Namespaces.

Geprueft wird:
  VS-HOST-DUP          mehrere VirtualServices fuer denselben Host am Sidecar (mesh)
  VS-HOST-WILDCARD     Wildcard-Host eines VirtualService ueberlagert einen konkreten Host
  VS-GW-MERGE          mehrere VirtualServices fuer denselben Host am selben Gateway (werden gemerged)
  VS-GW-SHADOW         beim Gateway-Merge kann eine Route eine Route des anderen VS verdecken
  VS-ROUTE-SHADOWED    Route innerhalb eines VS ist durch eine fruehere Route nie erreichbar
  VS-SUBSET-MISSING    VS verweist auf ein Subset, das keine DestinationRule definiert
  DR-HOST-DUP          mehrere DestinationRules fuer denselben Host (werden gemerged)
  DR-POLICY-CONFLICT   mehrere dieser DestinationRules setzen eine trafficPolicy (nur die aelteste greift)
  DR-SUBSET-DUP        derselbe Subset-Name ist fuer einen Host mehrfach definiert
  DR-HOST-WILDCARD     Wildcard-DestinationRule wird fuer einen Host von einer konkreten DR ersetzt
  DR-SUBSET-LABELS     zwei Subsets eines Hosts selektieren exakt dieselben Labels

Nutzt nur die Python-Standardbibliothek und ruft `kubectl` auf. Mit --file
werden stattdessen lokale YAML-Manifeste gelesen (benoetigt PyYAML).

Verwendung:
    python3 virtualservice.py <namespace> [--context CONTEXT]
    python3 virtualservice.py <namespace> --file manifests/
"""

import argparse
import json
import os
import re
import subprocess
import sys

ERROR = "ERROR"
WARN = "WARN"

KIND_SHORT = {"VirtualService": "vs", "DestinationRule": "dr"}


# --- Laden ---------------------------------------------------------------

def run(cmd):
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    except FileNotFoundError:
        return None, f"Befehl nicht gefunden: {cmd[0]}"
    except subprocess.CalledProcessError as e:
        return None, (e.stderr or e.stdout or "").strip()
    return result.stdout, None


def load_from_cluster(namespace, context):
    cmd = ["kubectl"]
    if context:
        cmd += ["--context", context]
    cmd += ["get", "virtualservices.networking.istio.io,destinationrules.networking.istio.io",
            "-n", namespace, "-o", "json"]
    out, err = run(cmd)
    if out is None:
        sys.exit(f"Fehler beim Lesen aus dem Cluster: {err}")
    return json.loads(out).get("items", [])


def load_from_files(paths, namespace):
    try:
        import yaml
    except ImportError:
        sys.exit("--file benoetigt PyYAML (pip install pyyaml)")
    files = []
    for path in paths:
        if os.path.isdir(path):
            files += sorted(os.path.join(path, f) for f in os.listdir(path)
                            if f.endswith((".yaml", ".yml")))
        else:
            files.append(path)
    items = []
    for f in files:
        with open(f) as fh:
            for doc in yaml.safe_load_all(fh):
                if not doc or doc.get("kind") not in KIND_SHORT:
                    continue
                meta = doc.setdefault("metadata", {})
                meta.setdefault("namespace", namespace)
                if meta["namespace"] == namespace:
                    items.append(doc)
    return items


# --- Hilfsfunktionen: Hosts & Gateways -----------------------------------

def fqdn(host, namespace):
    """Istio loest nur Kurznamen ohne Punkt relativ zum Namespace der Ressource auf."""
    if "." in host or host.startswith("*"):
        return host
    return f"{host}.{namespace}.svc.cluster.local"


def hosts_overlap(a, b):
    if a == b:
        return True
    for wild, other in ((a, b), (b, a)):
        if wild == "*":
            return True
        if wild.startswith("*."):
            suffix = wild[1:]
            other_base = other[1:] if other.startswith("*") else "." + other
            if other_base.endswith(suffix):
                return True
    return False


def norm_gateway(gw, namespace):
    if gw == "mesh" or "/" in gw:
        return gw
    return f"{namespace}/{gw}"


def res(item):
    return f"{KIND_SHORT[item['kind']]}/{item['metadata']['name']}"


def route_label(route, idx):
    return f"'{route['name']}'" if route.get("name") else f"#{idx}"


# --- Hilfsfunktionen: Match-Ueberdeckung ---------------------------------
# covers(a, b) == True bedeutet: jeder Request, der b erfuellt, erfuellt auch a.
# Im Zweifel wird False geliefert (lieber einen Konflikt uebersehen als falsch melden).

def string_covers(a, b, ignore_case=False):
    if not a:
        return True
    if not b:
        return False
    norm = (lambda s: s.lower()) if ignore_case else (lambda s: s)
    (ka, va), (kb, vb) = next(iter(a.items())), next(iter(b.items()))
    va, vb = norm(va), norm(vb)
    if ka == "exact":
        return kb == "exact" and va == vb
    if ka == "prefix":
        return kb in ("exact", "prefix") and vb.startswith(va)
    if ka == "regex":
        if kb == "regex":
            return va == vb
        if kb == "exact":
            try:
                return re.fullmatch(va, vb) is not None
            except re.error:
                return False
    return False


def uri_covers(ma, mb):
    a, b = ma.get("uri"), mb.get("uri")
    if not a or a == {"prefix": "/"}:
        return True
    ia, ib = bool(ma.get("ignoreUriCase")), bool(mb.get("ignoreUriCase"))
    if ib and not ia:
        return False
    return string_covers(a, b, ignore_case=ia)


def map_covers(a, b):
    """Jeder Eintrag in a muss in b mit mindestens gleich strenger Bedingung vorkommen."""
    for key, cond in (a or {}).items():
        if key not in (b or {}):
            return False
        if not string_covers(cond, b[key]):
            return False
    return True


def match_covers(ma, mb, namespace):
    if not uri_covers(ma, mb):
        return False
    for field in ("scheme", "method", "authority"):
        if not string_covers(ma.get(field), mb.get(field)):
            return False
    if not map_covers(ma.get("headers"), mb.get("headers")):
        return False
    if not map_covers(ma.get("queryParams"), mb.get("queryParams")):
        return False
    for key, cond in (ma.get("withoutHeaders") or {}).items():
        if (mb.get("withoutHeaders") or {}).get(key) != cond:
            return False
    for field in ("port", "sourceNamespace"):
        if field in ma and ma[field] != mb.get(field):
            return False
    labels_b = mb.get("sourceLabels") or {}
    if any(labels_b.get(k) != v for k, v in (ma.get("sourceLabels") or {}).items()):
        return False
    if ma.get("gateways"):
        gw_a = {norm_gateway(g, namespace) for g in ma["gateways"]}
        gw_b = {norm_gateway(g, namespace) for g in mb.get("gateways") or []}
        if not gw_b or not gw_b <= gw_a:
            return False
    return True


def route_covers(ra, rb, namespace):
    """True, wenn Route ra jeden Request abfaengt, den Route rb matchen wuerde."""
    matches_a = ra.get("match") or [{}]
    matches_b = rb.get("match") or [{}]
    return all(any(match_covers(ma, mb, namespace) for ma in matches_a) for mb in matches_b)


# --- Pruefungen -----------------------------------------------------------

class Findings:
    def __init__(self):
        self.items = []
        self._seen = set()

    def add(self, severity, code, resources, message):
        resources = sorted(set(resources))
        key = (code, tuple(resources), message)
        if key in self._seen:
            return
        self._seen.add(key)
        self.items.append({"severity": severity, "code": code,
                           "resources": resources, "message": message})


def vs_host_bindings(vss):
    """Liefert (gateway, host_fqdn, vs) fuer jede Host/Gateway-Kombination."""
    for vs in vss:
        ns = vs["metadata"]["namespace"]
        spec = vs.get("spec", {})
        gateways = [norm_gateway(g, ns) for g in spec.get("gateways") or ["mesh"]]
        for host in spec.get("hosts") or []:
            for gw in gateways:
                yield gw, fqdn(host, ns), vs


def check_vs_hosts(vss, findings):
    by_key = {}
    bindings = list(vs_host_bindings(vss))
    for gw, host, vs in bindings:
        by_key.setdefault((gw, host), [])
        if vs not in by_key[(gw, host)]:
            by_key[(gw, host)].append(vs)

    for (gw, host), group in sorted(by_key.items(), key=lambda kv: kv[0]):
        if len(group) < 2:
            continue
        names = [res(v) for v in group]
        if gw == "mesh":
            findings.add(ERROR, "VS-HOST-DUP", names,
                         f"Host {host} ist am Sidecar (mesh) in {len(group)} VirtualServices "
                         f"definiert; Istio merged diese nicht, nur einer wird wirksam (IST0109)")
            continue
        findings.add(WARN, "VS-GW-MERGE", names,
                     f"Host {host} an Gateway {gw} in {len(group)} VirtualServices; Routen "
                     f"werden gemerged, die Reihenfolge ist nicht garantiert")
        ns = group[0]["metadata"]["namespace"]
        for a in group:
            for b in group:
                if a is b:
                    continue
                routes_a = a.get("spec", {}).get("http") or []
                routes_b = b.get("spec", {}).get("http") or []
                for ib, rb in enumerate(routes_b):
                    for ia, ra in enumerate(routes_a):
                        if route_covers(ra, rb, ns):
                            findings.add(ERROR, "VS-GW-SHADOW", [res(a), res(b)],
                                         f"Host {host} an Gateway {gw}: Route {route_label(rb, ib)} "
                                         f"in {res(b)} wird von Route {route_label(ra, ia)} in "
                                         f"{res(a)} verdeckt, falls {res(a)} im Merge zuerst kommt")
                            break

    for gw_a, host_a, vs_a in bindings:
        for gw_b, host_b, vs_b in bindings:
            if gw_a != gw_b or vs_a is vs_b or host_a == host_b:
                continue
            if not host_a.startswith("*") or not hosts_overlap(host_a, host_b):
                continue
            findings.add(WARN, "VS-HOST-WILDCARD", [res(vs_a), res(vs_b)],
                         f"Wildcard-Host {host_a} ({res(vs_a)}) ueberlagert {host_b} ({res(vs_b)}) "
                         f"an {gw_a}; fuer {host_b} gelten nur die Routen von {res(vs_b)}")


def check_vs_routes(vss, findings):
    for vs in vss:
        ns = vs["metadata"]["namespace"]
        routes = vs.get("spec", {}).get("http") or []
        for j, rj in enumerate(routes):
            for i in range(j):
                ri = routes[i]
                if route_covers(ri, rj, ns):
                    reason = ("hat kein Match (Catch-All)" if not ri.get("match")
                              else "matcht bereits alle ihre Requests")
                    findings.add(ERROR, "VS-ROUTE-SHADOWED", [res(vs)],
                                 f"Route {route_label(rj, j)} ist nie erreichbar: fruehere Route "
                                 f"{route_label(ri, i)} {reason}")
                    break


def dr_groups(drs):
    groups = {}
    for dr in drs:
        spec = dr.get("spec", {})
        if not spec.get("host"):
            continue
        host = fqdn(spec["host"], dr["metadata"]["namespace"])
        selector = json.dumps(spec.get("workloadSelector") or {}, sort_keys=True)
        groups.setdefault((host, selector), []).append(dr)
    return groups


def check_drs(drs, findings):
    groups = dr_groups(drs)
    for (host, _), group in sorted(groups.items(), key=lambda kv: kv[0]):
        names = [res(d) for d in group]
        if len(group) > 1:
            findings.add(WARN, "DR-HOST-DUP", names,
                         f"Host {host} hat {len(group)} DestinationRules; Subsets werden gemerged, "
                         f"trafficPolicy kommt nur aus der aeltesten")
            with_policy = [d for d in group if d.get("spec", {}).get("trafficPolicy")]
            if len(with_policy) > 1:
                findings.add(ERROR, "DR-POLICY-CONFLICT", [res(d) for d in with_policy],
                             f"Host {host}: {len(with_policy)} DestinationRules setzen eine "
                             f"trafficPolicy, nur die der aeltesten wird angewendet")

        subset_owner = {}
        label_owner = {}
        for dr in group:
            for subset in dr.get("spec", {}).get("subsets") or []:
                name = subset.get("name")
                if name in subset_owner:
                    findings.add(ERROR, "DR-SUBSET-DUP", [subset_owner[name], res(dr)],
                                 f"Host {host}: Subset '{name}' ist mehrfach definiert, "
                                 f"nur die erste Definition greift")
                else:
                    subset_owner[name] = res(dr)
                labels = json.dumps(subset.get("labels") or {}, sort_keys=True)
                if labels in label_owner and label_owner[labels][1] != name:
                    other_res, other_name = label_owner[labels]
                    findings.add(WARN, "DR-SUBSET-LABELS", [other_res, res(dr)],
                                 f"Host {host}: Subsets '{other_name}' und '{name}' selektieren "
                                 f"dieselben Labels {labels}")
                else:
                    label_owner.setdefault(labels, (res(dr), name))

    for (host_a, sel_a), group_a in groups.items():
        if not host_a.startswith("*"):
            continue
        for (host_b, sel_b), group_b in groups.items():
            if host_a == host_b or sel_a != sel_b or not hosts_overlap(host_a, host_b):
                continue
            if host_b.startswith("*") and len(host_b) < len(host_a):
                continue
            for a in group_a:
                for b in group_b:
                    findings.add(WARN, "DR-HOST-WILDCARD", [res(a), res(b)],
                                 f"Wildcard-DR {res(a)} ({host_a}) gilt nicht fuer {host_b}: "
                                 f"{res(b)} ersetzt sie dort vollstaendig (kein Merge)")


def vs_destinations(vs):
    spec = vs.get("spec", {})
    for kind in ("http", "tcp", "tls"):
        for route in spec.get(kind) or []:
            for dest in route.get("route") or []:
                if dest.get("destination"):
                    yield dest["destination"]
            if route.get("mirror"):
                yield route["mirror"]
            for mirror in route.get("mirrors") or []:
                if mirror.get("destination"):
                    yield mirror["destination"]


def check_subsets(vss, drs, namespace, findings):
    groups = dr_groups(drs)
    local_suffix = f".{namespace}.svc.cluster.local"
    for vs in vss:
        ns = vs["metadata"]["namespace"]
        for dest in vs_destinations(vs):
            subset = dest.get("subset")
            if not subset or not dest.get("host"):
                continue
            host = fqdn(dest["host"], ns)
            matching = [g for (h, _), g in groups.items() if h == host]
            if not matching:
                wild = [(h, g) for (h, _), g in groups.items()
                        if h.startswith("*") and hosts_overlap(h, host)]
                if wild:
                    longest = max(len(h) for h, _ in wild)
                    matching = [g for h, g in wild if len(h) == longest]
            drs_for_host = [d for g in matching for d in g]
            if not drs_for_host:
                if host.endswith(local_suffix):
                    findings.add(ERROR, "VS-SUBSET-MISSING", [res(vs)],
                                 f"Subset '{subset}' fuer {host} referenziert, aber es gibt "
                                 f"keine DestinationRule fuer diesen Host")
                continue
            defined = {s.get("name") for d in drs_for_host
                       for s in d.get("spec", {}).get("subsets") or []}
            if subset not in defined:
                findings.add(ERROR, "VS-SUBSET-MISSING", [res(vs)] + [res(d) for d in drs_for_host],
                             f"Subset '{subset}' fuer {host} ist in keiner DestinationRule "
                             f"definiert (vorhanden: {', '.join(sorted(defined)) or '-'})")


# --- Ausgabe --------------------------------------------------------------

def print_table(findings):
    if not findings:
        print("Keine Konflikte gefunden.")
        return
    for f in findings:
        print(f"[{f['severity']:<5}] {f['code']:<18} {', '.join(f['resources'])}")
        print(f"        {f['message']}")
    errors = sum(1 for f in findings if f["severity"] == ERROR)
    print()
    print(f"{len(findings)} Befund(e), davon {errors} Fehler")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("namespace", help="zu pruefender Namespace")
    parser.add_argument("--context", help="kubectl Context (optional)")
    parser.add_argument("--file", "-f", action="append",
                        help="YAML-Datei oder -Verzeichnis statt Cluster lesen (mehrfach moeglich)")
    args = parser.parse_args()

    if args.file:
        items = load_from_files(args.file, args.namespace)
    else:
        items = load_from_cluster(args.namespace, args.context)

    vss = [i for i in items if i.get("kind") == "VirtualService"]
    drs = [i for i in items if i.get("kind") == "DestinationRule"]

    findings = Findings()
    check_vs_hosts(vss, findings)
    check_vs_routes(vss, findings)
    check_drs(drs, findings)
    check_subsets(vss, drs, args.namespace, findings)
    result = sorted(findings.items, key=lambda f: (f["severity"] != ERROR, f["code"], f["resources"]))

    print(f"Namespace {args.namespace}: {len(vss)} VirtualServices, {len(drs)} DestinationRules")
    print()
    print_table(result)

    sys.exit(1 if any(f["severity"] == ERROR for f in result) else 0)


if __name__ == "__main__":
    main()
