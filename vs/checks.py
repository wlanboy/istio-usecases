"""Die Pruefungen. Jede Pruefung liefert die Befunde genau eines Codes."""

import json
from dataclasses import dataclass
from itertools import permutations

from matching import first_covering, routes_at
from model import wildcard_shadows

ERROR = "ERROR"
WARN = "WARN"


@dataclass(frozen=True)
class Finding:
    severity: str
    code: str
    resources: tuple
    message: str


def finding(severity, code, resources, message):
    return Finding(severity, code, tuple(sorted(set(resources))), message)


# --- Pruefungen: VirtualServices ------------------------------------------

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
            for rb in routes_at(b.routes, gw):
                ra = first_covering(routes_at(a.routes, gw), rb)
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
    for vs in cfg.vss:
        for host, subset in sorted({(d.host, d.subset) for d in vs.destinations if d.subset}):
            drs = cfg.drs_for_host(host)
            if not drs:
                # Fuer Hosts ausserhalb des Namespace kann die DR woanders liegen,
                # daher wird nur bei lokalen Services ein Fehler gemeldet.
                if cfg.is_local(host):
                    yield finding(ERROR, "VS-SUBSET-MISSING", [vs.ref],
                                  f"Subset '{subset}' fuer {host} referenziert, aber es gibt "
                                  f"keine DestinationRule fuer diesen Host")
                continue
            defined = {s.get("name") for dr in drs for s in dr.subsets}
            if subset not in defined:
                yield finding(ERROR, "VS-SUBSET-MISSING", [vs.ref] + [dr.ref for dr in drs],
                              f"Subset '{subset}' fuer {host} ist in keiner DestinationRule "
                              f"definiert (vorhanden: {', '.join(sorted(defined)) or '-'})")


def check_vs_dest_missing(cfg):
    # Ohne Service oder ServiceEntry gibt es fuer den Host keinen Cluster: 503 (NR).
    # Hosts ausserhalb des Namespace koennen anderswo definiert sein und werden
    # daher nicht geprueft.
    if not cfg.services_known:
        return
    for vs in cfg.vss:
        for host in sorted({d.host for d in vs.destinations}):
            if cfg.is_local(host) and not cfg.host_exists(host):
                yield finding(ERROR, "VS-DEST-MISSING", [vs.ref],
                              f"Ziel-Host {host} existiert nicht: weder Service noch "
                              f"ServiceEntry im Namespace (IST0101)")


def check_vs_dest_port(cfg):
    if not cfg.services_known:
        return
    for vs in cfg.vss:
        # Am Sidecar ergibt sich der Port ohne Angabe aus dem Listener, also dem
        # Service-Port selbst. Am Gateway ist das der Gateway-Port (z.B. 80), den
        # ein Service mit mehreren Ports meist nicht hat.
        at_gateway = any(gw != "mesh" for gw in vs.gateways)
        for d in sorted(set(vs.destinations), key=lambda d: (d.host, d.port or 0)):
            svc = cfg.services.get(d.host)
            if svc is None:
                continue
            ports = ", ".join(map(str, svc.ports)) or "-"
            if d.port is None:
                if at_gateway and len(svc.ports) > 1:
                    yield finding(ERROR, "VS-DEST-PORT", [vs.ref, svc.ref],
                                  f"Ziel {d.host} hat mehrere Ports ({ports}), aber "
                                  f"destination.port fehlt; am Gateway ist das Ziel "
                                  f"mehrdeutig (IST0112)")
            elif d.port not in svc.ports:
                yield finding(ERROR, "VS-DEST-PORT", [vs.ref, svc.ref],
                              f"Ziel {d.host}: Port {d.port} gibt es im Service nicht "
                              f"(vorhanden: {ports})")


def check_vs_gw_missing(cfg):
    # Gemeldet wird nur, wenn die Gateways des Ziel-Namespace gelesen werden konnten.
    for vs in cfg.vss:
        for gw in vs.gateways:
            if gw == "mesh" or gw in cfg.gateways:
                continue
            if gw.split("/", 1)[0] in cfg.gateway_namespaces:
                yield finding(ERROR, "VS-GW-MISSING", [vs.ref],
                              f"Gateway {gw} existiert nicht; der VirtualService wird "
                              f"dort nicht angewendet (IST0101)")


def check_vs_gw_host(cfg):
    # Ein Gateway nimmt nur Hosts an, die ein Server in servers[].hosts freigibt.
    # Alle anderen Hosts des VS ignoriert es an diesem Gateway ohne Fehlermeldung.
    for vs in cfg.vss:
        for key in vs.gateways:
            gw = cfg.gateways.get(key)
            if gw is None:
                continue
            missing = [h for h in vs.hosts if not gw.admits(h, vs.namespace)]
            if missing and len(missing) == len(vs.hosts):
                yield finding(ERROR, "VS-GW-HOST", [vs.ref, gw.ref],
                              f"Keiner der Hosts ({', '.join(missing)}) ist in Gateway {key} "
                              f"freigegeben; der VirtualService wird dort ignoriert (IST0132)")
            elif "mesh" not in vs.gateways:
                # Mit mesh sind Hosts, die nur fuer die Sidecars gedacht sind, normal.
                for host in missing:
                    yield finding(WARN, "VS-GW-HOST", [vs.ref, gw.ref],
                                  f"Host {host} ist in Gateway {key} nicht freigegeben und "
                                  f"wird dort ignoriert (IST0132)")


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


def labels_match(labels, selector):
    return all(labels.get(k) == v for k, v in selector.items())


def check_dr_subset_nopods(cfg):
    # Ein Pod gehoert zum Subset, wenn er den Selector des Service UND die
    # Subset-Labels traegt. Ohne solche Pods hat der Subset-Cluster keine
    # Endpunkte: Requests darauf enden mit 503 (UH).
    if not (cfg.services_known and cfg.pods_known):
        return
    users = {}
    for vs in cfg.vss:
        for d in vs.destinations:
            if d.subset:
                users.setdefault((d.host, d.subset), set()).add(vs.ref)
    for dr in cfg.drs:
        svc = cfg.services.get(dr.host)
        # Ohne Selector werden die Endpoints manuell gepflegt, Pods sagen dann nichts aus.
        if svc is None or not svc.selector:
            continue
        for subset in dr.subsets:
            name, labels = subset.get("name"), subset.get("labels") or {}
            if any(labels_match(p, svc.selector) and labels_match(p, labels)
                   for p in cfg.pod_labels):
                continue
            label_text = json.dumps(labels, sort_keys=True)
            vss = sorted(users.get((dr.host, name), ()))
            if vss:
                yield finding(ERROR, "DR-SUBSET-NOPODS", [dr.ref, svc.ref] + vss,
                              f"Host {dr.host}: Subset '{name}' {label_text} selektiert keinen "
                              f"Pod; Requests von {', '.join(vss)} darauf enden mit 503")
            else:
                yield finding(WARN, "DR-SUBSET-NOPODS", [dr.ref, svc.ref],
                              f"Host {dr.host}: Subset '{name}' {label_text} selektiert keinen "
                              f"Pod (wird von keinem VirtualService verwendet)")


CHECKS = [
    check_vs_host_dup,
    check_vs_host_wildcard,
    check_vs_gw_merge,
    check_vs_gw_shadow,
    check_vs_route_shadowed,
    check_vs_subset_missing,
    check_vs_dest_missing,
    check_vs_dest_port,
    check_vs_gw_missing,
    check_vs_gw_host,
    check_dr_host_dup,
    check_dr_policy_conflict,
    check_dr_subset_dup,
    check_dr_host_wildcard,
    check_dr_subset_labels,
    check_dr_subset_nopods,
]


def run_checks(cfg):
    found = [f for check in CHECKS for f in check(cfg)]
    # Doppelte Befunde verwerfen (z.B. Paare, die in beiden Richtungen gefunden werden),
    # dann Fehler zuerst, danach nach Code und Ressourcen sortieren.
    unique = dict.fromkeys(found)
    return sorted(unique, key=lambda f: (f.severity != ERROR, f.code, f.resources))
