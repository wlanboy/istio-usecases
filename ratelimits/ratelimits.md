# Rate Limiting mit Istio: Leitfaden für Entwickler

Dieses Dokument erklärt, was das lokale Rate Limit aus diesem Usecase technisch macht
und wie du es an ein Deployment hängst, das bereits im Cluster läuft. Die Demo selbst
(Installation, Lasttest) beschreibt [readme.md](readme.md).

## Wie es funktioniert

Jeder Pod im Mesh hat einen Envoy-Sidecar (`istio-proxy`). Eingehender Traffic läuft
erst durch diesen Sidecar und danach in deinen Container. Ein `EnvoyFilter` hängt in
die HTTP-Filterkette dieses Sidecars einen zusätzlichen Filter ein:
`envoy.filters.http.local_ratelimit`.

```
Client ──► Service ──► Pod
                       ┌───────────────────────────────────────────┐
                       │ istio-proxy (Envoy)                       │
                       │   local_ratelimit ──► Token übrig?        │
                       │        │ ja                 │ nein        │
                       │        ▼                    ▼             │
                       │   weiter an App        429 an Client      │
                       │                                           │
                       │ App-Container (z.B. nginx :8080)          │
                       └───────────────────────────────────────────┘
```

Der Filter arbeitet mit einem Token Bucket:

- Der Bucket fasst höchstens `max_tokens` Tokens. Das ist gleichzeitig die Burst-Größe.
- Jeder Request verbraucht ein Token.
- Alle `fill_interval` legt Envoy `tokens_per_fill` neue Tokens nach, maximal bis `max_tokens`.
- Ist der Bucket leer, antwortet Envoy sofort mit `429 Too Many Requests` und setzt den
  Header `x-local-rate-limit: true`. Der Request erreicht deine Anwendung nicht.

In diesem Usecase gilt `max_tokens: 5`, `tokens_per_fill: 5`, `fill_interval: 30s`.
Pro Pod gehen also 5 Requests alle 30 Sekunden durch.

### Was "lokal" bedeutet

Jeder Sidecar hat seinen eigenen Bucket. Die Pods wissen nichts voneinander. Daraus folgt:

- Das effektive Limit für den Service ist `Limit pro Pod × Anzahl Replicas`. Bei 2
  Replicas und 5 Tokens sind es bis zu 10 Requests pro 30 Sekunden.
- Skaliert ein HPA von 2 auf 6 Pods, verdreifacht sich das Gesamtlimit, ohne dass jemand
  das EnvoyFilter anfasst.
- Welcher Pod einen Request bekommt, entscheidet das Load Balancing. Ein einzelner Client
  kann deshalb schon ein 429 sehen, während ein anderer Pod noch Tokens hat.

Das lokale Limit schützt einen einzelnen Pod vor Überlast. Wenn du ein festes Limit
für den ganzen Service brauchst, etwa "100 Requests pro Minute pro API-Key", reicht es
nicht. Dafür gibt es das globale Rate Limit mit Redis in
[`../ratelimitService/`](../ratelimitService/readme.md).

### Wer betroffen ist und wer nicht

- Der Filter sitzt auf dem eingehenden Traffic (`context: SIDECAR_INBOUND`) des Ziel-Pods.
  Der aufrufende Client braucht keinen Sidecar. Im Lasttest dieses Usecases läuft der
  Client bewusst ohne.
- Das Limit gilt für alle HTTP-Requests, die über den Sidecar in den Pod kommen, egal
  über welchen Pfad.
- Liveness- und Readiness-Probes zählen nicht mit, solange Istio die Probes umschreibt
  (Standardverhalten). Sie laufen dann über den pilot-agent auf Port 15020 und nicht
  durch den Inbound-Listener.
- Ein 429 wird von Istio nicht automatisch wiederholt. Die Standard-Retry-Policy einer
  VirtualService enthält 429 nicht. Dein Client muss 429 selbst behandeln, am besten
  mit Backoff.
- Das funktioniert nur im Sidecar-Modus. Im Ambient-Modus gibt es keinen Sidecar, an den
  sich der Filter hängen kann.

## Rate Limit zu einem bestehenden Deployment hinzufügen

Annahme für die Beispiele: Dein Deployment heißt `my-app` und läuft im Namespace `my-ns`.

### 1. Prüfen, ob der Pod einen Sidecar hat

```bash
kubectl -n my-ns get pods -l app=my-app \
  -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.spec.containers[*].name}{"\n"}{end}'
```

In der Ausgabe muss `istio-proxy` neben deinem Container stehen. Fehlt er, aktiviere
die Injection für den Namespace und starte das Deployment neu:

```bash
kubectl label namespace my-ns istio-injection=enabled --overwrite
kubectl -n my-ns rollout restart deployment/my-app
```

Ohne Sidecar ignoriert Istio das EnvoyFilter. Es gibt dabei keine Fehlermeldung.

### 2. Labels der Pods ermitteln

Das EnvoyFilter wählt Pods über `workloadSelector.labels` aus. Maßgeblich sind die Labels
im Pod-Template, nicht die am Deployment selbst:

```bash
kubectl -n my-ns get deployment my-app -o jsonpath='{.spec.template.metadata.labels}'
```

Nimm ein Label, das nur auf deine Pods passt. `app: my-app` ist meistens richtig.
Ein zu breiter Selector wie `team: backend` drosselt alle Workloads mit diesem Label.
Lässt du `workloadSelector` ganz weg, gilt der Filter für jeden Pod im Namespace.

### 3. EnvoyFilter anlegen

Kopiere [manifests/20-envoyfilter-local-ratelimit.yaml](manifests/20-envoyfilter-local-ratelimit.yaml)
und passe vier Stellen an:

| Feld | Beispiel | Bedeutung |
|---|---|---|
| `metadata.name` | `my-app-local-ratelimit` | eindeutiger Name im Namespace |
| `metadata.namespace` | `my-ns` | derselbe Namespace wie das Deployment |
| `spec.workloadSelector.labels` | `app: my-app` | Labels aus Schritt 2 |
| `token_bucket` | siehe unten | das eigentliche Limit |

Das EnvoyFilter muss im Namespace des Deployments liegen. Ein `workloadSelector` wirkt
nur innerhalb des eigenen Namespace.

Gekürzte Fassung ohne die Kommentare aus der Vorlage:

```yaml
apiVersion: networking.istio.io/v1alpha3
kind: EnvoyFilter
metadata:
  name: my-app-local-ratelimit
  namespace: my-ns
spec:
  workloadSelector:
    labels:
      app: my-app
  configPatches:
    - applyTo: HTTP_FILTER
      match:
        context: SIDECAR_INBOUND
        listener:
          filterChain:
            filter:
              name: "envoy.filters.network.http_connection_manager"
      patch:
        operation: INSERT_BEFORE
        value:
          name: envoy.filters.http.local_ratelimit
          typed_config:
            "@type": type.googleapis.com/udpa.type.v1.TypedStruct
            type_url: type.googleapis.com/envoy.extensions.filters.http.local_ratelimit.v3.LocalRateLimit
            value:
              stat_prefix: http_local_rate_limiter
              token_bucket:
                max_tokens: 50
                tokens_per_fill: 10
                fill_interval: 1s
              filter_enabled:
                runtime_key: local_rate_limit_enabled
                default_value:
                  numerator: 100
                  denominator: HUNDRED
              filter_enforced:
                runtime_key: local_rate_limit_enforced
                default_value:
                  numerator: 100
                  denominator: HUNDRED
              response_headers_to_add:
                - append: false
                  header:
                    key: x-local-rate-limit
                    value: "true"
```

Leg die Datei neben die übrigen Manifeste deines Deployments und wende sie an:

```bash
kubectl apply -f my-app-local-ratelimit.yaml
```

Einen Neustart der Pods brauchst du dafür nicht. istiod schickt die geänderte
Konfiguration innerhalb weniger Sekunden an die laufenden Sidecars.

### 4. Werte wählen

Rechne vom gewünschten Dauerdurchsatz pro Pod rückwärts:

- `tokens_per_fill / fill_interval` ist die Dauerrate. `10` pro `1s` ergibt 10 Requests
  pro Sekunde und Pod.
- `max_tokens` ist der Spielraum für Lastspitzen. Mit `50` darf ein Pod nach einer
  ruhigen Phase 50 Requests auf einmal annehmen.
- Gleiche `max_tokens` und `tokens_per_fill` wie in der Demo ergeben ein festes Fenster:
  N Requests, dann warten bis zum nächsten Intervall.
- Ein kurzes `fill_interval` (1s) verteilt die Last gleichmäßiger als ein langes (30s).
  Bei 30s sammelt der Client nach dem Burst bis zu 30 Sekunden lang nur 429.

Rechne zum Schluss mit der minimalen und maximalen Replica-Zahl nach, was das für den
ganzen Service bedeutet.

### 5. Prüfen, ob der Filter aktiv ist

Steckt der Filter in der Konfiguration des Sidecars?

```bash
POD=$(kubectl -n my-ns get pod -l app=my-app -o jsonpath='{.items[0].metadata.name}')
istioctl proxy-config listener "$POD" -n my-ns -o json | grep -c local_ratelimit
```

Eine Zahl größer 0 heißt, der Filter ist da. Bei 0 stimmen meist `workloadSelector`
oder Namespace nicht, oder der Pod hat keinen Sidecar.

Greift das Limit? Schick Requests an den Service, hier angenommen als `my-app`, und zähl die Statuscodes:

```bash
kubectl -n my-ns run rl-test --rm -it --restart=Never \
  --image=curlimages/curl:8.11.0 \
  --overrides='{"metadata":{"annotations":{"sidecar.istio.io/inject":"false"}}}' \
  -- sh -c 'for i in $(seq 1 100); do curl -s -o /dev/null -w "%{http_code}\n" http://my-app.my-ns.svc.cluster.local; done | sort | uniq -c'
```

Für den Namespace aus der Demo erledigt das [run.sh](run.sh).

Die Envoy-Statistiken zeigen, wie oft der Filter eingegriffen hat:

```bash
kubectl -n my-ns exec "$POD" -c istio-proxy -- \
  pilot-agent request GET stats | grep http_local_rate_limit
```

Relevant sind `.ok` (durchgelassen), `.rate_limited` (Limit überschritten) und
`.enforced` (tatsächlich mit 429 beantwortet). Liefert der Befehl nichts, filtert Istio
diese Statistiken weg. Dann diese Annotation ins Pod-Template des Deployments setzen
(braucht einen Rollout):

```yaml
spec:
  template:
    metadata:
      annotations:
        proxy.istio.io/config: |
          proxyStatsMatcher:
            inclusionRegexps:
              - ".*http_local_rate_limit.*"
```

### 6. Erst beobachten, dann blockieren

Bei einem Deployment mit echtem Traffic weißt du vorher oft nicht, wie viele Requests
ein Pod bekommt. Setze dann zuerst `filter_enforced` auf 0:

```yaml
              filter_enforced:
                runtime_key: local_rate_limit_enforced
                default_value:
                  numerator: 0
                  denominator: HUNDRED
```

Der Filter zählt weiter und erhöht `.rate_limited`, lässt aber jeden Request durch.
Beobachte den Zähler ein paar Tage unter normaler Last. Bleibt er bei 0 oder nahe 0,
setz `numerator` auf 100 und wende das Manifest erneut an.

### 7. Wieder entfernen

```bash
kubectl -n my-ns delete envoyfilter my-app-local-ratelimit
```

Die Sidecars verlieren den Filter ohne Neustart.

## Typische Fehler

| Symptom | Ursache |
|---|---|
| Nie ein 429 | Pod ohne Sidecar, `workloadSelector` passt nicht, EnvoyFilter im falschen Namespace |
| 429 auf ganz anderen Services | `workloadSelector` zu breit oder weggelassen |
| Limit greift später als erwartet | Limit gilt pro Pod, mehrere Replicas teilen sich die Last |
| Lange Phasen mit nur 429 | `fill_interval` zu lang, kürzer wählen und Werte entsprechend skalieren |
| EnvoyFilter angewendet, Sidecar lehnt Konfiguration ab | Tippfehler im `typed_config`, mit `istioctl analyze -n my-ns` und den Logs von istiod prüfen |

## Weiterführend

- Limit nur für bestimmte Pfade, etwa `/api/*`: Envoy erlaubt das über einen zusätzlichen
  `HTTP_ROUTE`-Patch mit `typed_per_filter_config`. Beispiel in der
  [Istio-Doku zu Rate Limits](https://istio.io/latest/docs/tasks/policy-enforcement/rate-limit/).
- Ein Limit über alle Pods hinweg, auch nach Header oder Client: [`../ratelimitService/`](../ratelimitService/readme.md).
- Alle Felder des Filters: [Envoy Local Rate Limit](https://www.envoyproxy.io/docs/envoy/latest/configuration/http/http_filters/local_rate_limit_filter).
