# VirtualService / DestinationRule Konfliktprüfung

`virtualservice.py` listet für einen Namespace alle `VirtualService`s und
`DestinationRule`s, die sich überlagern oder widersprechen oder auf etwas
verweisen, das es nicht gibt (Service, Port, Gateway, Pods eines Subsets).
Istio nimmt solche Konfigurationen meist ohne Fehler an und wendet dann nur
einen Teil davon an oder antwortet mit HTTP 503. Der Validierungs-Webhook warnt
nur bei einfachen Fällen innerhalb eines einzelnen `VirtualService`.

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
| `VS-DEST-MISSING` | ERROR | Ein Ziel-Host (`route`, `mirror`, `mirrors`) im Namespace hat weder Service noch ServiceEntry. Das führt zu HTTP 503 (`NR`), entspricht IST0101. |
| `VS-DEST-PORT` | ERROR | Ein VS an einem Gateway gibt für einen Service mit mehreren Ports keinen `destination.port` an (IST0112), oder der angegebene Port existiert im Service nicht. Am Sidecar ist ein fehlender Port unkritisch, dort gilt der Port des Listeners. |
| `VS-GW-MISSING` | ERROR | Der VS verweist in `spec.gateways` auf ein Gateway, das es nicht gibt (IST0101). |
| `VS-GW-HOST` | ERROR / WARN | Ein Host des VS ist in `servers[].hosts` des Gateways nicht freigegeben (auch nicht per `<namespace>/host`) und wird dort ignoriert (IST0132). ERROR, wenn keiner der Hosts passt und der VS am Gateway damit gar nicht greift. WARN für einzelne Hosts, außer der VS hängt zusätzlich an `mesh` (dann sind Hosts nur für Sidecars üblich). |
| `DR-HOST-DUP` | WARN | Mehrere DR für denselben Host (und denselben `workloadSelector`). Subsets werden gemerged, die `trafficPolicy` kommt aus der ältesten DR, die eine setzt. |
| `DR-POLICY-CONFLICT` | ERROR | Mehrere dieser DR setzen eine `trafficPolicy`. Nur die Policy der ältesten wird angewendet. |
| `DR-SUBSET-DUP` | ERROR | Ein Subset-Name ist für einen Host mehrfach definiert. Nur die erste Definition greift. |
| `DR-HOST-WILDCARD` | WARN | Eine Wildcard-DR gilt für einen Host nicht, weil eine konkrete DR sie dort vollständig ersetzt (kein Merge). |
| `DR-SUBSET-LABELS` | WARN | Zwei Subsets eines Hosts selektieren exakt dieselben Labels. |
| `DR-SUBSET-NOPODS` | ERROR / WARN | Kein Pod trägt den Selector des Service und die Labels des Subsets. ERROR, wenn ein VS das Subset verwendet (HTTP 503, `UH`), sonst WARN. |

Grenzen:
- Geprüft wird nur der angegebene Namespace. VS/DR aus anderen Namespaces
  (z. B. eine DR in `istio-system`) werden nicht berücksichtigt. Fehlt die DR
  für ein Subset und liegt der Host in einem anderen Namespace, meldet das Tool
  dazu nichts.
- Umgekehrt meldet `VS-SUBSET-MISSING` einen Fehler, obwohl das Subset
  existiert, wenn die DR für einen Host dieses Namespace in einem anderen
  Namespace liegt, z. B. im Root-Namespace `istio-system` oder im Namespace
  des aufrufenden Clients.
- `VS-DEST-MISSING`, `VS-DEST-PORT` und `DR-SUBSET-NOPODS` prüfen nur Hosts
  dieses Namespace (`<svc>.<namespace>.svc.cluster.local`). Ziele in anderen
  Namespaces und externe Hosts, deren ServiceEntry anderswo liegen kann, werden
  nicht gemeldet. `DR-SUBSET-NOPODS` prüft nur Services mit Selector und
  berücksichtigt keine WorkloadEntries.
- Gateways werden aus dem Namespace und aus jedem Namespace gelesen, auf den
  ein VS verweist (`istio-system/ingress`). Fehlen dafür die Rechte, entfallen
  `VS-GW-MISSING` und `VS-GW-HOST` für diese Verweise.
- Die Routenanalyse umfasst nur `http`-Routen. Bei Regex erkennt sie nur
  identische Ausdrücke oder einen `exact`-Wert, der auf den Ausdruck passt. Im
  Zweifel meldet sie nichts, statt falsch zu melden.
- `exportTo` wird nicht ausgewertet.

## Verwendung

```bash
python3 virtualservice.py <namespace>                     # aus dem Cluster lesen
python3 virtualservice.py <namespace> --context kind-local
python3 virtualservice.py <namespace> --file manifests/   # lokale YAML-Dateien statt Cluster
```

PyYAML wird nur für `--file` gebraucht; beim Lesen aus dem Cluster reicht die
Standardbibliothek.

Der Code ist auf mehrere Module verteilt, die neben `virtualservice.py` liegen
müssen:

| Modul | Inhalt |
|---|---|
| `virtualservice.py` | Kommandozeile und Ausgabe |
| `loader.py` | Laden aus dem Cluster (`kubectl`) oder aus YAML-Dateien |
| `model.py` | normalisiertes Modell (Hosts als FQDN, Gateways als `<namespace>/<name>`) |
| `matching.py` | Match-Überdeckung von HTTP-Routen |
| `checks.py` | die Prüfungen (`CHECKS`) |

Für den Einsatz als einzelne Datei (z.B. auf einem Jump-Host) lässt sich
daraus ein Zipapp bauen:

```bash
python3 -m zipapp . -m virtualservice:main -o virtualservice.pyz
python3 virtualservice.pyz <namespace>
```

Intern ausgeführte Befehle (ohne `--file`):

```bash
kubectl get virtualservices.networking.istio.io,destinationrules.networking.istio.io,gateways.networking.istio.io,serviceentries.networking.istio.io -n <namespace> -o json
kubectl get services -n <namespace> -o json
kubectl get pods -n <namespace> -o json
# fuer jeden anderen Namespace, auf den ein VS per <ns>/<gateway> verweist:
kubectl get gateways.networking.istio.io -n <ns> -o json
# nur wenn keine VS/DR gefunden wurden, um einen Tippfehler zu erkennen:
kubectl get namespace <namespace> -o name
```

Nur der erste Befehl muss gelingen. Sind Services, Pods oder fremde Gateways
nicht lesbar (fehlende RBAC-Rechte), gibt das Tool einen `Hinweis:` auf stderr
aus und lässt nur die davon abhängigen Prüfungen weg.

Mit `--file` werden neben VS/DR auch `Gateway`, `ServiceEntry` und `Service`
gelesen, für `DR-SUBSET-NOPODS` außerdem die Pod-Labels aus `Pod`,
`Deployment`, `StatefulSet`, `DaemonSet`, `ReplicaSet`, `Job` und `CronJob`.
`kind: List` (Ausgabe von `kubectl get -o yaml`) wird aufgelöst. Eine Prüfung
auf fehlende Objekte läuft nur, wenn die Dateien Objekte dieser Art enthalten.
Ohne Services in den Dateien entfallen also `VS-DEST-MISSING`, `VS-DEST-PORT`
und `DR-SUBSET-NOPODS`, statt jedes Ziel als fehlend zu melden. Enthalten die
Dateien Services, aber keine Workloads, gelten die Services wie im Cluster als
ohne Pods. Gateways zählen je Namespace.

Exit-Code: `1`, wenn mindestens ein `ERROR` gefunden wurde, sonst `0`. `2` bei
einem Ladefehler: Cluster nicht erreichbar, Namespace existiert nicht, PyYAML
fehlt, Datei fehlt oder ist kein gültiges YAML, Ressource ohne
`metadata.name`, oder die Dateien enthalten keine VS/DR für den Namespace.
Damit lässt sich das Tool direkt in CI einsetzen: ein falsch geschriebener
Namespace führt nicht zu einem grünen Lauf.

Beispielausgabe:

```
[ERROR] VS-ROUTE-SHADOWED  vs/route-catchall
        Route 'debug' ist nie erreichbar: fruehere Route 'default' hat kein Match (Catch-All)
[WARN ] VS-GW-MERGE        vs/shop-cart, vs/shop-checkout
        Host shop.vstest.local an Gateway vstest/vstest-gw in 2 VirtualServices; Routen werden gemerged, die Reihenfolge ist nicht garantiert
```

## Testfälle

Jede Datei in [`manifests/`](manifests/) deckt ein Fehlerbild mit eigenen
Hostnamen ab, damit sich die Fälle nicht gegenseitig beeinflussen. Die
Kommentare in den Dateien beschreiben den jeweiligen Konflikt.

Jede Datei enthält auch die Services für ihre Ziele, sonst würde
`VS-DEST-MISSING` in jedem Testfall anschlagen. Deployments gibt es keine.
Deshalb meldet `DR-SUBSET-NOPODS` jedes Subset eines Service, auch in
`10-ok.yaml` und `40-vs-subset-missing.yaml`.

Die Manifeste enthalten keinen Namespace. Er wird beim Apply mit `-n`
angegeben, so lässt sich auf einem anderen Cluster auch ein bestehender
Namespace nutzen. Deshalb verwenden die Testfälle nur Kurznamen: ein FQDN
(`svc.<ns>.svc.cluster.local`) oder eine Gateway-Referenz `<ns>/<gw>` würde den
Namespace wieder festlegen.

| Datei | Erwartete Befunde |
|-------|-------------------|
| `01-gateway.yaml` | – (Gateway `vstest-gw`) |
| `10-ok.yaml` | nur `DR-SUBSET-NOPODS` ×2 (Positivfall: korrekte Reihenfolge, alle Subsets definiert, aber keine Pods) |
| `20-vs-dup-host.yaml` | `VS-HOST-DUP` |
| `21-vs-wildcard.yaml` | `VS-HOST-WILDCARD` |
| `22-vs-gw-merge.yaml` | `VS-GW-MERGE` |
| `23-vs-gw-shadow.yaml` | `VS-GW-MERGE`, `VS-GW-SHADOW` |
| `24-vs-gw-route-gateways.yaml` | nur `VS-GW-MERGE`, kein `VS-GW-SHADOW` (Route per `match.gateways` auf ein anderes Gateway beschränkt) |
| `30-vs-route-catchall.yaml` | `VS-ROUTE-SHADOWED` (Catch-All zuerst) |
| `31-vs-route-prefix.yaml` | `VS-ROUTE-SHADOWED` ×3 (Prefix, Exact unter Prefix, `prefix /`) |
| `32-vs-route-match.yaml` | `VS-ROUTE-SHADOWED` ×3 (Regex, Header-Teilmenge, identischer Match) |
| `40-vs-subset-missing.yaml` | `VS-SUBSET-MISSING` ×2 (Subset fehlt, DR fehlt), `DR-SUBSET-NOPODS` ×2 (v1, v2 ohne Pods) |
| `50-dr-dup-host.yaml` | `DR-HOST-DUP` |
| `51-dr-policy-conflict.yaml` | `DR-HOST-DUP`, `DR-POLICY-CONFLICT` |
| `52-dr-subset-dup.yaml` | `DR-HOST-DUP`, `DR-SUBSET-DUP` |
| `53-dr-wildcard.yaml` | `DR-HOST-WILDCARD` |
| `54-dr-subset-labels.yaml` | `DR-SUBSET-LABELS` |
| `60-vs-dest-missing.yaml` | `VS-DEST-MISSING` (Ziel ohne Service, der Mirror hat einen) |
| `61-vs-dest-port.yaml` | `VS-DEST-PORT` ×2 (Port fehlt am Gateway, Port existiert nicht). Der VS nur am Sidecar ohne Port bleibt ohne Befund. |
| `70-vs-gw-ref.yaml` | `VS-GW-MISSING`, `VS-GW-HOST` ×2 (ERROR: kein Host passt, WARN: ein Host passt nicht). Der VS mit `mesh` bleibt ohne Befund. |
| `80-dr-subset-nopods.yaml` | `DR-SUBSET-NOPODS` ×3 (ERROR für die verwendeten Subsets v1 und v2, WARN für das unbenutzte v3) |

Die exakte Liste steht in [`test/expected.tsv`](test/expected.tsv).

### Installation

```bash
./install.sh               # Namespace vstest
./install.sh <namespace>   # z. B. ein bestehender Namespace auf einem anderen Cluster
```

Führt intern aus:

```bash
# nur wenn der Namespace noch nicht existiert:
kubectl create namespace <namespace>
kubectl label namespace <namespace> istio-injection=enabled

kubectl apply -n <namespace> -f manifests/
```

Beim Apply warnt der Istio-Webhook bereits bei einigen Route-Fällen: bei
Routen nach einem Catch-All (kein Match oder `prefix: /`, also 30 und
`route-root` in 31) und beim identischen Match in 32. Nicht erkannt werden die
Überlagerungen zwischen mehreren Ressourcen, der allgemeinere Prefix in
`route-prefix` (31) sowie der Regex- und der Header-Fall in 32.

### Test

```bash
./test.sh                # gegen den Cluster (Namespace vstest)
./test.sh <namespace>    # gegen den Cluster, anderer Namespace
./test.sh --offline      # direkt gegen manifests/, kein Cluster nötig
```

Führt intern aus:

```bash
python3 virtualservice.py <namespace> > "$TMP/output.txt"
# bzw. offline: python3 virtualservice.py vstest --file manifests/
# ohne installiertes PyYAML: uv run -q --no-project --with pyyaml python3 virtualservice.py ...
sed -nE 's/^\[[A-Z ]+\] +([A-Z-]+) +(.*)/\1\t\2/p' "$TMP/output.txt" | sed 's/, /,/g' \
  | sort > "$TMP/actual.tsv"
grep -v '^#' test/expected.tsv | sort > "$TMP/expected.tsv"
diff -u "$TMP/expected.tsv" "$TMP/actual.tsv"
```

Der Test schlägt fehl, wenn ein erwarteter Befund fehlt oder ein zusätzlicher
auftaucht (z. B. ein Fehlalarm für `10-ok.yaml` außer `DR-SUBSET-NOPODS`). Mit `--offline` prüft er
zusätzlich, dass Ladefehler (kaputtes YAML, fehlende Datei, Ressource ohne
Namen, keine VS/DR für den Namespace) Exit-Code `2` liefern und `kind: List`
gelesen wird. Exit-Code `1` des Tools
(ERROR-Befunde) ist erwartet; bricht es mit einem anderen Code ab, z. B. bei
einem Ladefehler, bricht auch der Test mit diesem Code ab, statt einen
irreführenden Diff zu zeigen.

### Aufräumen

```bash
./uninstall.sh               # Namespace vstest
./uninstall.sh <namespace>
```

Führt intern aus:

```bash
kubectl delete -n <namespace> -f manifests/ --ignore-not-found
```

Der Namespace selbst bleibt bestehen, da er auch ein vorhandener sein kann.
Einen von `install.sh` angelegten Namespace entfernt man bei Bedarf mit
`kubectl delete namespace vstest`.
