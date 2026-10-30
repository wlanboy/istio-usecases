# Wartungsmodus mit Argo CD

Derselbe Usecase wie in [../readme.md](../readme.md), aber deployt als App of
Apps in Argo CD. Der Wartungsmodus hängt an einem einzigen Helm-Parameter an
der Root-App. Ein- und Ausschalten braucht keinen Commit. Der automatische
Sync bleibt dabei an.

## Aufbau

```
wartung-root  (argocd/root-app.yaml, Helm-Chart argocd/apps/, Parameter wartung)
  ├── Namespaces client, service       (Sync-Welle -1, istio-injection: enabled)
  ├── wartung-client       -> charts/client        Namespace client
  ├── wartung-service      -> charts/service       Namespace service
  ├── wartung-healthz-cron -> cron/ (Kustomize)    Namespace client
  │                           Patch: spec.suspend = wartung
  └── wartung-aktiv        -> manifestwartung/     Namespace service
                              nur bei wartung=true
```

Der Parameter `wartung` steuert zwei Dinge:

| `wartung` | `wartung-aktiv`                          | CronJob `healthz-cron` |
|-----------|------------------------------------------|------------------------|
| `false`   | gibt es nicht                            | `suspend: false`       |
| `true`    | deployt die beiden Wartungs-VirtualServices | `suspend: true`     |

Beim Ausschalten entfernt `wartung-root` die App `wartung-aktiv` (Prune). Der
Finalizer `resources-finalizer.argocd.argoproj.io` löscht dabei die beiden
VirtualServices. Die Manifeste unter `manifestwartung/` und die Charts sind
dieselben wie beim Setup mit Skripten. Für Argo CD kommt nur
`cron/kustomization.yaml` dazu, damit Argo CD den CronJob per Kustomize-Patch
anhalten kann.

Alle Kind-Apps haben `automated` mit `prune` und `selfHeal`. Wer im Cluster von
Hand etwas ändert, etwa `suspend` am CronJob, wird nach ein paar Sekunden von
Argo CD zurückgesetzt.

## Dateien

| Pfad                                    | Inhalt                                                       |
|-----------------------------------------|--------------------------------------------------------------|
| `root-app.yaml`                         | Application `wartung-root`, `setup.sh` legt sie per CLI an    |
| `apps/values.yaml`                      | `wartung: false`, Git-Quelle der Kind-Apps                   |
| `apps/templates/namespaces.yaml`        | Namespaces `client` und `service`                            |
| `apps/templates/app-client.yaml`        | Application `wartung-client`                                 |
| `apps/templates/app-service.yaml`       | Application `wartung-service`                                |
| `apps/templates/app-healthz-cron.yaml`  | Application `wartung-healthz-cron` mit `suspend`-Patch       |
| `apps/templates/app-wartung-aktiv.yaml` | Application `wartung-aktiv`, nur bei `wartung=true`          |
| `login.sh`                              | argocd-CLI mit dem initialen admin-Passwort anmelden         |
| `setup.sh` / `delete.sh`                | Installation / Deinstallation                                |
| `../cron/kustomization.yaml`            | Kustomize-Hülle um `cronjob.yaml` für den Patch              |

## Voraussetzungen

- Usecase-Voraussetzungen aus [../readme.md](../readme.md): kind-Cluster mit
  Istio, Ingress-Gateway auf hostPort 80.
- Argo CD im Namespace `argocd` mit `server.insecure: true`, erreichbar über
  das Istio Ingress-Gateway unter <http://argocd.localhost>.
- `argocd`-CLI und `kubectl` mit Kontext auf den kind-Cluster.
- Die Dateien aus `argocd/` und `cron/kustomization.yaml` sind gepusht. Argo CD
  holt alles aus `https://github.com/wlanboy/istio-usecases.git`, Branch `main`.
  Für einen anderen Branch oder Fork gibt es `REPO_URL` und `REVISION`, siehe
  [Installation](#installation). Ohne Push geht es mit einem Git-Server im
  Cluster, siehe [Test ohne Push](#test-ohne-push).
- Der Usecase ist nicht schon per `../setup.sh` installiert. Sonst vorher
  `../delete.sh` ausführen.

## Installation

```bash
./setup.sh
```

Führt intern aus:

```bash
# login.sh
argocd login argocd.localhost:80 --plaintext --grpc-web --username admin \
  --password "$(argocd admin initial-password -n argocd | head -n1)"

# nur wenn wartung-root noch nicht existiert
argocd app create -f root-app.yaml

argocd app wait wartung-root --sync --health --timeout 180
argocd app wait wartung-client wartung-service wartung-healthz-cron --sync --health --timeout 180
argocd app list
```

Gibt es `wartung-root` schon, legt `setup.sh` sie nicht neu an. Ein erneuter
Aufruf setzt den Wartungsschalter also nicht zurück.

Mit anderer Git-Quelle:

```bash
REPO_URL=https://github.com/<user>/istio-usecases.git REVISION=mein-branch ./setup.sh
```

`setup.sh` hängt dann an `argocd app create` an:

```bash
--repo "${REPO_URL}" --revision "${REVISION}" \
--helm-set repoURL="${REPO_URL}" --helm-set targetRevision="${REVISION}"
```

`--repo` und `--revision` gelten für `wartung-root`, die beiden Helm-Werte für
die Kind-Apps.

Erwartete Ausgabe am Ende:

```
NAME                         ...  STATUS  HEALTH   SYNCPOLICY  ...  PATH
argocd/wartung-client        ...  Synced  Healthy  Auto-Prune  ...  wartung/charts/client
argocd/wartung-healthz-cron  ...  Synced  Healthy  Auto-Prune  ...  wartung/cron
argocd/wartung-root          ...  Synced  Healthy  Auto-Prune  ...  wartung/argocd/apps
argocd/wartung-service       ...  Synced  Healthy  Auto-Prune  ...  wartung/charts/service
```

Danach läuft der Test aus dem Usecase:

```bash
../run.sh
```

Die letzte Zeile lautet `==> Wartungsmodus ist NICHT aktiv: alle Aufrufe erwarten 200.`

## Wartungsmodus aktivieren

```bash
./login.sh
argocd app set wartung-root --helm-set wartung=true
```

`argocd app set` schreibt den Wert als Parameter-Override in
`spec.source.helm.parameters` von `wartung-root`, nicht ins Git. Argo CD
erkennt die geänderte Spec und synct automatisch:

1. `wartung-root` legt die App `wartung-aktiv` an. Die deployt
   `service-wartung` und `service-wartung-ingress` in den Namespace `service`.
2. `wartung-healthz-cron` bekommt per Patch `suspend: true`.
3. istiod verteilt die neuen Routen an die Sidecars und das Ingress-Gateway.

Im Test kam der Client nach 4 Sekunden auf 503. Prüfen:

```bash
../run.sh
argocd app list -o name
argocd app get wartung-root --show-params
kubectl -n client get cronjob healthz-cron -o jsonpath='{.spec.suspend}{"\n"}'
```

`run.sh` endet mit `==> Wartungsmodus ist AKTIV: ...`. In der Liste steht
`argocd/wartung-aktiv`, `--show-params` zeigt `wartung  true` und der CronJob
liefert `true`.

## Wartungsmodus deaktivieren

```bash
./login.sh
argocd app set wartung-root --helm-set wartung=false
```

`wartung-root` entfernt `wartung-aktiv` mit beiden VirtualServices, der
CronJob bekommt `suspend: false`. Im Test kam der Client nach 4 Sekunden wieder
auf 200. Prüfen:

```bash
../run.sh
kubectl -n service get virtualservice
```

Danach gibt es im Namespace `service` nur noch den VirtualService `service`
aus dem Chart.

## Andere Wege zum selben Schalter

Alle drei ändern denselben Parameter an `wartung-root`.

**Argo-CD-UI.** Application `wartung-root` öffnen, `Details`, Reiter
`Parameters`, `Edit`. Bei `wartung` den Wert auf `true` oder `false` setzen
und speichern. Die UI markiert den Wert als Override.

**kubectl ohne argocd-CLI.**

```bash
kubectl -n argocd patch application wartung-root --type merge \
  -p '{"spec":{"source":{"helm":{"parameters":[{"name":"wartung","value":"true"}]}}}}'
```

Ein Merge-Patch ersetzt die ganze Liste `parameters`. Mit `REPO_URL` oder
`REVISION` aus `setup.sh` stehen dort auch `repoURL` und `targetRevision`.
Die müssen dann im Patch mit drinstehen, sonst holen die Kind-Apps wieder aus
GitHub `main`. Aktuellen Stand anzeigen:

```bash
kubectl -n argocd get application wartung-root -o jsonpath='{.spec.source.helm.parameters}{"\n"}'
```

**argocd-CLI**, wie oben mit `argocd app set`.

## Warum der Override bleibt

`selfHeal` setzt Ressourcen zurück, die eine App verwaltet. `wartung-root`
verwaltet keine andere App, `setup.sh` legt sie per CLI an. Ihr
Parameter-Override ist Teil des Soll-Zustands, und niemand setzt ihn zurück.

Das ändert sich, sobald `wartung-root` selbst aus Git kommt, etwa als Kind
einer übergeordneten App of Apps oder aus einem ApplicationSet. Dann setzt die
übergeordnete App den Override beim nächsten Sync zurück. Abhilfe ist dort ein
`ignoreDifferences` auf `/spec/source/helm/parameters` von `wartung-root`
zusammen mit der Sync-Option `RespectIgnoreDifferences=true`.

Der Override lebt nur im Cluster. Nach `./delete.sh` und `./setup.sh` steht
`wartung` wieder auf `false` aus `apps/values.yaml`.

## Unterschiede zu wartung-an.sh / wartung-aus.sh

- **Keine Reihenfolge.** `wartung-an.sh` hält erst die CronJobs an und schaltet
  dann die Wartung ein, `wartung-aus.sh` umgekehrt. Argo CD synct
  `wartung-aktiv` und `wartung-healthz-cron` unabhängig voneinander. Das ist
  hier unkritisch. Läuft ein Job in dem Moment, bekommt er 503 mit
  `Retry-After` und endet laut [../cron/cron.md](../cron/cron.md) mit
  "Wartung, Lauf übersprungen" und Exit-Code 0.
- **Laufende Jobs bleiben.** `wartung-an.sh` löscht laufende Jobs mit Label
  `wartung=service`, Argo CD nicht. Ein laufender Job endet spätestens nach
  `activeDeadlineSeconds: 60`.
- **Kein Warten auf istiod.** Argo CD meldet `Synced`, sobald die
  VirtualServices im Cluster sind, nicht wenn die Sidecars sie haben. Zum
  Prüfen `../run.sh` verwenden.
- **Skripte nicht mischen.** `../wartung-an.sh` gegen das Argo-CD-Setup setzt
  `suspend: true`, `selfHeal` setzt es nach etwa 2 Sekunden zurück. Die per
  `kubectl apply` angelegten VirtualServices hat Argo CD nicht im Tracking und
  lässt sie stehen. Die Wartung ist dann aktiv, der CronJob läuft aber weiter.

## Welche Möglichkeiten es gibt

Das Setup hier nutzt Variante 1. Die anderen gehen auch, mit diesen
Unterschieden. Wer die Reihenfolge und den Abbruch laufender Jobs braucht,
nimmt Variante 3. Sonst ist 1 die bessere Wahl, weil Argo CD dort den
Wartungszustand anzeigt und es nur einen Schalter gibt.

| Variante | Schalter | Was fehlt oder stört |
|----------|----------|----------------------|
| 1. Helm-Parameter `wartung` an `wartung-root` (umgesetzt) | `argocd app set wartung-root --helm-set wartung=true` | Zustand nur im Cluster, nicht in Git |
| 2. Parameter direkt an `wartung-service` und `wartung-healthz-cron` | zwei `argocd app set` | `wartung-root` setzt die Kind-Apps per `selfHeal` zurück. Braucht dort `ignoreDifferences` plus `RespectIgnoreDifferences=true`. Zwei Schalter, die auseinanderlaufen können |
| 3. Skripte `../wartung-an.sh` / `../wartung-aus.sh` weiter nutzen | Skript | `wartung-healthz-cron` braucht `ignoreDifferences` auf `/spec/suspend` plus `RespectIgnoreDifferences=true`. Argo CD zeigt nicht, dass Wartung aktiv ist. Dafür Reihenfolge, Abbruch laufender Jobs und Warten auf 503 |
| 4. Wartungs-App außerhalb von `wartung-root` anlegen und löschen | `argocd app create wartung-aktiv --repo ... --path wartung/manifestwartung --dest-namespace service --dest-server https://kubernetes.default.svc --sync-policy automated` / `argocd app delete wartung-aktiv` | CronJob wird nicht angehalten, außer mit `ignoreDifferences` wie in 3 |
| 5. ApplicationSet mit Cluster-Generator, Schalter als Label am Cluster-Secret | `argocd cluster set <cluster> --label wartung=true` | Lohnt erst, wenn ein Schalter viele Services oder Cluster trifft. Für einen Service zu viel Aufbau |

Was nicht geht:

- **ConfigMap im Cluster plus Helm-`lookup`.** Argo CD rendert Charts mit
  `helm template` ohne Zugriff auf den Cluster. `lookup` liefert dort immer
  ein leeres Ergebnis.
- **Sync-Windows.** Ein Sync-Window blockiert oder erlaubt Syncs. Es schaltet
  keine Ressourcen ein oder aus.

## Aufräumen

```bash
./delete.sh
```

Führt intern aus:

```bash
./login.sh
argocd app delete wartung-root --cascade --yes --wait
kubectl wait --for=delete namespace/client namespace/service --timeout=120s
argocd app list
```

Die Finalizer löschen alle Kind-Apps mit ihren Ressourcen, auch
`wartung-aktiv`, wenn die Wartung gerade an ist. Die Namespaces `client` und
`service` gehören `wartung-root`, Argo CD löscht sie mit. Im Test dauerte das
etwa 9 Sekunden.

## Test ohne Push

Argo CD holt die Manifeste aus Git. Für einen Test mit lokalen, nicht
gepushten Änderungen läuft ein `git daemon` im Cluster. `setup.sh` zeigt per
`REPO_URL` darauf.

```bash
# Snapshot des Arbeitsverzeichnisses als Bare-Repo
tmp=$(mktemp -d)
git clone -q "$(git rev-parse --show-toplevel)" "${tmp}/repo"
rsync -a --exclude .git ../ "${tmp}/repo/wartung/"
git -C "${tmp}/repo" add -A
git -C "${tmp}/repo" -c user.name=test -c user.email=test@test commit -qm test
git clone -q --bare "${tmp}/repo" "${tmp}/istio-usecases.git"

# Git-Server im Cluster
kubectl create namespace gitserver
kubectl -n gitserver run gitserver --image=alpine/git:latest --restart=Never \
  --labels=app=gitserver --port=9418 --command -- sleep infinity
kubectl -n gitserver expose pod gitserver --port=9418
kubectl -n gitserver wait --for=condition=Ready pod/gitserver --timeout=120s
kubectl -n gitserver exec gitserver -- mkdir -p /srv/git
kubectl cp "${tmp}/istio-usecases.git" gitserver/gitserver:/srv/git/istio-usecases.git
kubectl -n gitserver exec gitserver -- sh -c \
  'apk add --no-cache git-daemon && git daemon --detach --reuseaddr --export-all --base-path=/srv/git /srv/git'

REPO_URL=git://gitserver.gitserver.svc.cluster.local/istio-usecases.git ./setup.sh
```

Das Image `alpine/git` bringt `git daemon` nicht mit, `apk add` braucht daher
Internetzugang aus dem Cluster. Nach Änderungen den Snapshot neu bauen,
kopieren und die betroffenen Apps neu einlesen, z.B.
`argocd app get wartung-healthz-cron --hard-refresh`. Aufräumen:

```bash
./delete.sh
kubectl delete namespace gitserver
```

## Troubleshooting

- **`argocd login` hängt mit `gRPC connection not ready`.** Server ohne Port
  angegeben. `login.sh` nutzt `argocd.localhost:80`. Ein anderer Server geht
  per `ARGOCD_SERVER=host:port ./login.sh`.
- **`app path does not exist` bei `argocd app create`.** Die Dateien aus
  `argocd/` sind nicht im Branch, auf den `root-app.yaml` zeigt. Pushen oder
  `REPO_URL`/`REVISION` setzen.
- **Wartung an, aber der Client bekommt 200.** Prüfen, ob `wartung-aktiv`
  existiert und `Synced` ist (`argocd app list`). Wenn ja, fehlt meist nur der
  xDS-Push, siehe Troubleshooting in [../readme.md](../readme.md).
- **Override ist nach kurzer Zeit weg.** `wartung-root` wird von einer anderen
  App oder einem ApplicationSet verwaltet, siehe
  [Warum der Override bleibt](#warum-der-override-bleibt).
- **`wartung-healthz-cron` ist `OutOfSync`, `suspend` springt.** Jemand hat
  `../wartung-an.sh` oder `kubectl patch` gegen das Argo-CD-Setup ausgeführt.
  Nur den Parameter an `wartung-root` verwenden.
