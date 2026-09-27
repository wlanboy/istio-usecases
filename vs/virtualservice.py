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
  VS-DEST-MISSING      Ziel-Host eines VS existiert nicht (weder Service noch ServiceEntry)
  VS-DEST-PORT         Ziel-Port fehlt (Gateway, Service mit mehreren Ports) oder existiert nicht
  VS-GW-MISSING        VS verweist auf ein Gateway, das es nicht gibt
  VS-GW-HOST           Host eines VS ist in servers.hosts des Gateways nicht freigegeben
  DR-HOST-DUP          mehrere DestinationRules fuer denselben Host (werden gemerged)
  DR-POLICY-CONFLICT   mehrere dieser DestinationRules setzen eine trafficPolicy (nur die aelteste greift)
  DR-SUBSET-DUP        derselbe Subset-Name ist fuer einen Host mehrfach definiert
  DR-HOST-WILDCARD     Wildcard-DestinationRule wird fuer einen Host von einer konkreten DR ersetzt
  DR-SUBSET-LABELS     zwei Subsets eines Hosts selektieren exakt dieselben Labels
  DR-SUBSET-NOPODS     Subset selektiert keinen Pod des Service

Nutzt nur die Python-Standardbibliothek und ruft `kubectl` auf. Mit --file
werden stattdessen lokale YAML-Manifeste gelesen (benoetigt PyYAML).

Verwendung:
    python3 virtualservice.py <namespace> [--context CONTEXT]
    python3 virtualservice.py <namespace> --file manifests/
"""

import argparse
import sys

from checks import ERROR, run_checks
from loader import hint, load_from_cluster, load_from_files
from model import Config


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


def report_skipped(cfg):
    """Nennt Pruefungen, die mangels geladener Objekte nicht laufen konnten."""
    if not cfg.services_known:
        hint("keine Services/ServiceEntries geladen: VS-DEST-MISSING, VS-DEST-PORT "
             "und DR-SUBSET-NOPODS entfallen")
    elif not cfg.pods_known:
        hint("keine Pods bzw. Workloads geladen: DR-SUBSET-NOPODS entfaellt")
    unknown = {gw.split("/", 1)[0] for vs in cfg.vss for gw in vs.gateways if gw != "mesh"}
    for ns in sorted(unknown - cfg.gateway_namespaces):
        hint(f"keine Gateways aus Namespace {ns} geladen: Verweise dorthin werden "
             f"nicht auf VS-GW-MISSING/VS-GW-HOST geprueft")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("namespace", help="zu pruefender Namespace")
    parser.add_argument("--context", help="kubectl Context (optional)")
    parser.add_argument("--file", "-f", action="append",
                        help="YAML-Datei oder -Verzeichnis statt Cluster lesen (mehrfach moeglich)")
    args = parser.parse_args()

    if args.file:
        source = load_from_files(args.file, args.namespace)
    else:
        source = load_from_cluster(args.namespace, args.context)

    cfg = Config(args.namespace, source)
    report_skipped(cfg)
    result = run_checks(cfg)

    print(f"Namespace {args.namespace}: {len(cfg.vss)} VirtualServices, "
          f"{len(cfg.drs)} DestinationRules")
    print()
    print_table(result)

    # Exit-Code: 0 = ok/nur Warnungen, 1 = mindestens ein ERROR, 2 = Ladefehler (siehe die()).
    sys.exit(1 if any(f.severity == ERROR for f in result) else 0)


if __name__ == "__main__":
    main()
