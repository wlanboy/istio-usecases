# Hold Connection Usecase (Umsetzungsplan)

> **Status:** Planung. Manifeste und Skripte sind noch nicht umgesetzt. Diese
> Datei beschreibt Zielbild, Aufbau und Testfälle; nach der Umsetzung wird sie
> zur regulären `readme.md` im Stil der anderen Usecases.

Demonstriert eine **Sync-to-Async-Bridge** hinter dem Istio Ingress-Gateway:
Ein externer Client kann nur synchron arbeiten (ein Request, eine Response). Die
Verarbeitung im Cluster läuft aber asynchron: `service-a` nimmt den Request an,
beauftragt `service-b` asynchron (HTTP 202), und `service-b` meldet das Ergebnis
später per Callback an `service-a` zurück. Erst dann antwortet `service-a` auf die
die ganze Zeit offen gehaltene Verbindung des Clients.

**Abgrenzung:** Istio hält die Verbindung nur offen (Envoy wartet als Proxy auf
die Upstream-Response). Die Zuordnung von Callback zu wartendem Request
(Korrelation) kann Istio **nicht** leisten, die liegt in `service-a`. Der
Beitrag von Istio in diesem Usecase ist:

1. Timeouts und Retries entlang der Kette so zu setzen, dass lange gehaltene
   Verbindungen funktionieren und kein Job doppelt gestartet wird.
2. Den Callback per **Consistent Hashing** auf genau den `service-a`-Pod zu
   routen, der die Client-Verbindung hält, ohne Shared State zwischen den
   Replicas.

## Architektur

```
Client (sync)                                      x-correlation-id: <uuid>
   |
   v
Istio Ingress-Gateway  -- VirtualService (timeout 30s, retries 0)
   |
   v  DestinationRule: consistentHash auf x-correlation-id
service-a (2 Replicas)                              service-b (worker)
   | 1. parkt Request unter <uuid>                     |
   | 2. POST /jobs  ------------------------------->   | 3. antwortet sofort 202
   |    (x-correlation-id, x-work-delay)               | 4. arbeitet x-work-delay Sekunden
   |                                                   |
   | <------------ 5. POST /callback ---------------   |
   |    (x-correlation-id: <uuid>, via Mesh + DR-Hash landet auf demselben Pod)
   | 6. weckt geparkten Request, antwortet 200
   v
Client bekommt Ergebnis
```

- Gateway→`service-a` und `service-b`→`service-a` hashen beide auf denselben
  Header `x-correlation-id`. Solange sich die Endpoint-Liste von `service-a` nicht
  ändert, landen beide Requests auf demselben Pod.
- Ohne `DestinationRule` verteilt Envoy per Round Robin. Bei 2 Replicas landet
  der Callback dann in ca. 50 % der Fälle auf dem falschen Pod, der die
  `correlationId` nicht kennt. Der wartende Request läuft in den Timeout.
- Die `x-correlation-id` setzt in diesem Usecase der Client (`run.sh` erzeugt
  eine UUID pro Request). Fehlt der Header, erzeugt `service-a` selbst eine ID
  und protokolliert eine Warnung. Diese Requests sind dann nicht hash-gebunden.

## Geplante Dateistruktur

```
holdConnection/
├── install.sh
├── run.sh
├── uninstall.sh
├── readme.md
├── manifests/
│   ├── 00-namespace.yaml                  # istio-injection: enabled
│   ├── 05-service-a-configmap.yaml        # Python-Skript bridge.py
│   ├── 06-service-b-configmap.yaml        # Python-Skript worker.py
│   ├── 10-service-a-deployment.yaml       # 2 Replicas
│   ├── 11-service-a-service.yaml
│   ├── 12-service-b-deployment.yaml       # 1 Replica
│   ├── 13-service-b-service.yaml
│   ├── 20-gateway.yaml                    # selector istio: ingressgateway, Host hold.demo.local
│   ├── 21-virtualservice-gateway.yaml     # Gateway -> service-a, timeout 30s, retries 0
│   ├── 22-virtualservice-mesh.yaml        # Mesh-intern: service-a und service-b, je retries 0
│   └── 30-destinationrule-hash.yaml       # erst durch run.sh angewendet (vorher/nachher)
└── test/
    └── client-pod.yaml                    # curl ohne Sidecar gegen das Ingress-Gateway
```

Wie in [`mirrorLogging/`](../mirrorLogging/readme.md) installiert `install.sh`
bewusst **ohne** die entscheidende Regel (`30-destinationrule-hash.yaml`). So
lässt sich in `run.sh` der Effekt vorher/nachher zeigen.

## Komponenten

Beide Services sind kleine Python-Skripte aus der Standardbibliothek, per
ConfigMap gemountet (Muster wie `logger` in `mirrorLogging/`). Es wird kein
eigenes Image gebaut; Basis-Image z. B. `python:3.13-slim`.

### service-a (`bridge.py`)

- `ThreadingHTTPServer`, Port 8080.
- `GET /process`:
  1. liest `x-correlation-id` (oder erzeugt eine), liest optional `x-work-delay`
  2. legt `pending[id] = threading.Event()` an
  3. ruft `POST http://service-b/jobs` mit beiden Headern auf und erwartet `202`
  4. wartet `event.wait(timeout=APP_TIMEOUT)` (Default 25s, **kleiner** als der
     Gateway-Timeout, damit die App mit einem sauberen `504` antwortet statt
     Envoy)
  5. antwortet `200` mit Ergebnis, Pod-Name und Wartezeit, oder `504` bei Timeout
- `POST /callback`:
  - ID bekannt: Ergebnis ablegen, `event.set()`, `200`
  - ID unbekannt: `404` mit Pod-Name im Body. Das ist der sichtbare Beleg für
    "Callback auf falschem Pod gelandet".
- Loggt jede Aktion mit `HOSTNAME`, damit die Pod-Zuordnung im Log
  nachvollziehbar ist.

Hinweis: Ein Thread pro wartendem Request reicht für die Demo. In Spring Boot
entspricht das `DeferredResult`/`CompletableFuture`, dort ohne blockierten
Request-Thread.

### service-b (`worker.py`)

- `ThreadingHTTPServer`, Port 8080.
- `POST /jobs`: antwortet sofort `202`, startet einen Hintergrund-Thread.
- Der Thread schläft `x-work-delay` Sekunden (Default 3) und schickt dann
  `POST http://service-a/callback` mit `x-correlation-id` und einem kleinen
  JSON-Ergebnis.
- Zählt eingehende Jobs pro `correlationId` und loggt Duplikate. Damit lässt
  sich ein durch Retries doppelt ausgelöster Job erkennen.

## Istio-Konfiguration

`21-virtualservice-gateway.yaml`:

```yaml
apiVersion: networking.istio.io/v1
kind: VirtualService
metadata:
  name: service-a-gateway
  namespace: ${NAMESPACE}
spec:
  hosts: ["hold.demo.local"]
  gateways: ["hold-connection-gateway"]
  http:
    - route:
        - destination:
            host: service-a
            port:
              number: 8080
      timeout: 30s
      retries:
        attempts: 0
```

`22-virtualservice-mesh.yaml`: zwei VirtualServices für den Mesh-internen
Verkehr, `service-a` (Callback) und `service-b` (`POST /jobs`), beide mit
`retries.attempts: 0`. Ein wiederholter Callback wäre harmlos, ein wiederholter
`POST /jobs` würde den Job aber doppelt starten.

`30-destinationrule-hash.yaml`:

```yaml
apiVersion: networking.istio.io/v1
kind: DestinationRule
metadata:
  name: service-a
  namespace: ${NAMESPACE}
spec:
  host: service-a
  trafficPolicy:
    loadBalancer:
      consistentHash:
        httpHeaderName: x-correlation-id
```

Die `DestinationRule` greift an zwei Stellen: am Ingress-Gateway (Hop Client →
A) und am Sidecar von `service-b` (Hop Callback → A). Beide müssen dieselbe
Endpoint-Liste sehen, damit der Hash auf denselben Pod zeigt.

## Testfälle (`run.sh`)

`run.sh` startet einen Test-Client-Pod **ohne** Sidecar, der das Ingress-Gateway
wie ein externer Client aufruft
(`http://istio-ingressgateway.istio-system.svc.cluster.local` mit
`Host: hold.demo.local`).

| # | Schritt | Erwartung |
|---|---------|-----------|
| 1 | 10 Requests, `x-work-delay: 3`, **ohne** DestinationRule | ca. die Hälfte `200` nach ~3s, der Rest `504` nach ~25s; im Log von `service-a` erscheinen `404`-Callbacks auf dem jeweils anderen Pod |
| 2 | `kubectl apply` der DestinationRule, kurz warten (xDS-Propagation) | – |
| 3 | 10 Requests, `x-work-delay: 3`, **mit** DestinationRule | alle `200` nach ~3s; Pod-Name in Response und Callback-Log identisch |
| 4 | 1 Request, `x-work-delay: 40` | `504` von `service-a` nach 25s; der späte Callback bekommt `404` (Request ist schon verworfen) |
| 5 | Worker-Log auf Duplikate prüfen | keine doppelten `correlationId`s (Retries sind aus) |

Die Requests in Schritt 1 und 3 laufen **parallel** (`curl` im Hintergrund plus
`wait`), damit mehrere Verbindungen gleichzeitig gehalten werden.

Beispielhafte Ausgabe:

```
=== ohne DestinationRule ===
id=3f2c... HTTP 200  3.02s  pod=service-a-7d9f-abc
id=91be... HTTP 504 25.01s  pod=service-a-7d9f-xyz
...
Erfolgreich: 5/10

=== mit DestinationRule (consistentHash x-correlation-id) ===
id=0a7d... HTTP 200  3.01s  pod=service-a-7d9f-abc
...
Erfolgreich: 10/10
```

## Timeout-Kette

Das kürzeste Timeout in der Kette entscheidet, wie lange eine Verbindung gehalten
werden kann. Für den Usecase gilt diese Staffelung (von innen nach außen jeweils
größer):

| Stelle | Wert | Konfiguration |
|--------|------|---------------|
| `service-a` Wartezeit | 25s | Env `APP_TIMEOUT` |
| VirtualService Route-Timeout | 30s | `http[].timeout` |
| Envoy `stream_idle_timeout` | 5 min (Envoy-Default) | unverändert, wird nicht erreicht |
| externer Loadbalancer vor dem Gateway | z. B. AWS ELB 60s | im Usecase nicht vorhanden, in der readme als Hinweis |
| Client | 35s | `curl --max-time 35` |

## Bekannte Grenzen (in der readme dokumentieren)

- **Consistent Hash ist nicht stabil bei Endpoint-Änderungen.** Scale-up/-down
  oder Rollout von `service-a` während offener Requests kann den Hash
  verschieben. Der Callback landet dann auf dem falschen Pod. Robustere
  Alternativen für Produktion:
  - Reply-To-Adresse: `service-a` gibt seine Pod-Adresse (Headless Service) als
    Callback-URL mit.
  - Shared Pub/Sub: Callback darf auf jedem Pod landen, das Ergebnis wird per
    Redis Pub/Sub oder Reply-Topic an den wartenden Pod verteilt.
- **Pod-Shutdown:** `terminationGracePeriodSeconds` und
  `proxy.istio.io/config: terminationDrainDuration` müssen größer als
  `APP_TIMEOUT` sein, sonst reißen gehaltene Verbindungen beim Rollout ab.
- **Ressourcen:** Viele gleichzeitig gehaltene Verbindungen zählen gegen
  `connectionPool`-Limits (`http1MaxPendingRequests`, `maxConnections`) einer
  eventuell vorhandenen DestinationRule.
- **Idempotenz:** Auch mit `retries.attempts: 0` kann ein Client selbst erneut
  senden. Die `correlationId` sollte in `service-b` als Idempotency-Key dienen.

## Optionale Erweiterungen

- `x-request-id` (vom Gateway erzeugt) statt eines Client-Headers als
  Correlation-ID verwenden. Zu prüfen ist, ob der Sidecar von `service-b` eine
  explizit gesetzte `x-request-id` beim Callback unverändert durchreicht.
- Zweite Variante mit Reply-To über Headless Service als Vergleich zum
  Consistent Hashing.
- Rollout-Test: `kubectl rollout restart deployment/service-a` während laufender
  Requests, um die Grenze des Consistent Hashing sichtbar zu machen.

## Umsetzungsschritte

1. Manifeste `00`–`13` (Namespace, ConfigMaps mit `bridge.py`/`worker.py`,
   Deployments, Services)
2. Gateway, VirtualServices (`20`–`22`) und DestinationRule (`30`)
3. `install.sh` / `uninstall.sh` nach dem Muster von `faultInjection/`
   (Namespace-Parameter, `${NAMESPACE}`-Rendering per `sed`, `30-*` beim
   Install überspringen)
4. `test/client-pod.yaml` und `run.sh` mit den Testfällen 1–5
5. readme vom Plan in die finale Doku überführen (Installation, Test,
   Troubleshooting, Aufräumen) und Eintrag in der Root-[`readme.md`](../readme.md)
   ergänzen
