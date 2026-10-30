# CronJob im Wartungsmodus

Ein Kubernetes-CronJob ruft jede Minute
`http://service.service.svc.cluster.local/healthz` mit dem Image
`curlimages/curl` auf. Während der Wartung soll er gar nicht erst starten.
Dafür gibt es zwei Stufen:

1. **CronJob anhalten.** `wartung-an.sh` setzt `spec.suspend: true` an allen
   CronJobs mit dem Label `wartung: service` und löscht deren laufende Jobs.
   Der CronJob-Controller legt dann keine neuen Jobs an. `wartung-aus.sh` setzt
   die CronJobs wieder fort, sobald der Client wieder 200 bekommt.
2. **503 im Skript auswerten.** Startet trotzdem ein Job, etwa ein manuell
   angelegter oder einer, der vor dem Anhalten schon lief, landet sein Aufruf
   im Wartungs-VirtualService. Der CronJob läuft im Mesh, curl bekommt `503` mit
   `Retry-After: 300`. Das Skript wertet das als "Wartung, Lauf übersprungen"
   und endet mit Exit-Code 0, der Job wird `Complete` statt `Failed`.

Stufe 1 braucht die Skripte. Wer die Wartung nur mit
`kubectl apply -f manifestwartung/` einschaltet, hält keine CronJobs an. Dann
greift nur Stufe 2.

## Ablauf

```
wartung-an.sh
  1. CronJobs mit Label wartung=service: spec.suspend=true
  2. laufende Jobs mit Label wartung=service: löschen
  3. kubectl apply -f manifestwartung/, warten bis client -> service 503 liefert

während der Wartung
  CronJob-Controller legt keine Jobs an
  manuell gestarteter Job: curl -> Sidecar -> 503 + Retry-After   -> "WARTUNG", Exit 0

wartung-aus.sh
  1. kubectl delete -f manifestwartung/, warten bis client -> service 200 liefert
  2. CronJobs mit Label wartung=service: spec.suspend=false
```

`wartung-aus.sh` setzt die CronJobs erst fort, wenn der Client-Sidecar die
Wartungsroute nicht mehr hat. Sonst könnte der erste Lauf noch in die 503
laufen. Bekommt der Client nach 30 Sekunden kein 200, bleiben die CronJobs
angehalten und das Skript endet mit Exit-Code 1.

## Label wartung=service

Die Skripte suchen die CronJobs clusterweit über das Label. Ein neuer CronJob,
der während der Wartung pausieren soll, braucht nur das Label, in
`wartung-an.sh` und `wartung-aus.sh` ändert sich nichts. In `cronjob.yaml`
steht das Label zweimal:

- unter `metadata.labels` des CronJobs. Darüber finden die Skripte den
  CronJob für `suspend`.
- unter `spec.jobTemplate.metadata.labels`, damit jeder Job das Label erbt.
  Darüber findet `wartung-an.sh` die laufenden Jobs.

Alle CronJobs mit dem Label anzeigen:

```bash
kubectl get cronjob -A -l wartung=service
```

## Verpasste Läufe

Kubernetes zählt Zeitpunkte, die in die Pause fallen, als verpasste Läufe. Ohne
`startingDeadlineSeconds` startet der Controller direkt nach dem Fortsetzen den
zuletzt verpassten Lauf. `cronjob.yaml` setzt `startingDeadlineSeconds: 30`:
Ein verpasster Lauf wird nur nachgeholt, wenn sein Zeitpunkt höchstens 30
Sekunden zurückliegt. Im Test hat der Controller nach einer Pause von zwei
Minuten keinen Lauf nachgeholt, der nächste reguläre Lauf kam zur nächsten
vollen Minute.

Werte unter 10 Sekunden nicht verwenden. Der Controller prüft nur alle paar
Sekunden, mit einem zu kleinen Wert verpasst er auch reguläre Läufe.

## Antworten im Skript

Das Skript im CronJob unterscheidet die Fälle so:

| Antwort                  | Herkunft                                | Ergebnis             |
|--------------------------|-----------------------------------------|----------------------|
| `200`                    | Service-Pod                             | Exit 0               |
| `503` mit `Retry-After`  | `directResponse` aus `manifestwartung/` | Exit 0, übersprungen |
| `503` ohne `Retry-After` | Envoy, z.B. `no healthy upstream`       | Exit 1               |
| alles andere, auch `000` | Fehler oder keine Verbindung            | Exit 1               |

Der `Retry-After`-Header ist das Merkmal der Wartung. Wer ihn in
`manifestwartung/` entfernt, muss auch das Skript in `cronjob.yaml` anpassen,
sonst endet ein Job während der Wartung mit `Failed`.

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

### Ohne Wartung

Statt auf den nächsten Zeitpunkt zu warten, einen Job aus dem CronJob starten:

```bash
kubectl -n client create job healthz-manuell --from=cronjob/healthz-cron
kubectl -n client wait --for=condition=complete job/healthz-manuell --timeout=60s
kubectl -n client logs job/healthz-manuell -c curl
kubectl -n client delete job healthz-manuell
```

Erwartete Ausgabe:

```
OK: http://service.service.svc.cluster.local/healthz liefert 200
```

### Wartung bricht laufende Jobs ab

Einen lang laufenden Job mit dem Label anlegen, der einen Lauf während des
Wartungsbeginns darstellt, dann die Wartung einschalten:

```bash
kubectl -n client create job sleep-test --image=curlimages/curl:8.11.1 -- sleep 300
kubectl -n client label job sleep-test wartung=service
../wartung-an.sh
kubectl -n client get cronjob,jobs
```

Erwartete Ausgabe von `wartung-an.sh`:

```
==> Halte CronJobs mit Label wartung=service an (spec.suspend=true)
cronjob.batch/healthz-cron patched
==> Breche laufende Jobs mit Label wartung=service ab
job.batch "sleep-test" deleted from client namespace
==> Aktiviere Wartungsmodus für 'service' (VirtualServices aus manifestwartung/)
...
==> Wartungsmodus ist aktiv: client -> service liefert 503.
```

`kubectl get cronjob` zeigt danach `SUSPEND True`.

### Keine neuen Läufe während der Wartung

Zwei Minuten lang auf neue Jobs warten. Die Ausgabe bleibt leer:

```bash
timeout 130 kubectl -n client get jobs --watch-only -o name
```

Ein manuell gestarteter Job läuft trotz `suspend`, `suspend` gilt nur für den
Zeitplan. Hier greift Stufe 2:

```bash
kubectl -n client create job healthz-manuell --from=cronjob/healthz-cron
kubectl -n client wait --for=condition=complete job/healthz-manuell --timeout=60s
kubectl -n client logs job/healthz-manuell -c curl
kubectl -n client delete job healthz-manuell
```

Erwartete Ausgabe:

```
WARTUNG: http://service.service.svc.cluster.local/healthz ist in Wartung (Retry-After: 300s), Lauf übersprungen
```

### Wartung beenden

```bash
../wartung-aus.sh
kubectl -n client get cronjob healthz-cron
```

Erwartete Ausgabe von `wartung-aus.sh`:

```
==> Beende Wartungsmodus für 'service' (entferne VirtualServices aus manifestwartung/)
...
==> Wartungsmodus ist aus: client -> service liefert 200.
==> Setze CronJobs mit Label wartung=service fort (spec.suspend=false)
cronjob.batch/healthz-cron patched
```

Der nächste reguläre Lauf liefert wieder `OK`:

```bash
kubectl -n client logs -l app=healthz-cron -c curl --prefix --tail=1
```

## CronJob trotz Wartung laufen lassen

Soll ein CronJob auch während der Wartung den echten Service erreichen, etwa
als Prüfung für das Wartungsteam, bekommt er das Label `wartung: service`
nicht. Zusätzlich schickt er den Bypass-Header mit, dafür im Skript in
`cronjob.yaml` den curl-Aufruf erweitern:

```sh
result=$(curl -s -o /dev/null -H 'x-wartung-bypass: true' -w '%{http_code} %header{retry-after}' "${SERVICE_URL}")
```

## Troubleshooting

- **CronJob startet während der Wartung weiter Jobs.** Das Label fehlt am
  CronJob oder die Wartung wurde ohne `wartung-an.sh` eingeschaltet. Prüfen:
  ```bash
  kubectl get cronjob -A -l wartung=service
  kubectl -n client get cronjob healthz-cron -o jsonpath='{.spec.suspend}{"\n"}'
  ```
- **CronJob bleibt nach der Wartung angehalten.** `wartung-aus.sh` ist mit
  Exit-Code 1 beendet, weil der Client kein 200 bekam. Ursache unter
  Troubleshooting in [../readme.md](../readme.md) suchen und das Skript erneut
  starten. Von Hand fortsetzen:
  ```bash
  kubectl -n client patch cronjob healthz-cron --type merge -p '{"spec":{"suspend":false}}'
  ```
- **Laufender Job wird beim Wartungsbeginn nicht abgebrochen.** Der Job hat
  das Label nicht. Bei Jobs aus dem CronJob fehlt es dann unter
  `spec.jobTemplate.metadata.labels`.
- **Job bleibt `Running`, Pod zeigt `1/2`.** Der Sidecar läuft nicht als
  nativer Sidecar. Prüfen, ob `istio-proxy` unter den Init-Containern steht:
  ```bash
  kubectl -n client get pod -l app=healthz-cron -o jsonpath='{.items[0].spec.initContainers[*].name}'
  ```
- **Manueller Job zeigt während der Wartung `OK` statt `WARTUNG`.** Der Pod
  hat keinen Sidecar oder istiod hat die Route noch nicht verteilt. Container
  des Pods prüfen und die Schritte unter Troubleshooting in
  [../readme.md](../readme.md) durchgehen.
- **`FEHLER: ... liefert HTTP 000`.** curl hat keine Verbindung bekommen, meist
  DNS oder ein Netzwerkproblem. Bei einem klassischen Sidecar ohne
  `nativeSidecar` kann es auch sein, dass curl vor Envoy gestartet ist.

## Aufräumen

```bash
kubectl delete -f cronjob.yaml --ignore-not-found
```

`../delete.sh` löscht den Namespace `client` und damit auch den CronJob.
