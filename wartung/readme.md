# Wartungsmodus Usecase

Simuliert die Wartung eines Services nur mit Istio-Mitteln. Zwei Helm-Charts
deployen den [http-tester](https://github.com/wlanboy/httptester) einmal als
`client` und einmal als `service`. Ein `kubectl apply -f manifestwartung/`
schaltet den Service in den Wartungsmodus: Aufrufer bekommen `503 Service
Unavailable` mit `Retry-After: 300`. Helm-Chart, Deployment und Pod des Services
bleiben dabei unverändert.

## Architektur

```
Namespace client                          Namespace service
  client (http-tester) ──mesh──────────▶   service (http-tester)
        ▲                                       ▲
        │ client.localhost                      │ service.localhost
        └──────── Istio Ingress-Gateway ────────┘
                  (istio-ingress, hostPort 80)

Ohne manifestwartung/:                Mit manifestwartung/:
  client -> service    200              client -> service    503 + Retry-After: 300
  service.localhost    200              service.localhost    503 + Retry-After: 300
                                        mit Header x-wartung-bypass: true -> 200
```

- Beide Namespaces haben `istio-injection: enabled`, jeder Pod bekommt einen
  Envoy-Sidecar.
- Jedes Chart bringt `Deployment`, `Service`, ein Istio-`Gateway` und einen
  `VirtualService` für seinen Hostnamen mit. Der Service lauscht auf Port 80
  und leitet auf Container-Port 5000 weiter.
- Der `VirtualService` im Service-Chart hängt nur am Gateway, nicht an `mesh`.
  Mesh-interne Aufrufe auf `service.service.svc.cluster.local` laufen also ohne
  VirtualService. So kann der Wartungs-VirtualService diesen Host übernehmen,
  ohne mit dem Chart zu kollidieren. Details stehen unter
  [Warum zwei VirtualServices](#warum-zwei-virtualservices).
- Die 503-Antwort erzeugt Envoy per `directResponse`. Beim mesh-internen Aufruf
  antwortet der Sidecar des Clients, beim Aufruf von außen das Ingress-Gateway.
  Der Request erreicht den Service-Pod in beiden Fällen nicht. Liveness- und
  Readiness-Probes laufen am VirtualService vorbei, der Pod bleibt `Ready`.

## Voraussetzungen

- lokaler kind-Cluster mit Istio. Das Istio Ingress-Gateway läuft in
  `istio-ingress` mit Label `istio: ingressgateway` und hostPort 80, sodass
  `http://*.localhost` vom Host aus beim Gateway ankommt.
- `kubectl` mit Kontext auf den kind-Cluster, `helm` ab v3
- Image `wlanboy/http-tester:latest` von Docker Hub. Ein lokal gebautes Image
  aus `../../httptester` lässt sich vorher in kind laden:

  ```bash
  docker build -t wlanboy/http-tester:latest ../../httptester
  kind load docker-image wlanboy/http-tester:latest --name local
  ```

## Verzeichnis

| Pfad                                                | Inhalt                                                   |
|-----------------------------------------------------|----------------------------------------------------------|
| `charts/client/`                                    | Helm-Chart Client (Host `client.localhost`)              |
| `charts/service/`                                   | Helm-Chart Service (Host `service.localhost`)            |
| `manifests/00-namespaces.yaml`                      | Namespaces `client` und `service` mit Sidecar-Injection  |
| `manifestwartung/10-virtualservice-wartung-mesh.yaml`    | Wartung für mesh-interne Aufrufe                    |
| `manifestwartung/11-virtualservice-wartung-ingress.yaml` | Wartung für Aufrufe über `service.localhost`        |
| `setup.sh` / `delete.sh`                            | Installation / Deinstallation                            |
| `wartung-an.sh` / `wartung-aus.sh`                  | Wartungsmodus ein- / ausschalten                         |
| `run.sh`                                            | Test aller vier Aufrufwege                               |
| `cron/cronjob.yaml`, `cron/cron.md`                 | CronJob mit curl, der die Wartung erkennt                |

## Installation

```bash
./setup.sh
```

Führt intern aus:

```bash
kubectl apply -f manifests/00-namespaces.yaml
helm upgrade --install service ./charts/service --namespace service
helm upgrade --install client ./charts/client --namespace client
kubectl -n service rollout status deployment/service --timeout=120s
kubectl -n client rollout status deployment/client --timeout=120s
kubectl apply -f cron/cronjob.yaml
kubectl -n service get deploy,svc,gateway,virtualservice
kubectl -n client get deploy,svc,gateway,virtualservice,cronjob
```

`setup.sh` legt auch den CronJob `healthz-cron` an, Details in
[cron/cron.md](cron/cron.md).

Danach erreichbar:

- Client-UI: <http://client.localhost>
- Service-UI: <http://service.localhost>

## Test ausführen

```bash
./run.sh
```

Das Skript ruft die API `/api/request` des Clients auf. Der Request an den
Service startet also im Client-Pod und läuft durch dessen Sidecar. Dazu kommt
ein direkter Aufruf über das Ingress-Gateway. Intern:

```bash
# client -> service, ohne und mit Bypass-Header
curl -s http://client.localhost/api/request -H 'content-type: application/json' \
  -d '{"url":"http://service.service.svc.cluster.local/healthz","headers":""}'
curl -s http://client.localhost/api/request -H 'content-type: application/json' \
  -d '{"url":"http://service.service.svc.cluster.local/healthz","headers":"x-wartung-bypass: true"}'

# von außen über das Ingress-Gateway, ohne und mit Bypass-Header
curl -s -o /dev/null -w 'HTTP-Status: %{http_code} | Retry-After: %header{retry-after}\n' \
  http://service.localhost/healthz
curl -s -o /dev/null -w 'HTTP-Status: %{http_code} | Retry-After: %header{retry-after}\n' \
  -H 'x-wartung-bypass: true' http://service.localhost/healthz

# Prüfung, ob der Wartungsmodus aktiv ist
kubectl -n service get virtualservice service-wartung
```

Dasselbe geht in der Client-UI unter <http://client.localhost> mit der URL
`http://service.service.svc.cluster.local/healthz`.

Erwartete Ausgabe **ohne** Wartung:

```
==> client -> service (mesh-intern, http://service.service.svc.cluster.local/healthz)
HTTP-Status: 200 | Retry-After: - | Body: {"status":"ok"}
==> client -> service mit Header 'x-wartung-bypass: true'
HTTP-Status: 200 | Retry-After: - | Body: {"status":"ok"}
==> Browser/curl -> http://service.localhost/healthz (Ingress-Gateway)
HTTP-Status: 200 | Retry-After:
==> Browser/curl -> http://service.localhost/healthz mit Header 'x-wartung-bypass: true'
HTTP-Status: 200 | Retry-After:

==> Wartungsmodus ist NICHT aktiv: alle Aufrufe erwarten 200.
```

## Wartungsmodus aktivieren

```bash
./wartung-an.sh
./run.sh
```

`wartung-an.sh` wendet die Manifeste an und wartet dann bis zu 30 Sekunden,
bis der Client wirklich 503 bekommt. istiod braucht ein paar Sekunden, bis die
neuen Routen in den Sidecars ankommen. Ohne das Warten zeigt ein direkt
folgendes `./run.sh` manchmal noch 200. Vorher hält es alle CronJobs mit dem
Label `wartung: service` an und löscht deren laufende Jobs, Details in
[cron/cron.md](cron/cron.md). Intern:

```bash
# für jeden CronJob aus: kubectl get cronjob -A -l wartung=service
kubectl -n <namespace> patch cronjob <name> --type merge -p '{"spec":{"suspend":true}}'
# für jeden laufenden Job aus: kubectl get job -A -l wartung=service
kubectl -n <namespace> delete job <name>

kubectl apply -f manifestwartung/
# jede Sekunde, bis status_code 503 ist (max. 30 Versuche)
curl -s http://client.localhost/api/request -H 'content-type: application/json' \
  -d '{"url":"http://service.service.svc.cluster.local/healthz"}'
```

Erwartete Ausgabe **mit** Wartung:

```
==> client -> service (mesh-intern, http://service.service.svc.cluster.local/healthz)
HTTP-Status: 503 | Retry-After: 300 | Body: {"status":"maintenance","message":"Service wird gewartet, bitte später erneut versuchen."}
==> client -> service mit Header 'x-wartung-bypass: true'
HTTP-Status: 200 | Retry-After: - | Body: {"status":"ok"}
==> Browser/curl -> http://service.localhost/healthz (Ingress-Gateway)
HTTP-Status: 503 | Retry-After: 300
==> Browser/curl -> http://service.localhost/healthz mit Header 'x-wartung-bypass: true'
HTTP-Status: 200 | Retry-After:

==> Wartungsmodus ist AKTIV: ohne Bypass-Header 503 + Retry-After: 300, mit Bypass-Header 200.
```

Mit dem Header `x-wartung-bypass: true` kann das Wartungsteam den Service
während der Wartung weiter aufrufen, etwa um ein Update zu prüfen, bevor
die Wartung endet.

Wartungsmodus beenden:

```bash
./wartung-aus.sh
```

Intern dasselbe in umgekehrter Richtung. Das Skript wartet, bis der Client
wieder 200 bekommt, und setzt erst dann die CronJobs fort:

```bash
kubectl delete -f manifestwartung/ --ignore-not-found
# jede Sekunde, bis status_code 200 ist (max. 30 Versuche)
curl -s http://client.localhost/api/request -H 'content-type: application/json' \
  -d '{"url":"http://service.service.svc.cluster.local/healthz"}'

# für jeden CronJob aus: kubectl get cronjob -A -l wartung=service
kubectl -n <namespace> patch cronjob <name> --type merge -p '{"spec":{"suspend":false}}'
```

Kommt nach 30 Sekunden nicht der erwartete Status, endet das Skript mit
Exit-Code 1 und die CronJobs bleiben angehalten.

Wartezeit in `retry-after`, Body und Bypass-Header stehen direkt in den beiden
Manifesten unter `manifestwartung/`. Wer sie ändert, ändert nur diese Dateien.

## Warum zwei VirtualServices

Istio behandelt den Weg über das Mesh und den Weg über das Gateway
unterschiedlich. Deshalb gibt es pro Weg ein eigenes Manifest.

- **Mesh, `10-virtualservice-wartung-mesh.yaml`.** Für
  `service.service.svc.cluster.local` gibt es aus dem Chart keinen
  VirtualService. Der Wartungs-VirtualService ist der einzige für diesen Host
  und greift im Sidecar jedes Aufrufers im Mesh. Mehrere VirtualServices für
  denselben Host werden für Sidecars nicht zusammengeführt. Hätte das Chart
  hier schon einen, wäre das Ergebnis nicht verlässlich.
- **Ingress, `11-virtualservice-wartung-ingress.yaml`.** Für
  `service.localhost` am `service-gateway` gibt es schon den VirtualService aus
  dem Chart. Am Gateway führt Istio VirtualServices für denselben Host zu einer
  Routenliste zusammen. Dabei kommen alle Routen mit Match zuerst und alle
  Catch-all-Routen ans Ende, untereinander nach Erstellungszeit sortiert. Die
  Route aus dem Chart ist ein Catch-all und älter. Eine Wartungsroute ohne
  Match würde nie erreicht. Das gilt auch für `prefix: /` und `regex: .*`, beide
  zählen für Istio als Catch-all, der Test im kind-Cluster bestätigt das. Die
  Wartungsroute matcht deshalb per `withoutHeaders` auf alle Requests ohne
  Bypass-Header.

Ein VirtualService am Gateway, der auf `service.service.svc.cluster.local`
routet, schickt den Request direkt zum Ziel. Der Mesh-VirtualService dieses
Hosts wird dabei nicht ausgewertet. Ohne das zweite Manifest käme man über
`service.localhost` also an der Wartung vorbei.

Die zusammengeführte Routenliste am Gateway lässt sich so prüfen:

```bash
istioctl proxy-config routes -n istio-ingress deploy/istio-ingressgateway --name http.80 -o json
```

Für `service.localhost:80` muss die Reihenfolge `wartung-bypass`, `wartung`
und dann die Route aus dem Chart sein.

## Troubleshooting

- **Client bekommt weiter 200 statt 503.** Direkt nach dem `apply` ist oft zu
  früh, istiod braucht ein paar Sekunden für den xDS-Push. Prüfen, ob
  die Route beim Client-Sidecar angekommen ist:
  ```bash
  istioctl proxy-config routes -n client deploy/client --name 80 -o json | grep -A3 wartung
  ```
- **`service.localhost` liefert 200, mesh-intern kommt 503.** Entweder ist nur
  `10-virtualservice-wartung-mesh.yaml` angewendet, oder die Wartungsroute im
  Ingress-Manifest hat keinen Match mehr und landet hinter der Chart-Route.
- **Client-Pod ohne Sidecar.** Ohne Sidecar wertet niemand den VirtualService
  aus, der Request geht direkt zum Service-Pod. Prüfen:
  ```bash
  kubectl -n client get pod -l app=client -o jsonpath='{.items[0].spec.containers[*].name}'
  kubectl get namespace client service --show-labels
  ```
- **Pods kommen nicht hoch (`ImagePullBackOff`).** Der kind-Cluster erreicht
  Docker Hub nicht. Dann das Image lokal bauen und per `kind load docker-image`
  laden, die Befehle stehen unter Voraussetzungen.

## Aufräumen

```bash
./delete.sh
```

Führt intern aus:

```bash
kubectl delete -f manifestwartung/ --ignore-not-found
kubectl delete -f cron/cronjob.yaml --ignore-not-found
helm uninstall client --namespace client --ignore-not-found
helm uninstall service --namespace service --ignore-not-found
kubectl delete -f manifests/00-namespaces.yaml --ignore-not-found
```

Die Namespaces `client` und `service` werden mit gelöscht.
