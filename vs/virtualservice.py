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
from dataclasses import dataclass
from itertools import permutations
from typing import NoReturn

ERROR = "ERROR"
WARN = "WARN"

KIND_SHORT = {"VirtualService": "vs", "DestinationRule": "dr"}


# --- Laden ---------------------------------------------------------------

def die(message) -> NoReturn:
    """Beendet mit Exit-Code 2, damit Ladefehler nicht wie ERROR-Befunde (1) aussehen."""
    print(message, file=sys.stderr)
    sys.exit(2)


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
        die(f"Fehler beim Lesen aus dem Cluster: {err}")
    return json.loads(out).get("items", [])


def load_from_files(paths, namespace):
    try:
        import yaml
    except ImportError:
        die("--file benoetigt PyYAML (pip install pyyaml)")
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
            # Eine Datei kann mehrere Dokumente (---) enthalten; nur VS/DR sind relevant.
            for doc in yaml.safe_load_all(fh):
                if not doc or doc.get("kind") not in KIND_SHORT:
                    continue
                # Manifeste ohne Namespace landen wie bei `kubectl apply -n` im Ziel-Namespace.
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
    """True, wenn es einen Hostnamen gibt, auf den sowohl a als auch b passen."""
    if a == b:
        return True
    for wild, other in ((a, b), (b, a)):
        if wild == "*":
            return True
        # *.foo passt nur auf Subdomains (a.foo, *.a.foo), nicht auf foo selbst.
        if wild.startswith("*.") and other.endswith(wild[1:]):
            return True
    return False


def wildcard_shadows(wild, host):
    """True, wenn der Wildcard-Host wild auch host abdeckt. Istio waehlt pro Host
    die spezifischste Konfiguration, die der Wildcard gilt fuer host dann nicht."""
    if not wild.startswith("*") or wild == host or not hosts_overlap(wild, host):
        return False
    # Ist host selbst die allgemeinere Wildcard, wird das Paar andersherum gemeldet.
    return not (host.startswith("*") and len(host) < len(wild))


def norm_gateway(gw, namespace):
    """Gateway-Referenzen auf <namespace>/<name> bringen, damit 'gw' und 'ns/gw' gleich sind."""
    if gw == "mesh" or "/" in gw:
        return gw
    return f"{namespace}/{gw}"


# --- Modell ---------------------------------------------------------------
# Die Kubernetes-Objekte werden beim Laden einmal normalisiert: Hosts als FQDN,
# Gateways als <namespace>/<name>. Die Pruefungen arbeiten danach nur noch auf
# diesen Klassen und muessen Namespaces nicht mehr beruecksichtigen.

@dataclass(eq=False)
class Route:
    label: str        # "'name'" oder "#index" fuer die Ausgabe
    matches: list     # HTTPMatchRequests, ODER-verknuepft; [{}] steht fuer "matcht alles"
    catch_all: bool   # die Route hat gar keine match-Liste


@dataclass(eq=False)
class VirtualService:
    ref: str          # "vs/<name>"
    hosts: list       # FQDNs
    gateways: list    # "<namespace>/<name>" oder "mesh"
    routes: list      # http-Routen als Route, in Auswertungsreihenfolge
    subset_refs: list  # (host, subset) aller Ziele, die ein Subset verwenden


@dataclass(eq=False)
class DestinationRule:
    ref: str          # "dr/<name>"
    host: str         # FQDN, leer wenn spec.host fehlt
    selector: str     # workloadSelector als sortiertes JSON, damit vergleichbar
    subsets: list
    has_policy: bool


@dataclass(frozen=True)
class Finding:
    severity: str
    code: str
    resources: tuple
    message: str


def finding(severity, code, resources, message):
    return Finding(severity, code, tuple(sorted(set(resources))), message)


def ref(item):
    return f"{KIND_SHORT[item['kind']]}/{item['metadata']['name']}"


def parse_route(route, idx, namespace):
    matches = []
    for match in route.get("match") or [{}]:
        if match.get("gateways"):
            match = dict(match, gateways=[norm_gateway(g, namespace) for g in match["gateways"]])
        matches.append(match)
    label = f"'{route['name']}'" if route.get("name") else f"#{idx}"
    return Route(label, matches, catch_all=not route.get("match"))


def vs_destinations(spec):
    """Liefert alle Ziele eines VS: Routen-Ziele sowie mirror/mirrors aus http, tcp und tls."""
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


def parse_vs(item):
    ns = item["metadata"]["namespace"]
    spec = item.get("spec", {})
    return VirtualService(
        ref=ref(item),
        hosts=[fqdn(h, ns) for h in spec.get("hosts") or []],
        # Ohne spec.gateways gilt ein VirtualService implizit nur fuer "mesh" (Sidecars).
        gateways=[norm_gateway(g, ns) for g in spec.get("gateways") or ["mesh"]],
        routes=[parse_route(r, i, ns) for i, r in enumerate(spec.get("http") or [])],
        subset_refs=[(fqdn(d["host"], ns), d["subset"]) for d in vs_destinations(spec)
                     if d.get("host") and d.get("subset")],
    )


def parse_dr(item):
    ns = item["metadata"]["namespace"]
    spec = item.get("spec", {})
    return DestinationRule(
        ref=ref(item),
        host=fqdn(spec["host"], ns) if spec.get("host") else "",
        selector=json.dumps(spec.get("workloadSelector") or {}, sort_keys=True),
        subsets=spec.get("subsets") or [],
        has_policy=bool(spec.get("trafficPolicy")),
    )


class Config:
    """Alle VS/DR eines Namespace plus die Gruppierungen, die die Pruefungen brauchen."""

    def __init__(self, namespace, items):
        self.namespace = namespace
        self.vss = [parse_vs(i) for i in items if i.get("kind") == "VirtualService"]
        self.drs = [parse_dr(i) for i in items if i.get("kind") == "DestinationRule"]

        # (gateway, host) -> VirtualServices; ein VS zaehlt pro Gruppe nur einmal.
        self.vs_by_binding = {}
        for vs in self.vss:
            for host in vs.hosts:
                for gw in vs.gateways:
                    group = self.vs_by_binding.setdefault((gw, host), [])
                    if vs not in group:
                        group.append(vs)

        # (host, workloadSelector) -> DestinationRules. Nur DRs mit gleichem Host
        # und gleichem workloadSelector konkurrieren miteinander.
        self.dr_by_host = {}
        for dr in self.drs:
            if dr.host:
                self.dr_by_host.setdefault((dr.host, dr.selector), []).append(dr)

    def drs_for_host(self, host):
        """Die DRs, die Istio fuer host verwendet: die mit exakt diesem Host, sonst
        die der spezifischsten (laengsten) passenden Wildcard."""
        exact = [dr for (h, _), group in self.dr_by_host.items() if h == host for dr in group]
        if exact:
            return exact
        wild = [(h, group) for (h, _), group in self.dr_by_host.items()
                if h.startswith("*") and hosts_overlap(h, host)]
        if not wild:
            return []
        longest = max(len(h) for h, _ in wild)
        return [dr for h, group in wild if len(h) == longest for dr in group]


# --- Hilfsfunktionen: Match-Ueberdeckung ---------------------------------
# covers(a, b) == True bedeutet: jeder Request, der b erfuellt, erfuellt auch a.
# Im Zweifel wird False geliefert (lieber einen Konflikt uebersehen als falsch melden).

def string_covers(a, b, ignore_case=False):
    """Vergleicht zwei Istio-StringMatches ({exact|prefix|regex: wert})."""
    # Keine Bedingung in a matcht alles; eine Bedingung nur in b schraenkt b ein.
    if not a:
        return True
    if not b:
        return False
    # Ein StringMatch hat genau einen Schluessel: exact, prefix oder regex.
    (ka, va), (kb, vb) = next(iter(a.items())), next(iter(b.items()))
    if ka == "regex":
        # Regex-Muster nicht kleinschreiben (\D wuerde zu \d), sondern per Flag vergleichen.
        if kb == "regex":
            return va == vb
        # Ob eine Regex eine andere Regex oder ein Prefix umfasst, ist nicht
        # sicher entscheidbar; nur gegen einen exakten Wert wird getestet.
        if kb == "exact":
            try:
                return re.fullmatch(va, vb, re.IGNORECASE if ignore_case else 0) is not None
            except re.error:
                return False
        return False
    if ignore_case:
        va, vb = va.lower(), vb.lower()
    if ka == "exact":
        return kb == "exact" and va == vb
    if ka == "prefix":
        return kb in ("exact", "prefix") and vb.startswith(va)
    return False


def uri_covers(ma, mb):
    a, b = ma.get("uri"), mb.get("uri")
    # prefix "/" ist gleichbedeutend mit "kein URI-Match".
    if not a or a == {"prefix": "/"}:
        return True
    ia, ib = bool(ma.get("ignoreUriCase")), bool(mb.get("ignoreUriCase"))
    # b ignoriert Gross/Klein, a nicht: b matcht z.B. /API, a aber nur /api.
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


def match_covers(ma, mb):
    """Ein HTTPMatchRequest ist eine UND-Verknuepfung: jede Bedingung von ma muss
    von mb mindestens gleich streng erfuellt werden."""
    if not uri_covers(ma, mb):
        return False
    for field in ("scheme", "method", "authority"):
        if not string_covers(ma.get(field), mb.get(field)):
            return False
    if not map_covers(ma.get("headers"), mb.get("headers")):
        return False
    if not map_covers(ma.get("queryParams"), mb.get("queryParams")):
        return False
    # withoutHeaders ist eine Negation; hier wird bewusst nur Gleichheit akzeptiert.
    for key, cond in (ma.get("withoutHeaders") or {}).items():
        if (mb.get("withoutHeaders") or {}).get(key) != cond:
            return False
    for field in ("port", "sourceNamespace"):
        if field in ma and ma[field] != mb.get(field):
            return False
    labels_b = mb.get("sourceLabels") or {}
    if any(labels_b.get(k) != v for k, v in (ma.get("sourceLabels") or {}).items()):
        return False
    # Ist ma auf Gateways beschraenkt, muss mb auf eine Teilmenge davon beschraenkt sein.
    if ma.get("gateways"):
        if not mb.get("gateways") or not set(mb["gateways"]) <= set(ma["gateways"]):
            return False
    return True


def route_covers(ra, rb):
    """True, wenn Route ra jeden Request abfaengt, den Route rb matchen wuerde."""
    # Mehrere Matches einer Route sind ODER-verknuepft: jeder Match von rb muss
    # von mindestens einem Match von ra abgedeckt sein.
    return all(any(match_covers(ma, mb) for ma in ra.matches) for mb in rb.matches)


def first_covering(routes, route):
    """Die erste Route aus routes, die alle Requests von route abfaengt, sonst None."""
    return next((r for r in routes if route_covers(r, route)), None)


# --- Pruefungen: VirtualServices ------------------------------------------
# Jede Pruefung liefert die Befunde genau eines Codes.

def shared_hosts(cfg):
    """(gateway, host, vss) fuer jeden Host, den mehrere VS am selben Gateway definieren."""
    for (gw, host), group in sorted(cfg.vs_by_binding.items(), key=lambda kv: kv[0]):
        if len(group) > 1:
            yield gw, host, group


def check_vs_host_dup(cfg):
    for gw, host, group in shared_hosts(cfg):
        # Am Sidecar wird nicht gemerged: ein doppelter Host ist immer ein Fehler.
        if gw == "mesh":
            yield finding(ERROR, "VS-HOST-DUP", [v.ref for v in group],
                          f"Host {host} ist am Sidecar (mesh) in {len(group)} VirtualServices "
                          f"definiert; Istio merged diese nicht, nur einer wird wirksam (IST0109)")


def check_vs_gw_merge(cfg):
    for gw, host, group in shared_hosts(cfg):
        if gw != "mesh":
            yield finding(WARN, "VS-GW-MERGE", [v.ref for v in group],
                          f"Host {host} an Gateway {gw} in {len(group)} VirtualServices; Routen "
                          f"werden gemerged, die Reihenfolge ist nicht garantiert")


def check_vs_gw_shadow(cfg):
    for gw, host, group in shared_hosts(cfg):
        if gw == "mesh":
            continue
        # Am Gateway werden die Routen aneinandergehaengt. Da die Reihenfolge der VS
        # nicht feststeht, wird jedes Paar in beiden Richtungen (a vor b, b vor a) geprueft.
        for a, b in permutations(group, 2):
            for rb in b.routes:
                ra = first_covering(a.routes, rb)
                if ra is not None:
                    yield finding(ERROR, "VS-GW-SHADOW", [a.ref, b.ref],
                                  f"Host {host} an Gateway {gw}: Route {rb.label} in {b.ref} "
                                  f"wird von Route {ra.label} in {a.ref} verdeckt, falls "
                                  f"{a.ref} im Merge zuerst kommt")


def check_vs_host_wildcard(cfg):
    # Istio waehlt pro Request den spezifischsten Host. Der Wildcard-VS gilt fuer
    # den konkreten Host dann gar nicht mehr (kein Merge).
    for (gw, wild), wild_vss in cfg.vs_by_binding.items():
        for (gw_b, host), host_vss in cfg.vs_by_binding.items():
            if gw_b != gw or not wildcard_shadows(wild, host):
                continue
            for a in wild_vss:
                for b in host_vss:
                    if a is not b:
                        yield finding(WARN, "VS-HOST-WILDCARD", [a.ref, b.ref],
                                      f"Wildcard-Host {wild} ({a.ref}) ueberlagert {host} "
                                      f"({b.ref}) an {gw}; fuer {host} gelten nur die Routen "
                                      f"von {b.ref}")


def check_vs_route_shadowed(cfg):
    # Istio wertet HTTP-Routen der Reihe nach aus, der erste Treffer gewinnt.
    # Eine Route ist tot, wenn eine fruehere Route alle ihre Requests abfaengt.
    for vs in cfg.vss:
        for j, route in enumerate(vs.routes):
            earlier = first_covering(vs.routes[:j], route)
            if earlier is None:
                continue
            reason = ("hat kein Match (Catch-All)" if earlier.catch_all
                      else "matcht bereits alle ihre Requests")
            yield finding(ERROR, "VS-ROUTE-SHADOWED", [vs.ref],
                          f"Route {route.label} ist nie erreichbar: fruehere Route "
                          f"{earlier.label} {reason}")


def check_vs_subset_missing(cfg):
    local_suffix = f".{cfg.namespace}.svc.cluster.local"
    for vs in cfg.vss:
        for host, subset in vs.subset_refs:
            drs = cfg.drs_for_host(host)
            if not drs:
                # Fuer Hosts ausserhalb des Namespace kann die DR woanders liegen,
                # daher wird nur bei lokalen Services ein Fehler gemeldet.
                if host.endswith(local_suffix):
                    yield finding(ERROR, "VS-SUBSET-MISSING", [vs.ref],
                                  f"Subset '{subset}' fuer {host} referenziert, aber es gibt "
                                  f"keine DestinationRule fuer diesen Host")
                continue
            defined = {s.get("name") for dr in drs for s in dr.subsets}
            if subset not in defined:
                yield finding(ERROR, "VS-SUBSET-MISSING", [vs.ref] + [dr.ref for dr in drs],
                              f"Subset '{subset}' fuer {host} ist in keiner DestinationRule "
                              f"definiert (vorhanden: {', '.join(sorted(defined)) or '-'})")


# --- Pruefungen: DestinationRules -----------------------------------------

def dr_groups(cfg):
    """(host, drs) je Host und workloadSelector, sortiert."""
    for (host, _), group in sorted(cfg.dr_by_host.items(), key=lambda kv: kv[0]):
        yield host, group


def check_dr_host_dup(cfg):
    for host, group in dr_groups(cfg):
        if len(group) > 1:
            yield finding(WARN, "DR-HOST-DUP", [d.ref for d in group],
                          f"Host {host} hat {len(group)} DestinationRules; Subsets werden "
                          f"gemerged, trafficPolicy kommt nur aus der aeltesten DR, die eine setzt")


def check_dr_policy_conflict(cfg):
    for host, group in dr_groups(cfg):
        with_policy = [d for d in group if d.has_policy]
        if len(with_policy) > 1:
            yield finding(ERROR, "DR-POLICY-CONFLICT", [d.ref for d in with_policy],
                          f"Host {host}: {len(with_policy)} DestinationRules setzen eine "
                          f"trafficPolicy, nur die der aeltesten wird angewendet")


def check_dr_subset_dup(cfg):
    for host, group in dr_groups(cfg):
        # Ueber alle DRs eines Hosts hinweg: wer hat welchen Subset-Namen zuerst definiert.
        owner = {}
        for dr in group:
            for subset in dr.subsets:
                name = subset.get("name")
                if name in owner:
                    yield finding(ERROR, "DR-SUBSET-DUP", [owner[name], dr.ref],
                                  f"Host {host}: Subset '{name}' ist mehrfach definiert, "
                                  f"nur die erste Definition greift")
                else:
                    owner[name] = dr.ref


def check_dr_subset_labels(cfg):
    for host, group in dr_groups(cfg):
        # Ueber alle DRs eines Hosts hinweg: welches Subset hat eine Label-Kombination zuerst.
        owner = {}
        for dr in group:
            for subset in dr.subsets:
                name = subset.get("name")
                labels = json.dumps(subset.get("labels") or {}, sort_keys=True)
                if labels in owner and owner[labels][1] != name:
                    other_ref, other_name = owner[labels]
                    yield finding(WARN, "DR-SUBSET-LABELS", [other_ref, dr.ref],
                                  f"Host {host}: Subsets '{other_name}' und '{name}' "
                                  f"selektieren dieselben Labels {labels}")
                else:
                    owner.setdefault(labels, (dr.ref, name))


def check_dr_host_wildcard(cfg):
    # Anders als Subsets werden Wildcard- und konkrete DRs nicht gemerged: fuer einen
    # Host gilt nur die spezifischste DR, die Einstellungen der Wildcard-DR entfallen.
    for (wild, sel_a), wild_drs in cfg.dr_by_host.items():
        for (host, sel_b), host_drs in cfg.dr_by_host.items():
            if sel_a != sel_b or not wildcard_shadows(wild, host):
                continue
            for a in wild_drs:
                for b in host_drs:
                    yield finding(WARN, "DR-HOST-WILDCARD", [a.ref, b.ref],
                                  f"Wildcard-DR {a.ref} ({wild}) gilt nicht fuer {host}: "
                                  f"{b.ref} ersetzt sie dort vollstaendig (kein Merge)")


CHECKS = [
    check_vs_host_dup,
    check_vs_host_wildcard,
    check_vs_gw_merge,
    check_vs_gw_shadow,
    check_vs_route_shadowed,
    check_vs_subset_missing,
    check_dr_host_dup,
    check_dr_policy_conflict,
    check_dr_subset_dup,
    check_dr_host_wildcard,
    check_dr_subset_labels,
]


def run_checks(cfg):
    found = [f for check in CHECKS for f in check(cfg)]
    # Doppelte Befunde verwerfen (z.B. Paare, die in beiden Richtungen gefunden werden),
    # dann Fehler zuerst, danach nach Code und Ressourcen sortieren.
    unique = dict.fromkeys(found)
    return sorted(unique, key=lambda f: (f.severity != ERROR, f.code, f.resources))


# --- Ausgabe --------------------------------------------------------------

def print_table(findings):
    if not findings:
        print("Keine Konflikte gefunden.")
        return
    for f in findings:
        print(f"[{f.severity:<5}] {f.code:<18} {', '.join(f.resources)}")
        print(f"        {f.message}")
    errors = sum(1 for f in findings if f.severity == ERROR)
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

    cfg = Config(args.namespace, items)
    result = run_checks(cfg)

    print(f"Namespace {args.namespace}: {len(cfg.vss)} VirtualServices, "
          f"{len(cfg.drs)} DestinationRules")
    print()
    print_table(result)

    # Exit-Code: 0 = ok/nur Warnungen, 1 = mindestens ein ERROR, 2 = Ladefehler (siehe die()).
    sys.exit(1 if any(f.severity == ERROR for f in result) else 0)


if __name__ == "__main__":
    main()
