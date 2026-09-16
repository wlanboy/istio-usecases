# OpenShift-Integration Usecase (OVN-Kubernetes)

Installiert **Open-Source-Istio** (Upstream-Helm-Charts von `istio-release.storage.googleapis.com`,
**nicht** der Red-Hat-Operator "OpenShift Service Mesh") auf einem OpenShift-Cluster mit
OVN-Kubernetes als Standard-SDN, und zeigt die dafuer noetige OpenShift-spezifische
Konfiguration: SCC-Rechte, `istio-cni` statt privilegierter Init-Container, sowie die
Anbindung eines Istio Ingress Gateway an eine **OpenShift Route** statt (oder zusaetzlich zu)
einem `LoadBalancer`-Service.

## Warum das nicht einfach `helm install --set profile=openshift` ist

Die Istio-eigene Doku ([`install-OpenShift.md`](https://github.com/istio/istio/blob/master/manifests/charts/install-OpenShift.md),
[istio.io/.../platform-setup/openshift](https://istio.io/latest/docs/setup/platform-setup/openshift/))
nennt durchgaengig `--set profile=openshift`. Das stimmt fuer `istioctl install` (dort ist
`openshift` ein **IstioOperator-Profil**, das intern `global.platform: openshift` setzt), ist
fuer **rohe Helm-Charts** (`helm install ...`) aber inzwischen veraltet/falsch: geprueft gegen
den aktuellen Chart-Quellcode (u. a. Release 1.31.0) gibt es dort **keine**
`files/profile-openshift.yaml` mehr - der Helm-Wert `profile=openshift` fuehrt zu
`Error: unknown profile "openshift"` und bricht die Installation ab. Der tatsaechlich
existierende, wirksame Wert heisst **`platform=openshift`** (laedt
`files/profile-platform-openshift.yaml` in jedem Chart). Alle Skripte hier verwenden deshalb
`--set platform=openshift`.

`platform=openshift` setzt u. a.:

- `pilot.cni.enabled=true`, `cni.provider=multus`, `cni.chained=false` - Traffic-Redirection
  laeuft ueber den `istio-cni`-DaemonSet statt ueber einen privilegierten `istio-init`
  Init-Container in jedem Pod (OpenShift verbietet Pods sonst `NET_ADMIN`/`NET_RAW` per SCC).
- `cni.cniBinDir=/var/lib/cni/bin`, `cni.cniConfDir=/etc/cni/multus/net.d` - die auf OpenShift
  per Multus verwalteten CNI-Pfade statt der Kubernetes-Standardpfade.
- `seLinuxOptions.type=spc_t` fuer die `istio-cni`-DaemonSet-Pods.

## Architektur

```
Client (extern)
    |
    | HTTPS (Route, edge-terminiert am OpenShift-Router)
    v
OpenShift Router (HAProxy)  --Route "ovintegration-<namespace>" in istio-system--
    |
    | HTTP (Route -> Service, Port http2/8080)
    v
Service istio-ingressgateway (ClusterIP, Namespace istio-system)
    |
    v
Istio Ingress Gateway (Envoy) --- Gateway "ovintegration-gateway" (hosts: "*")
    |
    | VirtualService "ovintegration-nginx" routet auf Service nginx.<namespace>
    v
nginx-Pod (Namespace <namespace>, mit Istio-Sidecar)


Node-Ebene (einmalig pro Cluster):
  Pod-Erstellung --Multus liest Annotation k8s.v1.cni.cncf.io/networks: default/istio-cni--
       |          (automatisch vom Sidecar-Injector gesetzt, wenn pilot.cni.provider=multus)
       v
  istio-cni-node DaemonSet (privilegiert, 1 Pod/Node, Namespace kube-system)
       |
       v
  richtet iptables-Redirect im Pod-Netns ein, OHNE dass der App-Pod selbst privilegiert sein muss
```

- Die **OpenShift Route** liegt zwingend im selben Namespace wie der referenzierte Service
  (`istio-ingressgateway`), also in `istio-system` - unabhaengig davon, in welchem Namespace
  der Demo-Workload (`nginx`) laeuft. Mehrere Demo-Namespaces teilen sich denselben Gateway/
  Router und unterscheiden sich nur durch eigene `Route`/`Gateway`/`VirtualService`-Objekte.
- Der Ingress-Gateway-Service laeuft hier bewusst als `ClusterIP` (nicht `LoadBalancer`):
  die externe Erreichbarkeit kommt bereits vom OpenShift-Router/der Route, ein zusaetzlicher
  Cloud-Loadbalancer ist nicht noetig - relevant gerade bei On-Prem-/Bare-Metal-OpenShift auf
  OVN-Kubernetes ohne Cloud-Provider-Integration.
- `istio-cni` ersetzt den privilegierten `istio-init`-Init-Container durch einen einzelnen
  privilegierten DaemonSet-Pod pro Node (Namespace `kube-system`, analog zu OVN-Kubernetes'
  eigenen Node-Komponenten). Auf OpenShift wird er dafuer **nicht** als "chained" CNI-Plugin
  in die bestehende OVN-Kubernetes-Konfiguration eingehaengt (das verbietet OpenShift), sondern
  ueber **Multus** als eigenstaendiges `NetworkAttachmentDefinition` aufgerufen. Der
  Sidecar-Injector setzt dafuer automatisch die Pod-Annotation
  `k8s.v1.cni.cncf.io/networks: default/istio-cni` (`pilot.cni.provider=multus`) - keine
  manuelle Namespace-Konfiguration noetig.
- OVN-Kubernetes selbst bleibt die primaere CNI fuer Pod-IP-Vergabe und Kubernetes-`NetworkPolicy`;
  `istio-cni` haengt sich nur zusaetzlich ein, um im Pod-Netzwerk-Namespace die iptables-Regeln
  fuer den Envoy-Sidecar zu setzen. Eigene `NetworkPolicy`-Objekte (OVN-Kubernetes wertet diese
  nativ aus) muessen weiterhin Traffic zu `istiod` (Port 15012, 15017) und zwischen Sidecars
  erlauben, falls im Cluster ein restriktives Default-Deny gilt - das ist unabhaengig von Istio
  und betrifft jede Mesh-Installation auf einem Cluster mit aktiven `NetworkPolicy`-Objekten.

## Voraussetzungen

- OpenShift-Cluster mit Cluster-Admin-Rechten (fuer `oc adm policy`, CRDs, DaemonSet in
  `kube-system`) und OVN-Kubernetes als Netzwerk-Plugin (Standard seit OpenShift 4.14+).
- `oc` mit gueltigem Kontext auf diesen Cluster (`oc whoami`, `oc get clusterversion`).
- `helm` >= 3.x lokal installiert.
- Kein bereits installiertes Red Hat OpenShift Service Mesh (Operator) im selben Cluster -
  kollidiert mit einer parallelen Open-Source-Istio-Installation (gleiche CRDs/Webhooks).

## Installation

### 1. Istio-Plattform installieren (einmalig pro Cluster)

```bash
./install-istio.sh
```

Fuehrt intern aus:

```bash
helm repo add istio https://istio-release.storage.googleapis.com/charts
helm repo update istio

oc create namespace istio-system                                    # falls nicht vorhanden
oc adm policy add-scc-to-group anyuid system:serviceaccounts:istio-system

helm upgrade --install istio-base istio/base -n istio-system \
  --set platform=openshift --wait

helm upgrade --install istio-cni istio/cni -n kube-system \
  --set platform=openshift
oc adm policy add-scc-to-user privileged -z istio-cni -n kube-system
oc -n kube-system rollout status daemonset/istio-cni-node --timeout=120s

helm upgrade --install istiod istio/istiod -n istio-system \
  --set platform=openshift --set pilot.cni.enabled=true --wait

helm upgrade --install istio-ingressgateway istio/gateway -n istio-system \
  --set platform=openshift --set service.type=ClusterIP --wait
```

**Warum `anyuid`-SCC?** Der Istio-Sidecar (`istio-proxy`) laeuft fest mit `runAsUser: 1337`
(hartcodiert im Sidecar-Injection-Template, unabhaengig von `platform=openshift`). OpenShifts
Standard-SCC `restricted-v2` erlaubt aber nur eine dem Projekt automatisch zugewiesene
UID-Range (`MustRunAsRange`), nicht eine feste UID wie 1337. Ohne `anyuid`-SCC fuer die
ServiceAccounts in `istio-system` (Control Plane, Gateway) und im jeweiligen Mesh-Namespace
(siehe `install.sh` unten) werden die Pods mit `unable to validate against any security
context constraint` abgelehnt.

**Warum `privileged`-SCC nur fuer `istio-cni`?** Nur der `istio-cni-node`-DaemonSet braucht
echte Node-Rechte (fremde Pod-Netzwerk-Namespaces umkonfigurieren). Alle anderen
Istio-Komponenten kommen mit `anyuid` aus - das ist der ganze Sinn von `istio-cni`: die
elevated privileges wandern aus jedem einzelnen App-Pod in einen einzigen, zentral
kontrollierten DaemonSet.

### 2. Demo-Usecase installieren (pro Namespace, wiederholbar)

```bash
./install.sh                          # Standard-Namespace: ovintegration-demo
./install.sh mein-namespace           # eigenen Namespace verwenden
```

Legt (falls noch nicht vorhanden) den Namespace mit `istio-injection: enabled` an, vergibt
`anyuid`-SCC fuer dessen ServiceAccounts, deployt ein `nginx` als Testziel und legt
`Gateway`/`VirtualService` (im Demo-Namespace) sowie eine `Route` (zwingend in `istio-system`,
da sie auf den dortigen `istio-ingressgateway`-Service zeigt) an. Die Route bekommt **keinen**
festen Hostnamen - OpenShift vergibt automatisch einen unter der Cluster-Wildcard-Domain
(`<name>-<namespace>.apps.<cluster-domain>`), das Skript liest ihn zurueck und gibt ihn aus.

Existiert der Namespace bereits, wird `00-namespace.yaml` uebersprungen; alle anderen
Manifeste werden trotzdem (erneut) appliziert.

## Test ausfuehren

```bash
./run.sh                         # Standard-Namespace: ovintegration-demo
./run.sh mein-namespace          # eigenen Namespace verwenden
```

Liest den Hostnamen der zugehoerigen Route aus `istio-system`, ruft ihn per `curl -k https://...`
auf und prueft zwei Dinge: HTTP-Status 200 **und** den Response-Header `server: istio-envoy`.
Letzterer beweist, dass die Antwort tatsaechlich durch den Istio-Ingress-Gateway-Envoy
(und nicht direkt von nginx) gelaufen ist - der OpenShift-Router selbst faelscht/entfernt
diesen Header nicht.

## Troubleshooting

**`Error: unknown profile "openshift"` beim `helm install`.** `profile=openshift` statt
`platform=openshift` verwendet - siehe Abschnitt oben. Betrifft auch `istioctl`-Beispiele, die
1:1 auf Helm uebertragen wurden.

**Pods in `istio-system`/Demo-Namespace bleiben im Status `Pending`/`CrashLoopBackOff`,
Events zeigen `unable to validate against any security context constraint`.**
`anyuid`-SCC fehlt fuer die ServiceAccounts des betroffenen Namespace. Pruefen mit:
```bash
oc get scc anyuid -o jsonpath='{.groups}'
oc describe pod <pod> -n <namespace> | grep -A5 Events
```
Nachtraeglich vergeben: `oc adm policy add-scc-to-group anyuid system:serviceaccounts:<namespace>`.

**`istio-cni-node`-DaemonSet-Pods starten nicht / `0/<n>` Ready.**
`privileged`-SCC fehlt fuer die ServiceAccount `istio-cni` in `kube-system`. Pruefen mit:
```bash
oc get scc privileged -o jsonpath='{.users}'
oc -n kube-system get pods -l k8s-app=istio-cni-node
```

**App-Pod haengt in `ContainerCreating`, `istio-proxy`-Container fehlt komplett oder Pod
haengt mit `network: plugin type="istio-cni" failed`.** Multus findet das
`NetworkAttachmentDefinition` `istio-cni` nicht (liegt standardmaessig im Namespace `default`
und wird per `k8s.v1.cni.cncf.io/networks: default/istio-cni`-Annotation referenziert - diese
Annotation setzt der Sidecar-Injector automatisch, sofern `pilot.cni.provider=multus` beim
`istiod`-Install gesetzt war). Pruefen mit:
```bash
oc get network-attachment-definitions.k8s.cni.cncf.io -A
oc get pod <pod> -n <namespace> -o jsonpath='{.metadata.annotations.k8s\.v1\.cni\.cncf\.io/networks}'
```

**`curl` auf die Route liefert `503 Service Unavailable` vom Router (nicht von Envoy).**
Der `istio-ingressgateway`-Service/-Pod ist nicht bereit oder die Route zeigt auf den falschen
Port. Pruefen mit:
```bash
oc -n istio-system get pods -l app=istio-ingressgateway
oc -n istio-system get route ovintegration-<namespace> -o yaml
```

**`curl` liefert HTTP 200, aber ohne `server: istio-envoy`.** Die Route hat direkt auf ein
Ziel geroutet, das nicht der Ingress-Gateway ist (z. B. falscher Service-Name in
`22-route.yaml`), oder ein anderes Gateway/eine andere Route im Cluster hat Vorrang.

## Aufraeumen

```bash
./uninstall.sh                   # Demo-Usecase, Standard-Namespace: ovintegration-demo
./uninstall.sh mein-namespace    # eigenen Namespace verwenden
```

Entfernt Route, Gateway, VirtualService und nginx-Deployment/-Service/-ConfigMap - der
Namespace selbst sowie die Istio-Plattform bleiben bestehen (koennten von weiteren
Demo-Namespaces mitgenutzt werden).

```bash
./uninstall-istio.sh             # komplette Istio-Plattform entfernen (alle Demo-Namespaces vorher aufraeumen!)
```

Entfernt alle Helm-Releases (`istio-ingressgateway`, `istiod`, `istio-cni`, `istio-base`) und
die vergebenen SCC-Bindings. CRDs bleiben laut Helm-Konvention bewusst erhalten (siehe
Skript-Ausgabe fuer den manuellen vollstaendigen Cleanup inkl. CRDs und Namespace).
