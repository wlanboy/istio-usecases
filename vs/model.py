"""Normalisiertes Modell der VirtualServices, DestinationRules und der Objekte,
auf die sie verweisen, sowie Hilfsfunktionen fuer Hosts und Gateways."""

import json
from dataclasses import dataclass

KIND_SHORT = {"VirtualService": "vs", "DestinationRule": "dr", "Gateway": "gw",
              "ServiceEntry": "se", "Service": "svc"}


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


@dataclass(frozen=True)
class Destination:
    host: str         # FQDN
    subset: str       # leer ohne Subset
    port: object      # Portnummer oder None


@dataclass(eq=False)
class VirtualService:
    ref: str          # "vs/<name>"
    namespace: str
    hosts: list       # FQDNs
    gateways: list    # "<namespace>/<name>" oder "mesh"
    routes: list      # http-Routen als Route, in Auswertungsreihenfolge
    destinations: list  # alle Ziele (Destination) aus http, tcp und tls inkl. mirror


@dataclass(eq=False)
class DestinationRule:
    ref: str          # "dr/<name>"
    host: str         # FQDN, leer wenn spec.host fehlt
    selector: str     # workloadSelector als sortiertes JSON, damit vergleichbar
    subsets: list
    has_policy: bool


@dataclass(eq=False)
class Service:
    ref: str          # "svc/<name>"
    host: str         # FQDN
    selector: dict    # leer bei Services ohne Selector (Endpoints manuell gepflegt)
    ports: list       # Portnummern


@dataclass(eq=False)
class Gateway:
    ref: str          # "gw/<name>", aus anderen Namespaces "gw/<namespace>/<name>"
    namespace: str
    hosts: list       # (Namespace-Teil, Host) aus servers[].hosts

    def admits(self, host, vs_namespace):
        """True, wenn ein Server des Gateways host fuer VirtualServices aus
        vs_namespace freigibt. "ns/host" beschraenkt auf VS aus ns, "." auf den
        Namespace des Gateways, "*" oder ohne Praefix gilt fuer alle."""
        for ns, gw_host in self.hosts:
            allowed = ns in ("*", vs_namespace) or (ns == "." and vs_namespace == self.namespace)
            if allowed and hosts_overlap(gw_host, host):
                return True
        return False


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
        namespace=ns,
        hosts=[fqdn(h, ns) for h in spec.get("hosts") or []],
        # Ohne spec.gateways gilt ein VirtualService implizit nur fuer "mesh" (Sidecars).
        gateways=[norm_gateway(g, ns) for g in spec.get("gateways") or ["mesh"]],
        routes=[parse_route(r, i, ns) for i, r in enumerate(spec.get("http") or [])],
        destinations=[Destination(fqdn(d["host"], ns), d.get("subset") or "",
                                  (d.get("port") or {}).get("number"))
                      for d in vs_destinations(spec) if d.get("host")],
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


def parse_service(item):
    ns = item["metadata"]["namespace"]
    spec = item.get("spec", {})
    return Service(
        ref=ref(item),
        host=fqdn(item["metadata"]["name"], ns),
        selector=spec.get("selector") or {},
        ports=[p["port"] for p in spec.get("ports") or [] if p.get("port")],
    )


def parse_gateway(item, namespace):
    ns = item["metadata"]["namespace"]
    hosts = []
    for server in item.get("spec", {}).get("servers") or []:
        for host in server.get("hosts") or []:
            hosts.append(tuple(host.split("/", 1)) if "/" in host else ("*", host))
    name = item["metadata"]["name"]
    return Gateway(ref=f"gw/{name}" if ns == namespace else f"gw/{ns}/{name}",
                   namespace=ns, hosts=hosts)


def by_kind(items, kind):
    return [i for i in items if i.get("kind") == kind]


class Config:
    """Alle VS/DR eines Namespace, die Objekte, auf die sie verweisen, und die
    Gruppierungen, die die Pruefungen brauchen."""

    def __init__(self, namespace, source):
        self.namespace = namespace
        self.local_suffix = f".{namespace}.svc.cluster.local"
        items = source.items
        self.vss = [parse_vs(i) for i in by_kind(items, "VirtualService")]
        self.drs = [parse_dr(i) for i in by_kind(items, "DestinationRule")]

        self.services_known = source.services_known
        self.pods_known = source.pods_known
        self.gateway_namespaces = source.gateway_namespaces
        # FQDN -> Service, "<namespace>/<name>" -> Gateway
        self.services = {s.host: s for s in map(parse_service, by_kind(items, "Service"))}
        self.se_hosts = [fqdn(h, i["metadata"]["namespace"])
                         for i in by_kind(items, "ServiceEntry")
                         for h in i.get("spec", {}).get("hosts") or []]
        self.gateways = {f"{i['metadata']['namespace']}/{i['metadata']['name']}":
                         parse_gateway(i, namespace) for i in by_kind(items, "Gateway")}
        self.pod_labels = [i["metadata"].get("labels") or {} for i in by_kind(items, "Pod")]

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

    def is_local(self, host):
        return host.endswith(self.local_suffix)

    def host_exists(self, host):
        return host in self.services or any(hosts_overlap(h, host) for h in self.se_hosts)

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

