# Mirror Logging Usecase

Demonstriert Istio **Traffic Mirroring** (Shadow-Traffic): ein `VirtualService`
schickt jeden Request an `nginx` zusätzlich als 1:1-Kopie (fire-and-forget) an
einen zweiten Service `logger`. Der Client bekommt ausschließlich die Antwort
von `nginx` (PRIMARY) zu sehen — der Mirror läuft asynchron im Hintergrund und
beeinflusst weder Response-Body/-Status noch nennenswert die Latenz. Genau
dieses Muster wird in der Praxis genutzt, um z. B. eine neue Service-Version
mit echtem Produktionstraffic zu testen, ohne echte Nutzer zu gefährden.

Der `logger`-Pod ist in diesem Usecase kein echtes Backend, sondern ein
kleiner Python-HTTP-Server, der jeden eingehenden (gespiegelten) Request
protokolliert — Methode, Pfad, **alle Header** und Body. Der Wert des
`Authorization`-Headers (typischerweise ein OAuth-Bearer-Token) sowie einiger
weiterer Token-Header wird dabei durch `*`-Zeichen ersetzt, bevor er ins Log
geschrieben wird — die Redaction passiert im `logger` selbst, das Token
erscheint an keiner Stelle im Klartext im Log.

**Zwei getrennte Schritte, bewusst:** `install.sh` installiert nur die
Infrastruktur (`nginx`, `logger`) — **ohne** den Mirror-`VirtualService`.
`nginx` ist danach bereits ganz normal per `curl` erreichbar, aber es wird
noch nichts gespiegelt. Erst `run.sh` wendet
`manifests/20-virtualservice-mirror.yaml` an und aktiviert damit das
Mirroring, bevor es den Test-Request schickt — so lässt sich der Effekt der
Mirror-Regel gezielt vorher/nachher demonstrieren, statt dass sie schon beim
Setup unsichtbar mit installiert wird.

## Architektur

```
Test-Client-Pod --curl (Authorization: Bearer <token>)--> Envoy-Sidecar (outbound)
                                                              |
                                          VirtualService "nginx":
                                            |-- route (100%)  --> nginx-Service --> nginx-Pod (PRIMARY, Antwort geht an Client)
                                            |-- mirror (100%) --> logger-Service --> logger-Pod
                                                                     |
                                                    protokolliert Method/Path/Header/Body,
                                                    Authorization-Wert durch "*" ersetzt,
                                                    Antwort des logger wird von Envoy VERWORFEN
```

- Der `VirtualService` für den Host `nginx` definiert neben der normalen
  `route` zusätzlich `mirror`/`mirrorPercentage`. Envoy schickt daraufhin
  jeden Request, der an `nginx` geht, **zusätzlich** (nicht stattdessen) an
  den `logger`-Service — als eigenständige, unabhängige Kopie.
- **Fire-and-forget:** Die Antwort des `logger` wird von Envoy verworfen.
  Der Client wartet nicht auf sie und bekommt sie auch nicht zu sehen — nur
  die Antwort von `nginx` zählt für ihn.
- Der `logger` ist ein einfacher `http.server`-basierter Python-Server
  (`manifests/06-logger-configmap.yaml`), der für **jede** HTTP-Methode alle
  Header ausgibt. Für Header, deren Name auf einen Token schließen lässt
  (`authorization`, `proxy-authorization`, `x-auth-token`,
  `x-access-token`), wird nur das Auth-Schema (z. B. `Bearer`) geloggt, der
  eigentliche Tokenwert vollständig durch `*` ersetzt.
- **Wichtig:** Wie bei [`faultInjection/`](../faultInjection/readme.md) und
  [`namespaceRouting/`](../namespaceRouting/readme.md) wird die
  Mirror-Regel am **ausgehenden** Envoy-Sidecar des *aufrufenden* Pods
  ausgewertet. Der Test-Client muss daher selbst Teil des Meshes sein
  (Sidecar-Injection aktiv), sonst wird nichts gespiegelt und der Request
  geht unverändert nur an `nginx`.

## Voraussetzungen

- laufender Kubernetes-Cluster mit installiertem Istio (Sidecar-Injection verfügbar)
- `kubectl` mit gültigem Kontext auf diesen Cluster

## Installation

```bash
./install.sh                          # Standard-Namespace: mirror-logging-demo
./install.sh mein-namespace           # eigenen Namespace verwenden
```

Existiert der angegebene Namespace bereits, wird `00-namespace.yaml` übersprungen
(kein erneutes Anlegen/Überschreiben) — nur ConfigMaps, Deployments und Services
werden in diesen bestehenden Namespace appliziert. Der Mirror-`VirtualService`
(`20-virtualservice-mirror.yaml`) wird hier **bewusst nicht** appliziert, das
übernimmt erst `run.sh`.

Führt intern aus (Platzhalter `${NAMESPACE}` in den Manifesten werden per `sed`
durch den gewählten Namespace ersetzt):

```bash
kubectl get namespace <namespace>                       # Existenzprüfung
sed "s|\${NAMESPACE}|<namespace>|g" manifests/00-namespace.yaml | kubectl apply -f -   # nur falls Namespace neu
sed "s|\${NAMESPACE}|<namespace>|g" manifests/05-nginx-configmap.yaml | kubectl apply -f -
sed "s|\${NAMESPACE}|<namespace>|g" manifests/06-logger-configmap.yaml | kubectl apply -f -
sed "s|\${NAMESPACE}|<namespace>|g" manifests/10-nginx-deployment.yaml | kubectl apply -f -
sed "s|\${NAMESPACE}|<namespace>|g" manifests/11-nginx-service.yaml | kubectl apply -f -
sed "s|\${NAMESPACE}|<namespace>|g" manifests/12-logger-deployment.yaml | kubectl apply -f -
sed "s|\${NAMESPACE}|<namespace>|g" manifests/13-logger-service.yaml | kubectl apply -f -
# 20-virtualservice-mirror.yaml wird HIER übersprungen, siehe run.sh
kubectl -n <namespace> rollout status deployment/nginx --timeout=120s
kubectl -n <namespace> rollout status deployment/logger --timeout=120s
kubectl -n <namespace> get deploy,svc,virtualservice
```

**Hinweis:** Wird ein bereits existierender Namespace verwendet, muss dieser das
Label `istio-injection: enabled` tragen (bzw. Sidecar-Injection anderweitig
aktiviert haben) — sowohl `nginx` als auch der Test-Client müssen Teil des Meshes
sein, damit die Mirror-Regel (sobald aktiviert) greift.

## Test ausführen

```bash
./run.sh                         # Standard-Namespace: mirror-logging-demo
./run.sh mein-namespace          # eigenen Namespace verwenden
```

Aktiviert zuerst das Mirroring (`20-virtualservice-mirror.yaml`, von
`install.sh` bewusst ausgelassen) und startet danach einen Test-Client-Pod,
der einen `POST`-Request mit einem
`Authorization: Bearer s3cr3t-oauth-token-abcdef123456`-Header, einem
zusätzlichen Custom-Header sowie einem JSON-Body gegen
`http://nginx.<namespace>.svc.cluster.local` absetzt. Anschließend werden die
Logs des `logger`-Pods ausgegeben.

Führt intern aus (Platzhalter aus `test/client-pod.yaml` werden per `sed`
ersetzt):

```bash
# Mirroring aktivieren (einmalig; erneute Aufrufe von run.sh sind ein No-Op-Apply)
sed "s|\${NAMESPACE}|<namespace>|g" manifests/20-virtualservice-mirror.yaml | kubectl apply -f -

sed -e "s|\${POD_NAME}|<generierter-name>|g" \
    -e "s|\${NAMESPACE}|<namespace>|g" \
    -e "s|\${TARGET}|http://nginx.<namespace>.svc.cluster.local|g" \
    test/client-pod.yaml | kubectl apply -f -

kubectl -n <namespace> wait --for=jsonpath='{.status.phase}'=Succeeded pod/<generierter-name> --timeout=90s
kubectl -n <namespace> logs <generierter-name> -c client
kubectl -n <namespace> delete pod <generierter-name>

# kurze Wartezeit, da der Mirror asynchron/fire-and-forget zugestellt wird
kubectl -n <namespace> logs deployment/logger --since=15s
```

Erwartete Ausgabe (gekürzt):

```
=== Request an http://nginx.mirror-logging-demo.svc.cluster.local (mit Authorization-Header und JSON-Body) ===
HTTP-Status: 200, Dauer: 0.014s

==> Logs des logger-Pods 'logger-...' (gespiegelte Anfrage, Authorization-Header redigiert):
=== Mirrored Request: POST / ===
Host: nginx.mirror-logging-demo.svc.cluster.local
Authorization: Bearer *******************************
X-Demo-Header: mirror-test
Content-Type: application/json
Content-Length: 17
--- Body (17 bytes) ---
{"hello":"world"}
=== Ende ===
```

Die Antwort HTTP-Status `200` mit sehr kurzer Dauer beweist, dass der Client
ausschließlich mit `nginx` (PRIMARY) spricht — der `logger` läuft komplett im
Hintergrund mit. Im Log des `logger` erscheint derselbe Request ein zweites
Mal, aber mit geschwärztem `Authorization`-Wert.

Der Test-Client-Pod läuft **mit** Istio-Sidecar (analog zu
[`faultInjection/`](../faultInjection/readme.md) und
[`namespaceRouting/`](../namespaceRouting/readme.md)), da die Mirror-Regel am
Client-seitigen Envoy ausgewertet wird. Als natives Sidecar (Istio 1.27+,
K8s 1.29+, Standard seit Istio 1.27) beendet Kubelet den `istio-proxy`
automatisch, sobald der `client`-Container terminiert.

## Troubleshooting

Erscheint im Log des `logger`-Pods **kein** gespiegelter Request:

- **`VirtualService` noch nicht aktiviert.** Anders als bei den übrigen
  Usecases legt hier `install.sh` den Mirror-`VirtualService` **nicht** an —
  das passiert erst beim ersten Aufruf von `./run.sh`. Prüfen mit:
  ```bash
  kubectl -n <namespace> get virtualservice nginx
  ```
  Fehlt er, `./run.sh` (erneut) ausführen.

- **Sidecar fehlt beim Client.** Prüfen mit:
  ```bash
  kubectl -n <namespace> get pod <pod-name> -o jsonpath='{.spec.containers[*].name}'
  ```
  Es müssen zwei Container erscheinen (`client` und `istio-proxy`).

- **`VirtualService` falscher Namespace.** Er muss im selben Namespace wie der
  `nginx`-Service liegen (hier `<namespace>`), damit er ohne `exportTo`/Cross-
  Namespace-Konfiguration automatisch für Aufrufer aus anderen Namespaces
  sichtbar ist.

- **Zu kurz nach der Installation getestet.** Istiod braucht nach dem Anwenden
  des `VirtualService` kurz Zeit, bis die Regel per xDS beim Sidecar des
  Clients ankommt. Kurz warten und `./run.sh` erneut ausführen.

- **`logger`-Pod noch nicht bereit / neu gestartet.** Status prüfen mit
  `kubectl -n <namespace> get pod -l app=logger`.

Erscheint der `Authorization`-Header **unredigiert** im Log: Header-Name im
Request prüfen — die Redaction in `manifests/06-logger-configmap.yaml`
(`REDACT_HEADERS`) matcht case-insensitiv exakt auf `authorization`,
`proxy-authorization`, `x-auth-token` bzw. `x-access-token`. Ein anderer
Header-Name für das Token müsste dort ergänzt werden.

Aktueller Zustand der Regel:

```bash
kubectl -n <namespace> get virtualservice nginx -o yaml
istioctl proxy-config route <client-pod> -n <namespace> -o json
```

## Aufräumen

```bash
./uninstall.sh                   # Standard-Namespace: mirror-logging-demo
./uninstall.sh mein-namespace    # eigenen Namespace verwenden
```

Entfernt nur die von diesem Usecase angelegten Ressourcen (VirtualService,
Deployments, Services, ConfigMaps) — der Namespace selbst bleibt bestehen. Zum
vollständigen Entfernen:

```bash
kubectl delete namespace <namespace>   # z.B. mirror-logging-demo
```
