# VirtualService / DestinationRule Konfliktprüfung

`virtualservice.py` listet für einen Namespace alle `VirtualService`s und
`DestinationRule`s, die sich überlagern oder widersprechen. Istio nimmt solche
Konfigurationen meist ohne Fehler an und wendet dann nur einen Teil davon an.
Der Validierungs-Webhook warnt nur bei einfachen Fällen innerhalb eines
einzelnen `VirtualService`.

Das Tool nutzt nur die Python-Standardbibliothek und ruft `kubectl` auf. Für
`--file` wird zusätzlich PyYAML benötigt.

## Geprüfte Konflikte

| Code | Schwere | Bedeutung |
|------|---------|-----------|
| `VS-HOST-DUP` | ERROR | Mehrere VS für denselben Host am Sidecar (`mesh`). Istio merged sie nicht, nur einer wirkt (IST0109). Kurzname und FQDN (`svc` / `svc.ns.svc.cluster.local`) zählen als derselbe Host. |
| `VS-HOST-WILDCARD` | WARN | Ein Wildcard-Host (`*.foo`) überlagert einen konkreten Host eines anderen VS. Für den konkreten Host gelten nur dessen Routen. |
| `VS-GW-MERGE` | WARN | Mehrere VS für denselben Host am selben Gateway. Istio merged die Routen, die Reihenfolge ist nicht garantiert. |
| `VS-GW-SHADOW` | ERROR | Beim Gateway-Merge kann eine Route eines VS eine Route des anderen verdecken, z. B. ein Catch-All. |
| `VS-ROUTE-SHADOWED` | ERROR | Eine Route im VS ist nie erreichbar, weil eine frühere Route alle ihre Requests abfängt (Catch-All, `prefix /`, allgemeinerer Prefix, Regex, Teilmenge der Header-Bedingungen, identischer Match). |
| `VS-SUBSET-MISSING` | ERROR | Der VS verweist auf ein Subset, das in keiner DR des Hosts definiert ist, oder für den Host gibt es gar keine DR. Das führt zu HTTP 503 (`NR`). |
| `DR-HOST-DUP` | WARN | Mehrere DR für denselben Host (und denselben `workloadSelector`). Subsets werden gemerged. |
| `DR-POLICY-CONFLICT` | ERROR | Mehrere dieser DR setzen eine `trafficPolicy`. Nur die älteste wird angewendet. |
| `DR-SUBSET-DUP` | ERROR | Ein Subset-Name ist für einen Host mehrfach definiert. Nur die erste Definition greift. |
| `DR-HOST-WILDCARD` | WARN | Eine Wildcard-DR gilt für einen Host nicht, weil eine konkrete DR sie dort vollständig ersetzt (kein Merge). |
| `DR-SUBSET-LABELS` | WARN | Zwei Subsets eines Hosts selektieren exakt dieselben Labels. |

Grenzen:
- Geprüft wird nur der angegebene Namespace. VS/DR aus anderen Namespaces
  (z. B. eine DR in `istio-system`) werden nicht berücksichtigt. Fehlt die DR
  für ein Subset und liegt der Host in einem anderen Namespace, meldet das Tool
  dazu nichts.
- Die Routenanalyse umfasst nur `http`-Routen. Bei Regex erkennt sie nur
  identische Ausdrücke oder einen `exact`-Wert, der auf den Ausdruck passt. Im
  Zweifel meldet sie nichts, statt falsch zu melden.
- `exportTo` wird nicht ausgewertet.

## Verwendung

```bash
python3 virtualservice.py <namespace>                     # aus dem Cluster lesen
python3 virtualservice.py <namespace> --context kind-local
python3 virtualservice.py <namespace> --file manifests/   # lokale YAML-Dateien statt Cluster
python3 virtualservice.py <namespace> --format json       # table (Standard) | tsv | json
```

Intern ausgeführter Befehl (ohne `--file`):

```bash
kubectl get virtualservices.networking.istio.io,destinationrules.networking.istio.io -n <namespace> -o json
```

Exit-Code: `1`, wenn mindestens ein `ERROR` gefunden wurde, sonst `0`. Damit
lässt sich das Tool direkt in CI einsetzen.

Beispielausgabe:

```
[ERROR] VS-ROUTE-SHADOWED  vs/route-catchall
        Route 'debug' ist nie erreichbar: fruehere Route 'default' hat kein Match (Catch-All)
[WARN ] VS-GW-MERGE        vs/shop-cart, vs/shop-checkout
        Host shop.vstest.local an Gateway vstest/vstest-gw in 2 VirtualServices; Routen werden gemerged, die Reihenfolge ist nicht garantiert
```

## Testfälle (Namespace `vstest`)

Jede Datei in [`manifests/`](manifests/) deckt ein Fehlerbild mit eigenen
Hostnamen ab, damit sich die Fälle nicht gegenseitig beeinflussen. Die
Kommentare in den Dateien beschreiben den jeweiligen Konflikt.

| Datei | Erwartete Befunde |
|-------|-------------------|
| `00-namespace.yaml`, `01-gateway.yaml` | – (Namespace `vstest`, Gateway `vstest-gw`) |
| `10-ok.yaml` | keine (Positivfall: korrekte Reihenfolge, alle Subsets definiert) |
| `20-vs-dup-host.yaml` | `VS-HOST-DUP` (Kurzname + FQDN) |
| `21-vs-wildcard.yaml` | `VS-HOST-WILDCARD` |
| `22-vs-gw-merge.yaml` | `VS-GW-MERGE` |
| `23-vs-gw-shadow.yaml` | `VS-GW-MERGE`, `VS-GW-SHADOW` |
| `30-vs-route-catchall.yaml` | `VS-ROUTE-SHADOWED` (Catch-All zuerst) |
| `31-vs-route-prefix.yaml` | `VS-ROUTE-SHADOWED` ×3 (Prefix, Exact unter Prefix, `prefix /`) |
| `32-vs-route-match.yaml` | `VS-ROUTE-SHADOWED` ×3 (Regex, Header-Teilmenge, identischer Match) |
| `40-vs-subset-missing.yaml` | `VS-SUBSET-MISSING` ×2 (Subset fehlt, DR fehlt) |
| `50-dr-dup-host.yaml` | `DR-HOST-DUP` (Kurzname + FQDN) |
| `51-dr-policy-conflict.yaml` | `DR-HOST-DUP`, `DR-POLICY-CONFLICT` |
| `52-dr-subset-dup.yaml` | `DR-HOST-DUP`, `DR-SUBSET-DUP` |
| `53-dr-wildcard.yaml` | `DR-HOST-WILDCARD` |
| `54-dr-subset-labels.yaml` | `DR-SUBSET-LABELS` |

Die exakte Liste steht in [`test/expected.tsv`](test/expected.tsv).

### Installation

```bash
./install.sh
```

Führt intern aus:

```bash
kubectl apply -f manifests/
```

Beim Apply warnt der Istio-Webhook bereits bei einigen Route-Fällen (30, 31 und
dem identischen Match in 32). Die Überlagerungen zwischen mehreren Ressourcen
sowie der Regex- und der Header-Fall in 32 werden vom Webhook nicht erkannt.

### Test

```bash
./test.sh              # gegen den Cluster (Namespace vstest)
./test.sh --offline    # direkt gegen manifests/, kein Cluster nötig
```

Führt intern aus:

```bash
python3 virtualservice.py vstest --format tsv | cut -f2,3 | sort > "$TMP/actual.tsv"
# bzw. offline: python3 virtualservice.py vstest --file manifests/ --format tsv | ...
grep -v '^#' test/expected.tsv | sort > "$TMP/expected.tsv"
diff -u "$TMP/expected.tsv" "$TMP/actual.tsv"
```

Der Test schlägt fehl, wenn ein erwarteter Befund fehlt oder ein zusätzlicher
auftaucht (z. B. ein Fehlalarm für `10-ok.yaml`).

### Aufräumen

```bash
./uninstall.sh
```

Führt intern aus:

```bash
kubectl delete namespace vstest --ignore-not-found
```
