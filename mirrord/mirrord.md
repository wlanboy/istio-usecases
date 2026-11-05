# mirrord mit Istio: lokalen Container statt Cluster-Pod bedienen

[mirrord](https://github.com/metalbear-co/mirrord) hängt einen lokal laufenden Prozess
oder Container an einen Pod im Cluster. Eingehender Traffic des Pods wird zum lokalen
Prozess umgeleitet (`steal`) oder kopiert (`mirror`). Ausgehende Verbindungen, DNS und
Umgebungsvariablen kommen aus dem Cluster. Der lokale Container verhält sich dadurch so,
als liefe er im Pod.

Dieser Usecase zeigt das an einem `nginx`-Deployment im Istio-Mesh (Namespace
`mirrordtest`): Ein lokaler `nginx`-Container übernimmt die Requests, die im Cluster an
den Service `nginx` gehen. Wir arbeiten ohne mirrord-Operator, nur mit dem
Open-Source-CLI.

Getestet mit mirrord 3.264.0, kind (Kubernetes 1.37) und Istio 1.30.7 (Sidecar-Modus).

## Architektur

```
curl-Pod (Mesh) --mTLS--> nginx-Pod
                           ├─ istio-proxy (terminiert mTLS)
                           │     └─ iptables-Redirect vom mirrord-Agent
                           │              │
                           │              ▼  Port-Forward über die Kube-API
                           │        mirrord intproxy (lokaler Container "mirrord-cli")
                           │              │
                           │              ▼
                           │        nginx-local (lokaler Docker-Container)
                           └─ nginx (Cluster-Container, bekommt im steal-Modus nichts mehr)
```

- `mirrord container` startet lokal zwei Container: den Hilfscontainer
  `ghcr.io/metalbear-co/mirrord-cli:<version>` (intproxy) und den eigentlichen
  Container, in den die Bibliothek `libmirrord_layer.so` per `LD_PRELOAD` geladen wird.
- Im Cluster legt mirrord einen kurzlebigen Job `mirrord-agent-<id>` auf dem Node des
  Ziel-Pods an. Der Agent wechselt in den Netzwerk-Namespace des Ziel-Pods und setzt
  dort iptables-Regeln für Port 80.
- Der Agent erkennt Istio automatisch (`MIRRORD_AGENT_IN_SERVICE_MESH=true` in der
  Agent-Pod-Spec) und greift den Traffic erst nach dem `istio-proxy` ab. Der lokale
  Container bekommt also Klartext-HTTP. mTLS und AuthorizationPolicies des Meshes gelten
  weiter, und der HTTP-Header-Filter (siehe unten) funktioniert.
- Der Agent-Pod bekommt automatisch `sidecar.istio.io/inject: "false"` und kann deshalb
  auch in einem Namespace mit `istio-injection: enabled` laufen.
- Wird mirrord beendet, entfernt der Agent seine iptables-Regeln, und der Job räumt sich
  selbst auf (`ttlSecondsAfterFinished`). Danach antwortet wieder der Cluster-Pod.

## Was braucht man außer dem Binary (ohne Operator)?

Im Cluster wird nichts dauerhaft installiert, also keine CRDs, kein Operator und kein
DaemonSet. Folgendes muss aber vorhanden sein:

| Was | Wo | Anmerkung |
|---|---|---|
| `mirrord` CLI | lokal | `curl -fsSL https://raw.githubusercontent.com/metalbear-co/mirrord/main/scripts/install.sh \| bash` |
| Docker, Podman oder nerdctl | lokal | nur für `mirrord container`. `mirrord exec` für lokale Binaries braucht das nicht |
| Image `ghcr.io/metalbear-co/mirrord-cli:<version>` | lokal | wird beim ersten `mirrord container` automatisch gepullt. Override: `container.cli_image` |
| Image `ghcr.io/metalbear-co/mirrord:<version>` | Cluster-Nodes | Agent-Image. Ohne Internetzugang in eine interne Registry spiegeln und `agent.image` sowie ggf. `agent.image_pull_secrets` setzen. Die Version muss zum CLI passen |
| RBAC für den Entwickler | Cluster | siehe [rbac/mirrord-user.yaml](rbac/mirrord-user.yaml), getestet mit einem ServiceAccount ohne weitere Rechte |
| Erlaubnis für privilegierte Agent-Pods | Agent-Namespace | `hostPID: true`, hostPath `/run` und `/var`, Capabilities `SYS_ADMIN`, `SYS_PTRACE`, `NET_ADMIN`. Bei Pod Security Admission muss der Agent-Namespace `pod-security.kubernetes.io/enforce: privileged` haben, auf OpenShift die SCC `privileged` (siehe unten) |
| glibc-basiertes Image für den lokalen Container | lokal | Alpine/musl funktioniert nicht, siehe nächster Abschnitt |

Die minimale Role habe ich schrittweise ermittelt. Jede fehlende Regel führte zu einem
konkreten Fehler, die Meldungen stehen in der Tabelle.

| Ressource | Verben | Wofür |
|---|---|---|
| `apps/deployments`, `apps/replicasets` | get, list, watch | Ziel `deployment/nginx` auf einen Pod auflösen |
| `pods` | get, list, watch | Ziel-Pod und Agent-Pod finden |
| `batch/jobs` | create, get, list, watch, delete | Agent als Job starten |
| `pods/log` | get | CLI liest das Agent-Log, um den Start abzuwarten. Fehlt es: `cannot get resource "pods/log"` |
| `pods/portforward` | get, create | Verbindung CLI → Agent. Seit Kubernetes 1.31 läuft Port-Forward über WebSocket, das braucht `get`. Nur mit `create` kommt `failed to upgrade to a WebSocket connection: 403 Forbidden` |

Ohne Operator fehlen u. a.: Sperren gegen gleichzeitiges Stehlen desselben Ziels durch
mehrere Entwickler, Queue-Splitting, `copy_target` und eine zentrale Rechteverwaltung.
In einem geteilten Cluster deshalb mit HTTP-Header-Filter arbeiten (siehe unten).

## Fund: Alpine-Images funktionieren nicht

Der erste Test lief mit `nginx:1.27-alpine` als lokalem Container und brach sofort ab:

```
Error loading shared library ld-linux-x86-64.so.2: No such file or directory (needed by /opt/mirrord/lib/libmirrord_layer.so)
Error relocating /opt/mirrord/lib/libmirrord_layer.so: gnu_get_libc_version: symbol not found
Error relocating /opt/mirrord/lib/libmirrord_layer.so: __res_init: symbol not found
```

`libmirrord_layer.so` ist gegen glibc gebaut und wird per `LD_PRELOAD` in den
Container-Prozess geladen. Alpine nutzt musl, dort fehlen der glibc-Loader und die
Symbole. Abhilfe schafft ein glibc-basiertes Image wie Debian, Ubuntu oder UBI, hier
`nginx:1.27`. Das Cluster-Deployment darf weiterhin Alpine nutzen, die Einschränkung
betrifft nur den lokalen Container. Statisch gelinkte Binaries (z. B. Go mit
`CGO_ENABLED=0`) haben ein ähnliches Problem, weil `LD_PRELOAD` dort nicht greift.

## Dateien

| Datei | Inhalt |
|---|---|
| [manifests/00-namespace.yaml](manifests/00-namespace.yaml) | Namespace `mirrordtest` mit `istio-injection: enabled` |
| [manifests/05-nginx-html.yaml](manifests/05-nginx-html.yaml) | ConfigMap mit `index.html` "Antwort aus dem CLUSTER" |
| [manifests/10-nginx-deployment.yaml](manifests/10-nginx-deployment.yaml) | `nginx:1.27-alpine`, 1 Replica, Port 80 |
| [manifests/11-nginx-service.yaml](manifests/11-nginx-service.yaml) | Service `nginx` Port 80 |
| [test/curl-pod.yaml](test/curl-pod.yaml) | Test-Client im Mesh |
| [local/index.html](local/index.html) | Seite des lokalen Containers: "Antwort vom LOKALEN nginx-Container" |
| [mirrord.json](mirrord.json) | mirrord-Config: steal, alles |
| [mirrord-filter.json](mirrord-filter.json) | mirrord-Config: steal nur mit Header `x-mirrord: <$USER>` |
| [rbac/mirrord-user.yaml](rbac/mirrord-user.yaml) | minimale Rechte + Test-ServiceAccount `mirrord-dev` |
| [openshift/](openshift/) | Agent-Namespace, SCC-Binding, Entwickler-RBAC und Config für OpenShift |

## Voraussetzungen

- Kubernetes-Cluster mit Istio (Sidecar-Injection)
- `kubectl` mit gültigem Kontext
- `mirrord` CLI und Docker lokal

## 1. Cluster-Seite deployen

```bash
cd mirrord
kubectl apply -f manifests/
kubectl apply -f test/curl-pod.yaml
kubectl -n mirrordtest rollout status deploy/nginx --timeout=120s
kubectl -n mirrordtest wait --for=condition=Ready pod/curl --timeout=120s
kubectl -n mirrordtest get pods      # beide Pods 2/2 (App + istio-proxy)
```

Gegenprobe ohne mirrord.

```bash
kubectl -n mirrordtest exec curl -c curl -- curl -s http://nginx
# Antwort aus dem CLUSTER (Pod im Namespace mirrordtest)
```

## 2. mirrord konfigurieren

[mirrord.json](mirrord.json):

```json
{
  "target": {
    "path": "deployment/nginx",
    "namespace": "mirrordtest"
  },
  "agent": {
    "namespace": "mirrordtest"
  },
  "feature": {
    "network": {
      "incoming": {
        "mode": "steal"
      },
      "outgoing": true,
      "dns": true
    },
    "fs": "local",
    "env": true
  }
}
```

| Schlüssel | Bedeutung |
|---|---|
| `target.path` | Ziel: `deployment/<name>`, `pod/<name>`, `statefulset/<name>` usw. mirrord wählt einen Pod des Deployments und den App-Container. `istio-proxy` wird automatisch übersprungen. Explizit: `deployment/nginx/container/nginx` |
| `target.namespace` | Namespace des Ziels |
| `agent.namespace` | Namespace für den Agent-Job. Fehlt der Wert, landet der Job im Default-Namespace des kubeconfig-Kontexts, im Test war das `default` |
| `incoming.mode` | `steal`: Requests gehen nur noch an den lokalen Container. `mirror`: der Cluster-Pod antwortet, der lokale Container bekommt eine Kopie |
| `outgoing`, `dns` | ausgehende Verbindungen und DNS-Auflösung des lokalen Containers laufen über den Pod |
| `fs: "local"` | Dateisystem bleibt lokal (sonst würde der Container z. B. die `index.html` aus dem Pod lesen) |
| `env` | Umgebungsvariablen des Ziel-Containers werden übernommen |

Config prüfen, ohne etwas zu starten:

```bash
mirrord verify-config mirrord.json
```

Unbekannte Schlüssel lehnt `verify-config` ab (`failed to parse Json`), Tippfehler fallen
also auf.

## 3. Lokalen Container per mirrord anhängen

```bash
mirrord container -f mirrord.json -- \
  docker run --rm --name nginx-local \
    -v "$PWD/local/index.html:/usr/share/nginx/html/index.html:ro" \
    nginx:1.27
```

Mit Podman lautet der Aufruf `mirrord container -f mirrord.json -- podman run ...`.

In einem zweiten Terminal:

```bash
kubectl -n mirrordtest get pods
# zusätzlich: mirrord-agent-<id>-<id>   1/1   Running

kubectl -n mirrordtest exec curl -c curl -- curl -s http://nginx
# Antwort vom LOKALEN nginx-Container (via mirrord)
```

Zum Beenden `Ctrl+C` im mirrord-Terminal drücken oder `docker stop nginx-local` ausführen. Nach wenigen
Sekunden ist der Agent-Job weg und der Cluster antwortet wieder:

```bash
kubectl -n mirrordtest exec curl -c curl -- curl -s http://nginx
# Antwort aus dem CLUSTER (Pod im Namespace mirrordtest)
kubectl -n mirrordtest get jobs
# No resources found
```

Ein lokales Binary statt eines Containers startet man mit `mirrord exec -f mirrord.json -- <binary>`.

## 4. Mirror-Modus

Der Cluster-Pod antwortet weiter, der lokale Container bekommt eine Kopie jedes Requests.
Damit liest man echten Traffic mit, ohne Antworten zu verändern.

```bash
sed 's/"steal"/"mirror"/' mirrord.json > /tmp/mirrord-mirror.json
mirrord container -f /tmp/mirrord-mirror.json -- \
  docker run --rm --name nginx-local \
    -v "$PWD/local/index.html:/usr/share/nginx/html/index.html:ro" \
    nginx:1.27

kubectl -n mirrordtest exec curl -c curl -- curl -s http://nginx/mirror-test
# Antwort kommt vom Cluster-Pod; im Log von nginx-local erscheint parallel:
# "GET /mirror-test HTTP/1.1" 404 ... "curl/8.11.0"
```

Die Antwort des lokalen Containers wird verworfen.

## 5. Nur markierte Requests stehlen (HTTP-Header-Filter)

In einem geteilten Cluster sollte nicht der gesamte Traffic eines Services auf dem
Laptop eines Entwicklers landen. [mirrord-filter.json](mirrord-filter.json) stiehlt nur
Requests mit dem Header `x-mirrord: <lokaler $USER>`, alles andere bedient weiter der
Cluster-Pod:

```json
"incoming": {
  "mode": "steal",
  "http_filter": {
    "header_filter": "x-mirrord: {{ get_env(name="USER", default="dev") }}"
  }
}
```

`{{ get_env(...) }}` ist mirrords Tera-Templating und wird vor dem JSON-Parsen
ersetzt. Die inneren Anführungszeichen werden deshalb nicht escaped. Editoren
melden die Datei als ungültiges JSON, `mirrord verify-config` akzeptiert sie.

```bash
mirrord container -f mirrord-filter.json -- \
  docker run --rm --name nginx-local \
    -v "$PWD/local/index.html:/usr/share/nginx/html/index.html:ro" \
    nginx:1.27

kubectl -n mirrordtest exec curl -c curl -- curl -s http://nginx
# Antwort aus dem CLUSTER (Pod im Namespace mirrordtest)
kubectl -n mirrordtest exec curl -c curl -- curl -s -H "x-mirrord: $USER" http://nginx
# Antwort vom LOKALEN nginx-Container (via mirrord)
```

Der Filter funktioniert mit Istio nur, weil der Agent hinter dem Sidecar abgreift und
Klartext sieht. Damit der Header Aufrufketten überlebt, muss jeder Service ihn
weiterreichen (wie bei Tracing-Headern).

## 6. Mit minimalen Rechten testen

So lässt sich prüfen, ob die Role aus [rbac/mirrord-user.yaml](rbac/mirrord-user.yaml)
reicht, ohne cluster-admin:

```bash
kubectl apply -f rbac/mirrord-user.yaml

# kubeconfig mit dem Token des ServiceAccounts mirrord-dev bauen
kubectl config view --minify --raw > /tmp/kc-mirrord
TOKEN=$(kubectl -n mirrordtest create token mirrord-dev --duration=1h)
KUBECONFIG=/tmp/kc-mirrord kubectl config set-credentials mirrord-dev --token="$TOKEN"
KUBECONFIG=/tmp/kc-mirrord kubectl config set-context --current --user=mirrord-dev
KUBECONFIG=/tmp/kc-mirrord kubectl auth can-i --list -n mirrordtest

KUBECONFIG=/tmp/kc-mirrord mirrord container -f mirrord.json -- \
  docker run --rm --name nginx-local \
    -v "$PWD/local/index.html:/usr/share/nginx/html/index.html:ro" \
    nginx:1.27
```

Die Role gilt nur in `mirrordtest`. Deshalb muss `agent.namespace` auf `mirrordtest`
stehen, sonst scheitert das Anlegen des Agent-Jobs im Default-Namespace.

## 7. mirrord mit OpenShift (`oc`)

> Dieser Abschnitt ist nicht gegen ein OpenShift-Cluster getestet. Er überträgt die
> obigen Ergebnisse auf die OpenShift-Eigenheiten (SCC, SELinux, CRI-O, Istio-CNI über
> Multus, siehe [ovintegration/](../ovintegration/)).

### Login und kubeconfig

mirrord spricht direkt mit der Kube-API und nutzt dieselbe kubeconfig wie `kubectl`.
`oc login` schreibt den Kontext nach `~/.kube/config` (bzw. `$KUBECONFIG`), mehr ist auf
Client-Seite nicht nötig:

```bash
oc login --server=https://api.<cluster>:6443
oc project mirrordtest          # setzt den Default-Namespace des Kontexts
oc whoami && oc whoami --show-context
```

Ein `oc`-Plugin oder eine OpenShift-spezifische mirrord-Variante gibt es nicht.

### Warum der Agent auf OpenShift extra Rechte braucht

Der Agent-Pod verlangt `hostPID`, hostPath-Volumes (`/run`, `/var`) und die
Capabilities `SYS_ADMIN`, `SYS_PTRACE`, `NET_ADMIN`. Die Standard-SCC `restricted-v2`
lehnt das ab. Auch `anyuid`, die die Mesh-Namespaces für den Sidecar ohnehin haben,
reicht nicht. Nur die SCC `privileged` erlaubt hostPID und hostPath.

Entscheidend ist der ServiceAccount des Agent-Pods, nicht der des Entwicklers. Der
Pod wird vom Job-Controller erzeugt, die SCC-Prüfung läuft gegen den ServiceAccount im
Pod-Spec.

Den `default`-ServiceAccount eines App-Namespaces auf `privileged` zu heben, würde jeden
Pod dort privilegiert starten lassen. Daher einen eigenen Namespace mit eigenem
ServiceAccount nur für Agents anlegen (einmalig, als Cluster-Admin):

```bash
oc apply -f openshift/10-agent-namespace.yaml
# legt an: Namespace mirrord-agents, SA mirrord-agent,
# RoleBinding auf ClusterRole system:openshift:scc:privileged
# gleichwertig zu:
#   oc adm policy add-scc-to-user privileged -z mirrord-agent -n mirrord-agents

# Entwickler-Rechte für eine Gruppe (Platzhalter ${DEV_GROUP} ersetzen)
sed 's|\${DEV_GROUP}|mirrord-devs|g' openshift/20-developer-rbac.yaml | oc apply -f -
oc adm groups new mirrord-devs <user1> <user2>    # falls die Gruppe noch nicht existiert
```

[openshift/20-developer-rbac.yaml](openshift/20-developer-rbac.yaml) teilt die Rechte
aus [rbac/mirrord-user.yaml](rbac/mirrord-user.yaml) auf zwei Namespaces auf: Job,
Log und Port-Forward in `mirrord-agents`, Lesen von Deployment/ReplicaSet/Pod in
`mirrordtest`. Für weitere Ziel-Namespaces nur die Role `mirrord-target-reader` dort
anlegen.

Als Entwickler prüft man die Rechte so.

```bash
oc auth can-i create jobs -n mirrord-agents
oc auth can-i get pods --subresource=portforward -n mirrord-agents
oc auth can-i get deployments -n mirrordtest
```

### Config für OpenShift

[openshift/mirrord-openshift.json](openshift/mirrord-openshift.json):

```json
"agent": {
  "namespace": "mirrord-agents",
  "service_account": "mirrord-agent",
  "privileged": true
}
```

- `agent.namespace` + `agent.service_account`: Agent läuft mit dem SA, der die SCC
  `privileged` hat.
- `agent.privileged: true`: Der Agent-Container läuft dann wirklich privilegiert, unter
  SELinux also als `spc_t`. Ohne das blockiert SELinux auf RHCOS voraussichtlich den
  Zugriff auf den CRI-O-Socket unter `/host/run`, über den der Agent den Ziel-Container
  findet. Die mirrord-Doku nennt die Option für "strict environments".
- Der Header-Filter ist in dieser Config schon gesetzt, weil OpenShift-Cluster meist
  geteilt sind.

Gestartet wird wie gehabt, hier mit Podman.

```bash
mirrord verify-config openshift/mirrord-openshift.json
mirrord container -f openshift/mirrord-openshift.json -- \
  podman run --rm --name nginx-local \
    -v "$PWD/local/index.html:/usr/share/nginx/html/index.html:ro,Z" \
    docker.io/library/nginx:1.27
```

Auf RHEL/Fedora-Rechnern ist meist Podman statt Docker installiert. `:Z` setzt das
SELinux-Label für den Bind-Mount auf dem lokalen Rechner, Podman braucht außerdem den
voll qualifizierten Image-Namen, wenn keine Default-Registry konfiguriert ist.

Danach kontrollieren, welche SCC der Agent bekommen hat.

```bash
oc -n mirrord-agents get jobs,pods
oc -n mirrord-agents describe pod -l app=mirrord | grep -i scc
# erwartet: openshift.io/scc: privileged
```

### Weitere Punkte auf OpenShift

- **Agent-Image aus interner Registry.** Bei eingeschränktem Internetzugang
  `ghcr.io/metalbear-co/mirrord:<version>` spiegeln und in der Config
  `"agent": { "image": "<registry>/mirrord:<version>", "image_pull_secrets": [{ "name": "<secret>" }] }`
  setzen. Version = CLI-Version (`mirrord --version`).
- **Istio-CNI statt `istio-init`.** Auf OpenShift setzt Istio die iptables-Regeln per
  CNI (Multus). Der mirrord-Agent setzt seine eigenen Regeln zur Laufzeit im
  Netzwerk-Namespace des Pods, das ist davon unabhängig.
- **iptables-Backend.** Der Agent erkennt `iptables-legacy` oder `iptables-nft`
  automatisch. Schlägt das fehl (Requests kommen trotz laufendem Agent nicht lokal an),
  `"agent": { "nftables": true }` setzen.
- **Ohne Cluster-Admin geht es nicht.** Namespace, SCC-Binding und Gruppe muss einmalig
  ein Admin anlegen. Danach reichen die Rollen aus `20-developer-rbac.yaml`.

## Aufräumen

```bash
kubectl delete -f rbac/mirrord-user.yaml --ignore-not-found
kubectl delete -f test/curl-pod.yaml --ignore-not-found
kubectl delete -f manifests/ --ignore-not-found     # löscht auch den Namespace mirrordtest
kubectl get jobs -A | grep mirrord-agent            # sollte leer sein

# OpenShift
sed 's|\${DEV_GROUP}|mirrord-devs|g' openshift/20-developer-rbac.yaml | oc delete -f - --ignore-not-found
oc delete -f openshift/10-agent-namespace.yaml --ignore-not-found
```

Hängengebliebene lokale Container (z. B. nach `kill -9` von mirrord):

```bash
docker ps --filter ancestor=ghcr.io/metalbear-co/mirrord-cli:$(mirrord --version | awk '{print $2}')
docker rm -f nginx-local
```
