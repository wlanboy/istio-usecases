"""Match-Ueberdeckung von HTTP-Routen."""

import re

from model import Route


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
    for attr in ("scheme", "method", "authority"):
        if not string_covers(ma.get(attr), mb.get(attr)):
            return False
    if not map_covers(ma.get("headers"), mb.get("headers")):
        return False
    if not map_covers(ma.get("queryParams"), mb.get("queryParams")):
        return False
    # withoutHeaders ist eine Negation; hier wird bewusst nur Gleichheit akzeptiert.
    for key, cond in (ma.get("withoutHeaders") or {}).items():
        if (mb.get("withoutHeaders") or {}).get(key) != cond:
            return False
    for attr in ("port", "sourceNamespace"):
        if attr in ma and ma[attr] != mb.get(attr):
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


def routes_at(routes, gw):
    """Die Routen, wie sie an Gateway gw gelten: Matches, die per match.gateways auf
    andere Gateways beschraenkt sind, entfallen; Routen ohne verbleibenden Match
    gelten dort gar nicht."""
    result = []
    for route in routes:
        matches = [{k: v for k, v in m.items() if k != "gateways"} for m in route.matches
                   if not m.get("gateways") or gw in m["gateways"]]
        if matches:
            result.append(Route(route.label, matches, route.catch_all))
    return result


def first_covering(routes, route):
    """Die erste Route aus routes, die alle Requests von route abfaengt, sonst None."""
    return next((r for r in routes if route_covers(r, route)), None)
