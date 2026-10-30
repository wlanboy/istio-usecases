# CronJob im Wartungsmodus

Ein Kubernetes-CronJob ruft jede Minute
`http://service.service.svc.cluster.local/healthz` mit dem Image
`curlimages/curl` auf. Den CronJob muss man für die Wartung nicht anfassen. Er
läuft im Mesh, also gelten für ihn dieselben Wartungs-VirtualServices wie für
den `client`. Während der Wartung bekommt curl `503` mit `Retry-After: 300`.
Das Skript im CronJob wertet diese Antwort als "Wartung, Lauf übersprungen" und
endet mit Exit-Code 0. Der Job wird `Complete` statt `Failed`, es gibt keine
Retries und keinen Alarm.

## Ablauf

```
Ohne manifestwartung/:
  healthz-cron (curl) ──Sidecar──▶ service    200                -> "OK", Exit 0

Mit manifestwartung/:
  healthz-cron (curl) ──Sidecar──▶ (nicht erreicht)
        ▲                │
        └── 503 + Retry-After: 300 aus dem Sidecar   -> "WARTUNG", Exit 0

Echter Fehler (z.B. Service-Pod nicht bereit):
  503 ohne Retry-After, 5xx, Timeout                  -> "FEHLER", Exit 1, Job Failed
```

Das Skript unterscheidet die Fälle so:

| Antwort                  | Herkunft                                | Ergebnis             |
|--------------------------|-----------------------------------------|----------------------|
| `200`                    | Service-Pod                             | Exit 0               |
| `503` mit `Retry-After`  | `directResponse` aus `manifestwartung/` | Exit 0, übersprungen |
| `503` ohne `Retry-After` | Envoy, z.B. `no healthy upstream`       | Exit 1               |
| alles andere, auch `000` | Fehler oder keine Verbindung            | Exit 1               |

Der `Retry-After`-Header ist das Merkmal der Wartung. Wer ihn in
`manifestwartung/` entfernt, muss auch das Skript in `cronjob.yaml` anpassen,
sonst endet der CronJob während der Wartung mit `Failed`.

## Voraussetzungen

- Der Usecase aus [../readme.md](../readme.md) ist installiert (`../setup.sh`).
- Das Image `curlimages/curl:8.11.1` ist von Docker Hub erreichbar oder per
  `kind load docker-image` geladen:

  ```bash
  docker pull curlimages/curl:8.11.1
  kind load docker-image curlimages/curl:8.11.1 --name local
  ```

## Warum im Namespace client

Den VirtualService wertet der Envoy-Sidecar des Aufrufers aus. Ein curl-Pod
ohne Sidecar geht direkt zum Service-Pod und bekommt während der Wartung `200`.
Der CronJob liegt deshalb im Namespace `client`, der `istio-injection: enabled`
hat. Er läuft in jedem anderen Namespace mit Sidecar-Injection genauso.

## Sidecar in einem Job

Ein klassischer Istio-Sidecar macht bei Jobs zwei Probleme:

- curl startet vor Envoy, der erste Request läuft ins Leere.
- Wenn curl fertig ist, läuft `istio-proxy` weiter. Der Pod endet nie und der
  Job wird nie `Complete`.

Die Annotation `sidecar.istio.io/nativeSidecar: "true"` im Pod-Template löst
beides. Istio hängt `istio-proxy` dann als Init-Container mit
`restartPolicy: Always` ein, also als nativen Kubernetes-Sidecar. Kubernetes
startet curl erst, wenn der Proxy bereit ist, und beendet den Proxy, sobald curl
fertig ist.

## Installation

```bash
kubectl apply -f cronjob.yaml
kubectl -n client get cronjob healthz-cron
```

Weitere Einstellungen in `cronjob.yaml`:

- `concurrencyPolicy: Forbid` startet keinen neuen Lauf, solange der vorherige
  noch läuft.
- `backoffLimit: 0` wiederholt einen fehlgeschlagenen Lauf nicht. Der nächste
  reguläre Lauf kommt nach einer Minute.
- `activeDeadlineSeconds: 60` bricht einen hängenden Lauf nach 60 Sekunden ab.
- `ttlSecondsAfterFinished: 600` löscht beendete Jobs nach 10 Minuten.

## Test

Statt auf den nächsten Zeitpunkt zu warten, einen Job aus dem CronJob starten:

```bash
kubectl -n client create job healthz-manuell --from=cronjob/healthz-cron
kubectl -n client wait --for=condition=complete job/healthz-manuell --timeout=60s
kubectl -n client logs job/healthz-manuell -c curl
kubectl -n client delete job healthz-manuell
```

Erwartete Ausgabe **ohne** Wartung:

```
OK: http://service.service.svc.cluster.local/healthz liefert 200
```

Wartung einschalten und denselben Test wiederholen:

```bash
../wartung-an.sh
kubectl -n client create job healthz-manuell --from=cronjob/healthz-cron
kubectl -n client wait --for=condition=complete job/healthz-manuell --timeout=60s
kubectl -n client logs job/healthz-manuell -c curl
kubectl -n client delete job healthz-manuell
```

Erwartete Ausgabe **mit** Wartung:

```
WARTUNG: http://service.service.svc.cluster.local/healthz ist in Wartung (Retry-After: 300s), Lauf übersprungen
```

Die regulären Läufe zeigen dasselbe:

```bash
kubectl -n client get cronjob,jobs
kubectl -n client logs -l app=healthz-cron -c curl --prefix
```

Wartung beenden, ab dem nächsten Lauf kommt wieder `OK`:

```bash
../wartung-aus.sh
```

## CronJob trotz Wartung laufen lassen

Soll ein CronJob auch während der Wartung den echten Service erreichen, etwa
als Prüfung für das Wartungsteam, schickt er den Bypass-Header mit. Dafür im
Skript in `cronjob.yaml` den curl-Aufruf erweitern:

```sh
result=$(curl -s -o /dev/null -H 'x-wartung-bypass: true' -w '%{http_code} %header{retry-after}' "${SERVICE_URL}")
```

## Alternative: CronJob anhalten

Statt die Wartung im Skript auszuwerten, kann `wartung-an.sh` den CronJob
anhalten und `wartung-aus.sh` ihn wieder starten:

```bash
kubectl -n client patch cronjob healthz-cron -p '{"spec":{"suspend":true}}'
kubectl -n client patch cronjob healthz-cron -p '{"spec":{"suspend":false}}'
```

Das hat Nachteile:

- Die Wartung besteht dann aus Istio-Manifesten plus einem Eingriff an jedem
  CronJob, der den Service aufruft. Die Skripte müssen alle diese CronJobs
  kennen.
- Wer die Wartung nur mit `kubectl apply -f manifestwartung/` einschaltet,
  vergisst die CronJobs.
- Ohne `startingDeadlineSeconds` startet Kubernetes direkt nach dem Fortsetzen
  den zuletzt verpassten Lauf nach.

Wertet das Skript die Antwort aus, schaltet `manifestwartung/` alle Aufrufer
zusammen um, CronJobs eingeschlossen. Deshalb nutzt dieser Usecase den Weg über
das Skript.

## Troubleshooting

- **Job bleibt `Running`, Pod zeigt `1/2`.** Der Sidecar läuft nicht als
  nativer Sidecar. Prüfen, ob `istio-proxy` unter den Init-Containern steht:
  ```bash
  kubectl -n client get pod -l app=healthz-cron -o jsonpath='{.items[0].spec.initContainers[*].name}'
  ```
- **Während der Wartung kommt `OK` statt `WARTUNG`.** Der Pod hat keinen
  Sidecar oder istiod hat die Route noch nicht verteilt. Container des Pods
  prüfen und die Schritte unter Troubleshooting in [../readme.md](../readme.md)
  durchgehen.
- **`FEHLER: ... liefert HTTP 000`.** curl hat keine Verbindung bekommen, meist
  DNS oder ein Netzwerkproblem. Bei einem klassischen Sidecar ohne
  `nativeSidecar` kann es auch sein, dass curl vor Envoy gestartet ist.

## Aufräumen

```bash
kubectl delete -f cronjob.yaml --ignore-not-found
```

`../delete.sh` löscht den Namespace `client` und damit auch den CronJob.
