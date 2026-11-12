# Telepresence mit Istio: lokalen Container statt Cluster-Pod bedienen

[Telepresence](https://github.com/telepresenceio/telepresence) (CNCF Sandbox, Open Source)
leitet den eingehenden Traffic eines Workloads im Cluster auf einen lokal laufenden
Prozess oder Container um (`intercept`, `replace`) oder kopiert ihn dorthin (`wiretap`).
Der lokale Container bekommt die Umgebungsvariablen des Pods, kann die Volumes des Pods
mounten und erreicht Cluster-Services per DNS.

Der Aufbau ist derselbe wie in [mirrord/mirrord.md](../mirrord/mirrord.md). Ein `nginx`-Deployment
läuft im Istio-Mesh (Namespace `telepresencetest`), und ein lokaler `nginx`-Container übernimmt
die Requests an den Service `nginx`. Der wichtigste Unterschied zu mirrord ist, dass Telepresence
immer eine dauerhaft installierte Komponente im Cluster braucht, den **traffic-manager**.
Einen Modus nur mit CLI wie bei mirrord gibt es nicht.

Ich habe mit Telepresence 2.33.0 (OSS), kind (Kubernetes 1.37) und Istio 1.30.8
(Sidecar-Modus) getestet.

## Architektur

Telepresence kennt zwei Arten, den Agent an den Ziel-Pod zu bringen.

Im Standardfall läuft der Agent als Sidecar im Ziel-Pod:

```
curl-Pod (Mesh) --mTLS--> nginx-Pod (wird beim ersten Intercept neu gestartet)
                           ├─ istio-proxy (terminiert mTLS)
                           │     └─ nftables-Redirect Port 80 -> 9900
                           ├─ traffic-agent (Sidecar, Port 9900)
                           │        │
                           │        ▼  Port-Forward über die Kube-API
                           │   Telepresence-Daemon (lokaler Container "<kontext>-<ns>-cn")
                           │        │
                           │        ▼
                           │   nginx-local (lokaler Docker-Container)
                           └─ nginx (Cluster-Container, bekommt beim Intercept nichts mehr)

traffic-manager (StatefulSet in "ambassador", dauerhaft)
  + MutatingWebhook "agent-injector" (clusterweit, alle Namespaces außer kube-system)
```

- `telepresence helm install` legt im Namespace `ambassador` den traffic-manager an
  (StatefulSet `traffic-manager`, Image `ghcr.io/telepresenceio/tel2`), dazu eine
  ClusterRole, die u. a. Deployments, ReplicaSets und StatefulSets patchen und Pods
  evicten darf, und eine clusterweite `MutatingWebhookConfiguration`.
- Beim ersten Intercept evictet der traffic-manager den Pod. Der Webhook injiziert in den
  neuen Pod den Container `traffic-agent` und, bei numerischem `targetPort`, den
  Init-Container `tel-agent-init` mit `NET_ADMIN`. Das Deployment-Template bleibt
  unverändert.
- `tel-agent-init` legt im Pod eine eigene nftables-Tabelle `ip telepresence` an:

  ```
  chain output {
    type nat hook output priority dstnat - 10;
    oifname "lo" meta skgid != 7439 redirect to :meta l4proto . th dport map @redirect_ports
    ip daddr 10.244.1.13 redirect to :meta l4proto . th dport map @redirect_ports   # Pod-IP
    ...
    meta skgid 7439 th dport != 53 dnat to ip daddr
  }
  ```

  Istio leitet eingehende Verbindungen zuerst auf Envoy (15006) um. Envoy terminiert mTLS
  und verbindet sich mit `<Pod-IP>:80`. Diese Verbindung fängt die `output`-Kette ab
  (Priorität vor Istios `OUTPUT`) und leitet sie auf den Agent-Port 9900. Der Agent sieht
  also Klartext-HTTP hinter dem Sidecar, wie bei mirrord. mTLS und AuthorizationPolicies
  gelten weiter, und HTTP-Filter funktionieren.
- `telepresence connect --docker` startet den Daemon als lokalen Container (Image
  `ghcr.io/telepresenceio/telepresence`) und installiert zwei Docker-Plugins:
  `telemount` (Volumes des Pods) und `teleroute` (Netzwerk in den Cluster). Root-Rechte
  braucht das nicht. Ohne `--docker` läuft ein Root-Daemon auf dem Host (VPN-Interface,
  `sudo`), das habe ich nicht getestet.
- `telepresence intercept ... --docker-run -- <docker run Argumente>` startet den lokalen
  Container im Netzwerk des Daemon-Containers.

**Node-Agent.** Mit `--node-agent` (muss im traffic-manager aktiviert sein) startet statt eines
Sidecars der traffic-manager einen Job `tel-node-agent-<workload>-<id>` auf dem
Node des Ziel-Pods, im Namespace des traffic-managers. Der Job wechselt in die
Namespaces des Pods und setzt dort dieselben nftables-Regeln. Der Pod wird nicht neu
gestartet. Das entspricht dem Ansatz von mirrord.

## Was braucht man?

| Was | Wo | Anmerkung |
|---|---|---|
| `telepresence` CLI | lokal | Binary von den [GitHub-Releases](https://github.com/telepresenceio/telepresence/releases) |
| Docker | lokal | für `connect --docker` und `--docker-run` |
| Image `ghcr.io/telepresenceio/telepresence:<version>` | lokal | Daemon-Container, wird bei `connect --docker` gepullt |
| Docker-Plugins `telemount`, `teleroute` | lokal | werden bei `connect --docker` automatisch installiert (`docker plugin ls`) |
| traffic-manager | Cluster, dauerhaft | `telepresence helm install`. Namespace `ambassador`, StatefulSet, ClusterRole, ClusterRoleBinding, MutatingWebhook. Braucht einmalig Cluster-Admin |
| Image `ghcr.io/telepresenceio/tel2:<version>` | Cluster-Nodes | traffic-manager und traffic-agent |
| Images `docker.io/busybox`, `docker.io/curlimages/curl:8.1.1` | Cluster-Nodes | Helm-Hooks bei Upgrade und Uninstall |
| RBAC für Entwickler | Cluster | siehe [rbac/telepresence-user.yaml](rbac/telepresence-user.yaml) und Abschnitt 9 |
| `NET_ADMIN` für den Init-Container | Ziel-Namespace | nur Sidecar-Modus mit numerischem `targetPort` |
| privilegierte Pods im Manager-Namespace | `ambassador` | nur Node-Agent-Modus: `hostPID`, hostPath `/run`, `SYS_ADMIN`, `SYS_PTRACE`, `NET_ADMIN`, `NET_RAW` |

Anders als bei mirrord ist das lokale Image egal. Telepresence nutzt kein `LD_PRELOAD`,
deshalb funktioniert `nginx:1.27-alpine` auch lokal (getestet).

## Vergleich mit mirrord (beide ohne Operator)

| | mirrord OSS | Telepresence OSS |
|---|---|---|
| Dauerhaft im Cluster | nichts | traffic-manager, ClusterRole, clusterweiter Webhook |
| Ziel-Pod | bleibt unverändert, Agent als Job | Sidecar-Modus: Pod wird evictet und mit Agent neu erstellt. Node-Agent-Modus: unverändert |
| Agent nach Session-Ende | Job räumt sich auf | Sidecar bleibt im Pod bis `telepresence uninstall` (wieder ein Pod-Neustart) |
| Abgriff mit Istio | hinter dem Sidecar, Klartext | hinter dem Sidecar, Klartext |
| Steal / Mirror | `steal` / `mirror` | `intercept` / `wiretap`, zusätzlich `replace` (Container ersetzen) und `ingest` (nur Env und Volumes) |
| HTTP-Header-Filter | ja | ja, auch Pfad-Filter (`--http-path-prefix` usw.) |
| Lokales Image | nur glibc | beliebig |
| Ausgehender Traffic des lokalen Containers | über den Ziel-Pod | über den traffic-agent im Ziel-Pod, aber am istio-proxy vorbei (siehe Fund 3) |
| Rechte des Entwicklers | Job, Pod-Log, Port-Forward im Agent-Namespace | Port-Forward auf `traffic-manager-0` und im Ziel-Namespace. Ohne `enforcing` reicht weniger (siehe Fund 4) |
| Mehrere Entwickler auf einem Ziel | keine Sperre ohne Operator | traffic-manager erkennt Konflikte. `intercept.allowGlobalIntercepts: false` erzwingt Filter |

## Funde

### 1. Pod-Volumes überdecken eigene Bind-Mounts

Mit dem Bind-Mount aus mirrord.md antwortete der lokale Container mit `403 Forbidden`:

```
"/usr/share/nginx/html/index.html" is forbidden (13: Permission denied)
```

`intercept` mountet standardmäßig alle Volumes des Ziel-Containers per Docker-Plugin
`telemount` an denselben Pfad in den lokalen Container, hier die ConfigMap unter
`/usr/share/nginx/html`. Der Bind-Mount der lokalen `index.html` liegt dann in diesem
Remote-Mount und ist nicht lesbar. `--mount=false` schaltet das ab, das entspricht
`"fs": "local"` bei mirrord. Alle Beispiele unten verwenden `--mount=false`.

### 2. Bestehende Keepalive-Verbindungen umgehen den Intercept

Envoy hält Keepalive-Verbindungen zur App offen. Die NAT-Regel greift nur für neue
Verbindungen. Was vor dem Intercept aufgebaut wurde, geht weiter an den Cluster-Container.

| Situation | Ergebnis (10 Requests) |
|---|---|
| Sidecar-Modus, erster Intercept (Pod frisch gestartet) | 10× lokal |
| Sidecar-Modus, `detach`, Requests, dann erneuter `intercept` | 10× Cluster, auch nach 2 Minuten mit Requests alle 10 s |
| Node-Agent-Modus, Pod hatte vorher Traffic | 10× Cluster |
| wie oben, mit HTTP-Header-Filter (Sidecar-Modus) | 10× lokal |
| wie oben, mit [istio/sidecar-no-inbound-keepalive.yaml](istio/sidecar-no-inbound-keepalive.yaml) | 10× lokal |

Im Sidecar-Modus läuft zwar jede Verbindung durch den Agent, er entscheidet aber pro
TCP-Verbindung, nicht pro Request. Eine Verbindung, die er ohne aktiven Intercept an die
App weitergereicht hat, bleibt dort. Im Node-Agent-Modus kommen die alten Verbindungen
gar nicht erst am Agent vorbei (`ss` im Pod zeigte `envoy -> <Pod-IP>:80` direkt).
nginx schließt Keepalive-Verbindungen erst nach 75 s Leerlauf, bei laufendem Traffic also
nie.

Drei Abhilfen haben im Test funktioniert:

- **HTTP-Filter.** Mit Filter (Abschnitt 6) wertet der Agent jeden Request einzeln aus.
- **Sidecar-Ressource.** [istio/sidecar-no-inbound-keepalive.yaml](istio/sidecar-no-inbound-keepalive.yaml)
  setzt `inboundConnectionPool.http.maxRequestsPerConnection: 1`. Envoy öffnet für jeden Request
  eine neue Verbindung zur App. Schon das Anwenden der Ressource beendete im Test die alten
  Verbindungen. Die Ressource gehört nur in Entwicklungs-Namespaces, weil Envoy dann für
  jeden Request eine eigene TCP-Verbindung aufbaut.
- **Uninstall.** `telepresence uninstall <workload>` vor dem Intercept startet den Pod im
  Sidecar-Modus neu, und Envoy baut alle Verbindungen neu auf.

### 3. Ausgehender Traffic umgeht den istio-proxy

Der lokale Container erreicht Cluster-Services (`curl http://nginx` aus `nginx-local`
heraus funktioniert). Die Verbindung baut der traffic-agent im Ziel-Pod auf. Die Regel
`meta skgid 7439 ... dnat to ip daddr` (7439 = Gruppe des Agents) setzt aber eine
NAT-Entscheidung, bevor Istios `OUTPUT`-Kette läuft. Der Redirect auf Envoy (15001)
entfällt, die Verbindung geht als Klartext ohne Workload-Identität raus. Die
Telepresence-Doku beschreibt das als gewollt („mesh isolation“).

Mit `PeerAuthentication` `STRICT` im Namespace sah das so aus:

```
kubectl -n telepresencetest exec curl -c curl -- curl -s http://nginx
# Antwort vom LOKALEN nginx-Container (via Telepresence)      <- Inbound geht
docker exec nginx-local wget -qO- -T5 http://nginx               # lokales Image: nginx:1.27-alpine
# wget: error getting response: Resource temporarily unavailable  <- Outbound scheitert
# traffic-agent-Log: read tcp 10.244.1.17:38226->10.96.163.75:80: read: connection reset by peer
```

Ohne `STRICT` (PERMISSIVE) kam die Antwort. Ziele mit `STRICT` mTLS oder mit
AuthorizationPolicies auf `principals` sind aus dem lokalen Container also nicht
erreichbar. Wie sich mirrord hier verhält, habe ich nicht geprüft.

Für genau diesen Fall gibt es den Helm-Wert `agent.serviceMesh.dialSubnets`
([helm/values-istio.yaml](helm/values-istio.yaml)). Verbindungen in die dort gelisteten Netze
sollen über den Mesh-Proxy laufen. **In 2.33.0 ist der Wert unbrauchbar.** Sobald ein Pod mit so
konfiguriertem Agent existiert, gerät der traffic-manager in eine CrashLoop:

```
panic: cannot handle unexported field at {*agentconfig.Sidecar}.MeshDialSubnets[0].ip:
        "net/netip".Prefix
```

Zum Zurückrollen macht man ein Helm-Upgrade ohne den Wert und löscht danach die betroffenen
Pods und den Pod `traffic-manager-0`.

### 4. Im Standardmodus reicht Port-Forward auf den traffic-manager für alle Namespaces

Der traffic-manager hat eine ClusterRole und führt die eigentlichen Änderungen selbst aus.
Ob der Entwickler im Ziel-Namespace etwas darf, prüft der traffic-manager per `SubjectAccessReview`
(`create pods/portforward` im Ziel-Namespace). Im Standardmodus
`security.authentication.mode: permissive` wird das Ergebnis aber nur protokolliert.

Ich habe das mit einem ServiceAccount getestet, der nur `pods/portforward` im Namespace `ambassador`
hatte und in den Ziel-Namespaces gar nichts (`kubectl auth can-i get pods -n
telepresence-other` → `no`). Der Intercept auf `deployment/echo` in
`telepresence-other` lief, der Traffic kam lokal an. Der traffic-manager loggte:

```
WARN httpd/CreateIntercept: ... telepresence-dev is not permitted to create pods/portforward
     in namespace telepresence-other (not enforced)
```

Wer den traffic-manager erreicht, kann also jeden Workload im Cluster umleiten, dessen
Umgebungsvariablen lesen und dessen Volumes mounten, Secrets eingeschlossen. Mit
[helm/values-enforcing.yaml](helm/values-enforcing.yaml) lehnt der traffic-manager das ab:

```
connector.CreateIntercept: system:serviceaccount:telepresencetest:telepresence-dev is not
permitted to create pods/portforward in namespace telepresence-other
```

### 5. Weitere Beobachtungen

- **Pod-Neustarts im Sidecar-Modus.** Der erste Intercept startet den Pod neu, um den Agent
  zu injizieren. `detach` startet nichts neu, der Agent bleibt im Pod (`3/3`).
  `telepresence uninstall` startet den Pod erneut. `replace` startet ihn beim Start neu
  (App-Container raus) und beim `detach` noch einmal. Bei einem Deployment mit einer
  Replica antwortet während jedes Neustarts kurz niemand. Der erste
  Request direkt nach dem Intercept lief im Test in einen Timeout.
- **Node-Agent und `detach`.** Für etwa 2 s, solange der Job beendet wird, kam `503`
  zurück. Danach waren nftables-Tabelle und Job weg.
- **Pods ohne Agent, wenn der traffic-manager fehlt.** Der Webhook hat `failurePolicy: Ignore`. Pods, die
  erstellt werden, während der traffic-manager nicht läuft, starten ohne Agent.
- **Zweiter Daemon.** `telepresence uninstall` ohne bestehende Verbindung startete einen
  zweiten Daemon auf dem Host. Danach verlangte jedes Kommando `--use <daemon>`. Das lässt
  sich vermeiden, indem man vorher `telepresence connect --docker` ausführt. Hinterher
  beendet `telepresence quit -s` alle Daemons.

## Dateien

| Datei | Inhalt |
|---|---|
| [manifests/00-namespace.yaml](manifests/00-namespace.yaml) | Namespace `telepresencetest` mit `istio-injection: enabled` |
| [manifests/05-nginx-html.yaml](manifests/05-nginx-html.yaml) | ConfigMap mit `index.html` „Antwort aus dem CLUSTER“ |
| [manifests/10-nginx-deployment.yaml](manifests/10-nginx-deployment.yaml) | `nginx:1.27-alpine`, 1 Replica, Port 80 (`name: http`) |
| [manifests/11-nginx-service.yaml](manifests/11-nginx-service.yaml) | Service `nginx` Port 80, numerischer `targetPort` |
| [test/curl-pod.yaml](test/curl-pod.yaml) | Test-Client im Mesh |
| [test/other-namespace.yaml](test/other-namespace.yaml) | Namespace `telepresence-other` mit Deployment `echo` für Fund 4 |
| [local/index.html](local/index.html) | Seite des lokalen Containers: „Antwort vom LOKALEN nginx-Container“ |
| [helm/values-node-agent.yaml](helm/values-node-agent.yaml) | Node-Agent-Modus einschalten |
| [helm/values-enforcing.yaml](helm/values-enforcing.yaml) | Autorisierung erzwingen, Legacy-RBAC aus, Node-Agent an |
| [helm/values-registry.yaml](helm/values-registry.yaml) | Images aus eigener Registry (Platzhalter `${TP_REGISTRY}`) |
| [helm/values-istio.yaml](helm/values-istio.yaml) | `agent.serviceMesh.dialSubnets`, **in 2.33.0 defekt** (Fund 3) |
| [client-config-registry.yml](client-config-registry.yml) | Client-Konfiguration: Daemon-Image aus eigener Registry |
| [istio/sidecar-no-inbound-keepalive.yaml](istio/sidecar-no-inbound-keepalive.yaml) | Envoy ohne Keepalive zur App (Fund 2) |
| [rbac/telepresence-user.yaml](rbac/telepresence-user.yaml) | minimale Rechte + Test-ServiceAccount `telepresence-dev` |
| [openshift/](openshift/) | Service mit benanntem Port, SCC-Binding für Node-Agents, Helm-Werte (nicht getestet) |

## Voraussetzungen

- Kubernetes-Cluster mit Istio (Sidecar-Injection)
- `kubectl` mit Cluster-Admin-Kontext für die Installation des traffic-managers
- `telepresence` CLI und Docker lokal

## 1. Cluster-Seite deployen

```bash
cd telepresence
kubectl apply -f manifests/
kubectl apply -f test/curl-pod.yaml
kubectl -n telepresencetest rollout status deploy/nginx --timeout=120s
kubectl -n telepresencetest wait --for=condition=Ready pod/curl --timeout=120s
kubectl -n telepresencetest get pods      # beide Pods 2/2 (App + istio-proxy)

kubectl -n telepresencetest exec curl -c curl -- curl -s http://nginx
# Antwort aus dem CLUSTER (Pod im Namespace telepresencetest)
```

## 2. traffic-manager installieren

```bash
telepresence helm install
kubectl -n ambassador get statefulset,pods,svc
# statefulset.apps/traffic-manager, pod/traffic-manager-0 (1/1, ohne istio-proxy),
# service/agent-injector, service/traffic-manager, service/traffic-manager-headless
kubectl get mutatingwebhookconfigurations | grep agent-injector
```

Der Namespace `ambassador` hat kein `istio-injection`-Label, der traffic-manager läuft
außerhalb des Meshes. Werte setzt man mit `-f <datei>`, siehe Abschnitte 8 bis 10.

## 3. Verbinden

```bash
telepresence connect --docker -n telepresencetest
telepresence status
telepresence list
# deployment nginx: ready to attach (traffic-agent not yet installed)
```

`--docker` startet den Daemon als Container `kind-local-telepresencetest-cn` (Name aus
Kontext und Namespace). Welche Namespaces der Client sieht, steuert `--mapped-namespaces`.

## 4. Intercept mit lokalem Container

```bash
telepresence intercept nginx --port 80 --mount=false --docker-run -- \
  --rm --name nginx-local \
  -v "$PWD/local/index.html:/usr/share/nginx/html/index.html:ro" \
  nginx:1.27
# Using Deployment nginx
#    Intercept name: nginx
#    State         : ACTIVE
#    Intercepting  : 10.244.1.13 -> nginx-local
#        80 -> 80 TCP
```

Nach `--docker-run --` folgen `docker run`-Argumente. Mit dem Daemon im Container ist
`--port 80` der Port im lokalen Container. Telepresence übergibt die Umgebungsvariablen
des Pods (`NGINX_SERVICE_HOST`, ...) und setzt `TELEPRESENCE_*`-Variablen.

In einem zweiten Terminal prüft man das Ergebnis:

```bash
kubectl -n telepresencetest get pods
# nginx-...   3/3   Running   (neuer Pod mit traffic-agent)

kubectl -n telepresencetest exec curl -c curl -- curl -s http://nginx
# Antwort vom LOKALEN nginx-Container (via Telepresence)

docker exec nginx-local curl -s http://nginx     # Outbound aus dem lokalen Container
```

Den Intercept beendet `Ctrl+C` im ersten Terminal oder dieser Befehl:

```bash
telepresence detach nginx           # "leave" ist veraltet
kubectl -n telepresencetest exec curl -c curl -- curl -s http://nginx
# Antwort aus dem CLUSTER (Pod im Namespace telepresencetest)
telepresence list
# deployment nginx: ready to attach (traffic-agent already installed)
```

`uninstall` entfernt den Agent wieder und startet den Pod dabei neu:

```bash
telepresence uninstall nginx
```

Ein lokales Binary statt eines Containers startet man mit `telepresence intercept nginx --port 8080:http -- <binary>`
(lokaler Port 8080 für den Service-Port `http`). Das braucht einen Daemon ohne `--docker`
und damit Root-Rechte (nicht getestet).

## 5. Wiretap (Mirror)

Der Cluster-Pod antwortet weiter, der lokale Container bekommt eine Kopie.

```bash
telepresence wiretap nginx --port 80 --mount=false --docker-run -- \
  --rm --name nginx-local \
  -v "$PWD/local/index.html:/usr/share/nginx/html/index.html:ro" \
  nginx:1.27

kubectl -n telepresencetest exec curl -c curl -- curl -s http://nginx/
# Antwort aus dem CLUSTER (Pod im Namespace telepresencetest)
docker logs nginx-local 2>&1 | tail -1
# 172.16.0.2 - - [...] "GET / HTTP/1.1" 200 55 "-" "curl/8.11.0" "-"
```

## 6. Nur markierte Requests umleiten (HTTP-Filter)

```bash
telepresence intercept nginx --port 80 --mount=false \
  --http-header "x-telepresence=$USER" --docker-run -- \
  --rm --name nginx-local \
  -v "$PWD/local/index.html:/usr/share/nginx/html/index.html:ro" \
  nginx:1.27

kubectl -n telepresencetest exec curl -c curl -- curl -s http://nginx
# Antwort aus dem CLUSTER (Pod im Namespace telepresencetest)
kubectl -n telepresencetest exec curl -c curl -- curl -s -H "x-telepresence: $USER" http://nginx
# Antwort vom LOKALEN nginx-Container (via Telepresence)
```

Außerdem gibt es Pfad-Filter: `--http-path-prefix`, `--http-path-equal`, `--http-path-regex`. Mehrere
`--http-header` werden UND-verknüpft. Der Filter funktioniert auch mit Keepalive-Verbindungen,
die vor dem Intercept bestanden (Fund 2). Wie bei mirrord muss jeder Service in der
Aufrufkette den Header weiterreichen.

## 7. Container ersetzen (`replace`)

`replace` entfernt den App-Container aus dem Pod. Alle seine Ports gehen an den lokalen
Container, `--port` entfällt.

```bash
telepresence replace nginx --mount=false --docker-run -- \
  --rm --name nginx-local \
  -v "$PWD/local/index.html:/usr/share/nginx/html/index.html:ro" \
  nginx:1.27

kubectl -n telepresencetest get pod -l app=nginx \
  -o jsonpath='{range .items[*].spec.containers[*]}{.name}{"\n"}{end}'
# traffic-agent     (nginx ist weg, istio-proxy läuft als natives Sidecar in initContainers)
```

Nach `telepresence detach nginx` startet der traffic-manager den Pod erneut, diesmal mit nginx und Agent.
Im Node-Agent-Modus gibt es `replace` nicht.

## 8. Node-Agent-Modus (ohne Pod-Neustart)

```bash
telepresence helm upgrade -f helm/values-node-agent.yaml
telepresence uninstall nginx     # Pod darf keinen Sidecar-Agent haben
telepresence quit && telepresence connect --docker -n telepresencetest

telepresence intercept nginx --node-agent --port 80 --mount=false --docker-run -- \
  --rm --name nginx-local \
  -v "$PWD/local/index.html:/usr/share/nginx/html/index.html:ro" \
  nginx:1.27

kubectl -n telepresencetest get pods      # nginx-Pod unverändert 2/2, kein Neustart
kubectl -n ambassador get jobs,pods
# job.batch/tel-node-agent-nginx-10762414   Running
```

Mit `nodeAgent.enabled: true` wird der Node-Agent auch ohne `--node-agent` zum Standard,
`--node-agent=false` erzwingt den Sidecar. Nach `detach` löscht der traffic-manager den Job
samt nftables-Regeln im Pod.

Wegen Fund 2 sollte vorher [istio/sidecar-no-inbound-keepalive.yaml](istio/sidecar-no-inbound-keepalive.yaml)
aktiv sein. Alternativ arbeitet man mit HTTP-Filter.

```bash
kubectl apply -f istio/sidecar-no-inbound-keepalive.yaml
```

Der Job läuft mit `hostPID`, hostPath `/run` und `SYS_ADMIN`, `SYS_PTRACE`, `NET_ADMIN`,
`NET_RAW` im Namespace `ambassador` unter dem ServiceAccount `default`. Bei Pod Security
Admission braucht `ambassador` `pod-security.kubernetes.io/enforce: privileged`.

## 9. Rechte für Entwickler (`enforcing`)

Zuerst muss der traffic-manager im Modus `enforcing` laufen (Fund 4):

```bash
telepresence helm upgrade -f helm/values-enforcing.yaml
kubectl -n ambassador rollout status sts/traffic-manager
```

Dann reicht [rbac/telepresence-user.yaml](rbac/telepresence-user.yaml):

| Ressource | Namespace | Verben | Wofür |
|---|---|---|---|
| `pods/portforward`, `resourceNames: [traffic-manager-0]` | `ambassador` | create | Verbindung zum traffic-manager. Der Client wählt den Pod direkt über den festen Namen |
| `pods/portforward` | Ziel-Namespace | create | Verbindung zum traffic-agent und zugleich das Recht, das der traffic-manager per `SubjectAccessReview` prüft |

Im Unterschied zu mirrord reicht bei `pods/portforward` das Verb `create`, `get` war nicht
nötig. Fehlt das Recht im Ziel-Namespace, meldet der Client
`... is not permitted to create pods/portforward in namespace telepresencetest`.

So testet man die Roles mit einem ServiceAccount:

```bash
kubectl apply -f rbac/telepresence-user.yaml

kubectl config view --minify --raw > /tmp/kc-tp
TOKEN=$(kubectl -n telepresencetest create token telepresence-dev --duration=2h)
KUBECONFIG=/tmp/kc-tp kubectl config set-credentials telepresence-dev --token="$TOKEN"
KUBECONFIG=/tmp/kc-tp kubectl config set-context --current --user=telepresence-dev

telepresence quit -s
KUBECONFIG=/tmp/kc-tp telepresence connect --docker -n telepresencetest --manager-namespace ambassador
KUBECONFIG=/tmp/kc-tp telepresence intercept nginx --node-agent=false --port 80 --mount=false --docker-run -- \
  --rm --name nginx-local \
  -v "$PWD/local/index.html:/usr/share/nginx/html/index.html:ro" \
  nginx:1.27
```

- `--manager-namespace ambassador` ist nötig, sonst bricht `connect` mit `unable to
  determine the traffic-manager namespace` ab.
- Im Node-Agent-Modus reicht die Role nicht. Der Client will per Port-Forward direkt
  auf den Job-Pod in `ambassador`, dessen Name wechselt:
  `direct agent access to pod tel-node-agent-nginx-... refused (pods/portforward) and the
  QUIC tunnel is not available`. Dafür bräuchte der Entwickler `pods/portforward` ohne
  `resourceNames` in `ambassador` oder den QUIC-Tunnel (`quicTunnel.enabled`, nicht
  getestet).
- Statt `pods/portforward` prüft der traffic-manager alternativ die Pseudo-Ressourcen
  `telepresence.io/connections` und `telepresence.io/attachments`
  (`security.authorization.requiredGrant`). Die gibt es nur in RBAC-Regeln, nicht in der
  Kube-API. Ohne Port-Forward braucht man dann aber den QUIC-Tunnel.

## 10. Eigene Images aus interner Registry

Ich habe das gegen die lokale kind-Registry (`localhost:5001`) getestet. Für den Cluster gilt
[helm/values-registry.yaml](helm/values-registry.yaml), für den Client
[client-config-registry.yml](client-config-registry.yml), beide mit Platzhalter
`${TP_REGISTRY}`.

Die Images spiegelt man einmal pro Version:

```bash
export TP_REGISTRY=registry.internal/telepresenceio
VERSION=2.33.0

for img in tel2 telepresence; do
  docker pull ghcr.io/telepresenceio/$img:$VERSION
  docker tag  ghcr.io/telepresenceio/$img:$VERSION $TP_REGISTRY/$img:$VERSION
  docker push $TP_REGISTRY/$img:$VERSION
done
# Helm-Hooks: Pfad ist <registry>/<image>, also mit "curlimages/"
docker pull docker.io/busybox:latest
docker tag  docker.io/busybox:latest $TP_REGISTRY/busybox:latest
docker push $TP_REGISTRY/busybox:latest
docker pull docker.io/curlimages/curl:8.1.1
docker tag  docker.io/curlimages/curl:8.1.1 $TP_REGISTRY/curlimages/curl:8.1.1
docker push $TP_REGISTRY/curlimages/curl:8.1.1
```

Ohne das curl-Image schlägt schon `helm upgrade` fehl, weil der Pre-Upgrade-Hook es zieht:
`Failed to pull image "localhost:5001/telepresenceio/curlimages/curl:8.1.1": not found`.

So stellt man den traffic-manager um:

```bash
sed "s|\${TP_REGISTRY}|$TP_REGISTRY|g" helm/values-registry.yaml > /tmp/values-registry.yaml
telepresence helm upgrade -f helm/values-enforcing.yaml -f /tmp/values-registry.yaml
kubectl -n ambassador get pod traffic-manager-0 -o jsonpath='{.spec.containers[0].image}{"\n"}'
# registry.internal/telepresenceio/tel2:2.33.0
```

Bereits injizierte Sidecar-Agents behalten ihr altes Image, bis `telepresence uninstall
<workload>` den Pod neu startet. Neue Agents und Node-Agent-Jobs kamen im Test aus der
eigenen Registry.

Für den Client gehört der Inhalt nach `~/.config/telepresence/config.yml`.
`telepresence --config <datei>` funktioniert mit `--docker` nicht, der Daemon-Container
bindet das Verzeichnis read-only ein und scheitert an
`open /root/.config/telepresence/id: read-only file system`.

```bash
sed "s|\${TP_REGISTRY}|$TP_REGISTRY|g" client-config-registry.yml > ~/.config/telepresence/config.yml
telepresence quit -s
telepresence connect --docker -n telepresencetest
docker ps --format '{{.Names}} {{.Image}}' | grep -- -cn
# kind-local-telepresencetest-cn registry.internal/telepresenceio/telepresence:2.33.0
```

Docker installiert `telemount` und `teleroute` als Plugins (`docker plugin install`), sie
tauchen deshalb nicht in `docker images` auf. Die Client-Konfiguration hat dafür eigene
Felder (`client.docker.telemount`, `client.docker.teleroute`). Das Spiegeln
(`docker plugin push`) habe ich nicht getestet.

## 11. Telepresence mit OpenShift (`oc`)

> Dieser Abschnitt ist nicht gegen ein OpenShift-Cluster getestet. Er überträgt die
> obigen Ergebnisse auf die OpenShift-Eigenheiten (SCC, Istio-CNI über Multus, siehe
> [ovintegration/](../ovintegration/)) und die Telepresence-Doku.

`oc login` schreibt den Kontext in die kubeconfig, Telepresence nutzt sie wie `kubectl`.

| Komponente | Problem mit `restricted-v2` | Lösung |
|---|---|---|
| traffic-manager | Chart setzt `runAsUser: 1000`, liegt außerhalb des UID-Bereichs des Namespaces | [openshift/values-openshift.yaml](openshift/values-openshift.yaml) entfernt `runAsUser` |
| `tel-agent-init` (numerischer `targetPort`) | braucht `NET_ADMIN`, das weder `restricted-v2` noch `anyuid` erlaubt | benannten `targetPort` verwenden, dann gibt es keinen Init-Container |
| traffic-agent (Sidecar) | setzt nur `runAsGroup: 7439` | sollte unter der SCC des Workloads laufen |
| Node-Agent-Job | `hostPID`, hostPath, Capabilities | SCC `privileged` für SA `default` in `ambassador`: [openshift/10-node-agent-scc.yaml](openshift/10-node-agent-scc.yaml) |

Der benannte `targetPort` ist auf kind mit Istio getestet:

```bash
kubectl apply -f openshift/11-nginx-service-named-port.yaml   # targetPort: http
telepresence intercept nginx --node-agent=false --port 80 --mount=false -- true
kubectl -n telepresencetest get pod -l app=nginx \
  -o jsonpath='{.items[0].spec.initContainers[*].name}{"\n"}{range .items[0].spec.containers[*]}{.name}: {.ports}{"\n"}{end}'
# istio-init istio-proxy                                       <- kein tel-agent-init
# nginx: [{"containerPort":80,"name":"tm-http","protocol":"TCP"}]
# traffic-agent: [{"containerPort":9900,"name":"http","protocol":"TCP"}]
kubectl -n telepresencetest get endpointslices -l kubernetes.io/service-name=nginx \
  -o jsonpath='{.items[0].ports}{"\n"}'
# [{"name":"http","port":9900,"protocol":"TCP"}]
```

Der Agent übernimmt den Port-Namen, der Service zeigt auf 9900. Envoy leitet eingehende
Requests direkt an den Agent, ohne nftables-Regel.

Die Installation auf OpenShift muss ein Cluster-Admin ausführen:

```bash
telepresence helm install -f helm/values-enforcing.yaml -f openshift/values-openshift.yaml
oc apply -f openshift/10-node-agent-scc.yaml          # nur für den Node-Agent-Modus
oc -n ambassador get pod traffic-manager-0 -o jsonpath='{.metadata.annotations.openshift\.io/scc}{"\n"}'
```

Auf RHEL/Fedora ist meist Podman statt Docker installiert. `telepresence connect --docker` und
`--docker-run` setzen aber einen Docker-Daemon und Docker-Plugins voraus. Mit Podman habe ich das
nicht ausprobiert.

## Aufräumen

```bash
telepresence quit -s
telepresence helm uninstall                         # Hook entfernt auch alle Agents
kubectl delete ns ambassador --ignore-not-found     # bleibt nach helm uninstall stehen
kubectl delete -f rbac/telepresence-user.yaml --ignore-not-found
kubectl delete -f istio/sidecar-no-inbound-keepalive.yaml --ignore-not-found
kubectl delete -f test/other-namespace.yaml --ignore-not-found
kubectl delete -f test/curl-pod.yaml --ignore-not-found
kubectl delete -f manifests/ --ignore-not-found     # löscht auch den Namespace telepresencetest
kubectl get mutatingwebhookconfigurations | grep agent-injector   # sollte leer sein

# lokal: Daemon-Netzwerk und Docker-Plugins
docker network rm kind-local-telepresencetest-cn
docker plugin rm -f ghcr.io/telepresenceio/telemount:amd64-0.3.2 ghcr.io/telepresenceio/teleroute:amd64-0.5.0
rm -f ~/.config/telepresence/config.yml             # nur falls in Abschnitt 10 angelegt
```

Hängengebliebene lokale Container entfernt man so:

```bash
docker rm -f nginx-local
```
