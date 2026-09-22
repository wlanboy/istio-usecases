# Google-Cloud-Besonderheiten (GKE) fuer diesen Usecase

Dieses Dokument ist das GKE-Pendant zu [`readme.md`](readme.md): dort geht es um
Open-Source-Istio auf **OpenShift** mit **OVN-Kubernetes** als SDN (SCC-Rechte,
`istio-cni` ueber Multus, Route statt LoadBalancer). Hier geht es um dieselbe
Kernfrage, "wie kommt Open-Source-Istio ohne privilegierte App-Pods auf die
jeweilige Plattform", beantwortet fuer **Google Kubernetes Engine (GKE)**, und
das Aequivalent zum dortigen Abschnitt "Warum das nicht einfach
`profile=openshift` ist": was `--set platform=gke` in den rohen Helm-Charts
tatsaechlich bewirkt und wo es (anders als bei OpenShift) schlicht wirkungslos
ist.

## Warum `platform=gke` (und wieso es kein `profile=gke` gibt)

Anders als OpenShift hat GKE **kein** eigenes IstioOperator-Makro-Profil.
Geprueft gegen `manifests/profiles/` im Istio-Quellcode (Release 1.31): dort
existieren `openshift.yaml` und `openshift-ambient.yaml`, aber kein `gke.yaml`.
Anleitungen, die `istioctl install --set profile=gke` zeigen, gibt es in der
offiziellen Doku entsprechend nicht, anders als beim (fuer rohe Helm-Charts
ohnehin veralteten) `profile=openshift` in der Istio-eigenen OpenShift-Doku
(siehe `readme.md`). Der einzige GKE-spezifische Hebel ist der **Helm-Wert**
`platform=gke`, der (wie `platform=openshift`) in jedem Chart die Datei
`files/profile-platform-gke.yaml` einliest.

Geprueft gegen den aktuellen Chart-Quellcode (Release 1.31) setzt
`profile-platform-gke.yaml` in **allen** vier Charts (`base`, `cni`,
`istio-discovery`, `gateway`) denselben Inhalt:

```yaml
cni:
  cniBinDir: "" # intentionally unset for gke to allow template-based autodetection to work
  resourceQuotas:
    enabled: true
resourceQuotas:
  enabled: true
```

Wirksam ist das aber **nur im `istio/cni`-Chart**: nur dessen `values.yaml`
kennt ueberhaupt die Schluessel `cni.cniBinDir` und `cni.resourceQuotas`. Im
`base`-Chart taucht `resourceQuotas` in `values.yaml` gar nicht auf, im
`istiod`-Chart (`istio-control/istio-discovery`) kennt `cni:` nur `enabled`
und `provider`, nicht `cniBinDir`/`resourceQuotas`, und im `gateway`-Chart
existiert weder `cni:` noch `resourceQuotas:` in `values.yaml`. `--set
platform=gke` bei diesen drei Charts mitzugeben schadet nicht (Helm ignoriert
unbekannte Werte), bringt aber auch nichts. Bei OpenShift ist das anders:
`platform=openshift` setzt in mehreren Charts tatsaechlich unterschiedliche
Werte (SELinux-Optionen, CNI-Pfade). Fuer GKE reicht es, `platform=gke` beim
`istio/cni`-Release zu setzen; bei den anderen ist es nur Konsistenz mit dem
OpenShift-Installationsmuster in `readme.md`.

Im Einzelnen bewirkt `platform=gke` im `cni`-Chart:

- **`cniBinDir: ""`**: aktiviert die Auto-Erkennung im Chart-Template
  (`daemonset.yaml`): `/home/kubernetes/bin` statt des Kubernetes-Standards
  `/opt/cni/bin`, sobald `Capabilities.KubeVersion.GitVersion` die
  Zeichenfolge `-gke` enthaelt. Diese Auto-Erkennung existiert laut
  Chart-Kommentar nur aus Kompatibilitaetsgruenden und gilt als *deprecated
  zugunsten des expliziten `gke`-Platform-Profils*. `--set platform=gke`
  sollte also trotz Auto-Erkennung explizit gesetzt werden, statt sich
  darauf zu verlassen. Ein manuell gesetzter `cniBinDir`-Wert ueberschreibt die
  Erkennung in jedem Fall.
- **`resourceQuotas.enabled: true`**: legt ein `ResourceQuota`-Objekt
  `istio-cni-resource-quota` im `istio-cni`-Namespace an, das Pods mit
  `PriorityClass: system-node-critical` (genau die, mit der
  `istio-cni-node` laeuft) bis zum Limit `resourceQuotas.pods` (Default:
  `5000`) erlaubt. Grund: GKE laesst `system-node-critical`-Pods
  standardmaessig nur in bestimmten Namespaces zu (v. a. `kube-system`); ohne
  dieses Quota (oder eine Installation direkt in `kube-system`) bleibt der
  `istio-cni-node`-DaemonSet in einem eigenen `istio-system`- oder
  `istio-cni`-Namespace unschedulable.
- Im `cni`-Chart selbst **nicht** ueber `platform=gke` gesetzt, aber relevant:
  `cni.provider` steht bereits per Default auf `"default"` (nicht
  `"multus"`). Der `istio-cni`-Plugin haengt sich damit direkt als
  *chained CNI plugin* an die bestehende Node-CNI-Konfiguration in
  `/etc/cni/net.d` (Standardwert von `cni.cniConfDir`, auf GKE unveraendert).
  Ein Multus-Meta-Plugin, eine `NetworkAttachmentDefinition` oder eine
  Sidecar-Injector-Annotation wie auf OpenShift (siehe `readme.md`) sind auf
  GKE **nicht** noetig und werden von `platform=gke` auch nicht gesetzt -
  einfach, weil GKE kein Multus verwendet.

## GKE-Netzwerk-Grundlagen: nicht OVN-Kubernetes

`readme.md` behandelt OVN-Kubernetes als SDN. Das ist auf GKE **nicht**
relevant: GKE nutzt VPC-native Cluster mit einer eigenen CNI-Implementierung.
Seit einigen Jahren ist das standardmaessig **GKE Dataplane V2**, eine von
Google verwaltete, Cilium-/eBPF-basierte Variante (ersetzt `kube-proxy` und
wertet `NetworkPolicy` direkt per eBPF aus). Aeltere Cluster koennen noch auf
der Legacy-CNI (iptables-basiert, kubenet/Calico-Add-on) laufen. Fuer
`istio-cni` macht das praktisch keinen Unterschied: `cni.provider=default`
haengt sich in beiden Faellen als chained Plugin an die vorhandene
Node-CNI-Konfiguration, unabhaengig davon, ob Dataplane V2 oder die Legacy-CNI
darunterliegt. Eigene `NetworkPolicy`-Objekte fuer `istiod`-Erreichbarkeit
(Port 15012/15017) sind bei Dataplane V2 genauso noetig wie bei OVN-Kubernetes
auf OpenShift, falls im Cluster ein restriktives Default-Deny gilt. Das ist
unabhaengig vom SDN und betrifft jede Mesh-Installation.

## GKE Standard vs. Autopilot: die eigentliche Weichenstellung

Anders als bei OpenShift (dort ist SCC-Handling fuer alle Cluster gleich)
unterscheidet sich die GKE-Situation stark nach Cluster-Modus:

| | **GKE Standard** | **GKE Autopilot** |
|---|---|---|
| Privilegierte Pods (`istio-cni-node`) | funktioniert wie auf jedem gewoehnlichen Kubernetes: Node-Pools sind normale VMs, kein plattformseitiges Verbot privilegierter DaemonSets, solange kein eigenes Pod-Security-Admission/Gatekeeper-Regelwerk das einschraenkt. | von Google zentral eingeschraenkt: Autopilot erzwingt per Default ein Regelwerk oberhalb der Pod-Security-Standard-Stufe *Baseline* (mit Teilen von *Restricted*); `hostPath`-Volumes im Schreibmodus (die `istio-cni`-Install-Container braucht) sind darin nicht erlaubt. Freigeschaltet werden privilegierte Workloads nur ueber Googles eigenen **AllowlistSynchronizer/WorkloadAllowlist**-Mechanismus (`--autopilot-privileged-admission`), nicht ueber ein Namespace-Label wie bei GKE Standard oder eine SCC wie bei OpenShift. Offenes Upstream-`istio-cni` steht dort nicht automatisch auf der Allowlist. |
| Praktische Konsequenz | `install-istio.sh`-Analogon (siehe unten) laeuft im Prinzip unveraendert. | Selbstverwaltetes Open-Source-`istio-cni` ist auf Autopilot nicht ohne Weiteres lauffaehig (siehe u. a. [istio/istio#37150](https://github.com/istio/istio/issues/37150)). Google empfiehlt fuer Autopilot stattdessen **Cloud Service Mesh** (Googles verwaltetes Mesh-Angebot), das Sidecare ueber einen eigenen, bereits freigeschalteten Mechanismus injiziert, **ohne** dem App-Pod selbst erhoehte Rechte zu geben. |
| Passt zu diesem Usecase? | Ja, direktes Analogon zu `readme.md`. | Nein. Dieser Usecase (Open-Source-Istio, `istio-cni` als eigener DaemonSet) ist auf Autopilot bewusst nicht das empfohlene Setup. Fuer eine Autopilot-Demo waere Cloud Service Mesh das naechste Dokument, nicht dieses. |

Fuer den Rest dieses Dokuments (Installation, Architektur, Troubleshooting)
wird deshalb **GKE Standard** vorausgesetzt, analog zum
OVN-Kubernetes-OpenShift-Fall in `readme.md`.

## Architektur (GKE Standard)

```
Client (extern)
    |
    | HTTPS, direkt gegen die externe IP
    v
Google Cloud Load Balancer (L4, von GKE ueber den Service-Typ LoadBalancer
    provisioniert; TCP-Passthrough auf Node-Ebene, kein eigener HTTP-Layer
    wie der OpenShift-Router)
    |
    v
Service istio-ingressgateway (Typ LoadBalancer statt ClusterIP, Namespace
    istio-system)
    |
    v
Istio Ingress Gateway (Envoy) --- Gateway "ovintegration-gateway" (hosts: "*")
    |
    | VirtualService "ovintegration-nginx" routet auf Service nginx.<namespace>
    v
nginx-Pod (Namespace <namespace>, mit Istio-Sidecar)


Node-Ebene (einmalig pro Cluster):
  Pod-Erstellung --Sidecar-Injector fuegt istio-proxy-Container hinzu--
       |
       v
  istio-cni-node DaemonSet (privilegiert, 1 Pod/Node, chained CNI Plugin,
       provider=default, kein Multus/keine NetworkAttachmentDefinition)
       |
       v
  richtet iptables-Redirect im Pod-Netns ein, OHNE dass der App-Pod selbst
  privilegiert sein muss
```

Zentraler Unterschied zu OpenShift: dort war der Ingress-Gateway-Service
bewusst `ClusterIP`, weil die externe Erreichbarkeit ueber Router/Route kam.
Auf GKE gibt es keine plattformeigene Route-Instanz. Der uebliche Weg ist ein
Service vom Typ `LoadBalancer`, der von GKE automatisch einen externen Google
Cloud Load Balancer samt externer IP provisioniert. Ein TLS-Terminierungspunkt
wie der OpenShift-Router existiert hier nicht automatisch; TLS muesste
entweder direkt am Envoy terminiert werden (analog zu
[`custom-ca-tls.md`](custom-ca-tls.md)) oder ueber einen zusaetzlichen
GKE-Ingress/Gateway-API-Layer vor dem Istio-Gateway.

## Voraussetzungen

- GKE-Cluster im **Standard**-Modus (nicht Autopilot, siehe oben) mit
  Cluster-Admin-Rechten.
- `gcloud` mit gueltigem Kontext (`gcloud container clusters get-credentials
  <cluster> --zone <zone>`) sowie `kubectl`.
- `helm` >= 3.x lokal installiert.
- Falls der Cluster mit einem eigenen, restriktiven Pod-Security-Admission-
  oder Gatekeeper/OPA-Regelwerk betrieben wird: der Namespace fuer
  `istio-cni` (Standard: `kube-system`, oder ein eigener Namespace, siehe
  `resourceQuotas` oben) muss privilegierte Pods weiterhin zulassen. Das ist
  eine reine Cluster-Konfigurationsfrage, keine GKE-Plattformvorgabe wie bei
  Autopilot.

## Installation

Analog zu `install-istio.sh` in `readme.md`, aber ohne `oc`/SCC-Aufrufe (GKE
kennt kein SCC-Konzept, alles laeuft ueber gewoehnliches Kubernetes-RBAC) und
mit `platform=gke` statt `platform=openshift`:

```bash
helm repo add istio https://istio-release.storage.googleapis.com/charts
helm repo update istio

kubectl create namespace istio-system   # falls nicht vorhanden

helm upgrade --install istio-base istio/base -n istio-system \
  --set platform=gke --wait

helm upgrade --install istio-cni istio/cni -n kube-system \
  --set platform=gke
kubectl -n kube-system rollout status daemonset/istio-cni-node --timeout=120s

helm upgrade --install istiod istio/istiod -n istio-system \
  --set platform=gke --set pilot.cni.enabled=true --wait

helm upgrade --install istio-ingressgateway istio/gateway -n istio-system \
  --set platform=gke --set service.type=LoadBalancer --wait
```

`istio-cni` bewusst in `kube-system` (wie im OpenShift-Skript), damit die
`system-node-critical`-PriorityClass ohne zusaetzliches `ResourceQuota`
funktioniert (siehe oben, Alternative waere ein eigener Namespace plus
`--set resourceQuotas.pods=<n>`).

Externe IP des Gateways abfragen:

```bash
kubectl -n istio-system get service istio-ingressgateway \
  -o jsonpath='{.status.loadBalancer.ingress[0].ip}'
```

Kann je nach GCP-Quota/Firewall-Konfiguration einige Sekunden bis Minuten
dauern, bis `EXTERNAL-IP` von `<pending>` auf eine echte Adresse wechselt.

## Troubleshooting

**`istio-cni-node`-Pods bleiben `Pending`, Events zeigen
`FailedScheduling` mit einem Hinweis auf `PriorityClass` oder Quota.**
`system-node-critical`-Pods sind im gewaehlten Namespace nicht erlaubt.
Entweder `istio-cni` (wie oben) in `kube-system` installieren, oder im
Ziel-Namespace `--set resourceQuotas.pods=<n>` setzen und pruefen:
```bash
kubectl get resourcequota -n <namespace>
kubectl describe pod <pod> -n <namespace> | grep -A5 Events
```

**`istio-cni-node`-Pods werden von einem Admission-Webhook/Policy-Controller
abgelehnt (`hostPath`, `privileged`, `NET_ADMIN` o. ae.).** Auf GKE
**Autopilot** erwartbar (siehe Tabelle oben). Dort ist Upstream-`istio-cni`
nicht das empfohlene Setup, siehe [istio/istio#37150](https://github.com/istio/istio/issues/37150).
Auf GKE **Standard** deutet das auf ein eigenes, zusaetzlich installiertes
Pod-Security-Admission- oder Gatekeeper/OPA-Regelwerk hin:
```bash
kubectl get pods -n kube-system -l k8s-app=istio-cni-node
kubectl describe pod <pod> -n kube-system | grep -A10 Events
kubectl get ns kube-system -o jsonpath='{.metadata.labels}'   # PSA-Labels?
```

**Service `istio-ingressgateway` bleibt dauerhaft bei `EXTERNAL-IP:
<pending>`.** Meist GCP-Projekt-Quota fuer externe IP-Adressen/Forwarding-
Rules oder eine fehlende Berechtigung des GKE-Service-Accounts, einen Load
Balancer zu provisionieren. Pruefen mit:
```bash
kubectl -n istio-system describe service istio-ingressgateway
gcloud compute forwarding-rules list
```

**`curl` auf die externe IP liefert nichts / Timeout, obwohl `EXTERNAL-IP`
gesetzt ist.** Firewall-Regel fehlt. GKE legt bei `LoadBalancer`-Services
i. d. R. automatisch passende VPC-Firewall-Regeln an; bei einer manuell
gehaerteten VPC (z. B. restriktive Default-Firewall) muss der Zugriff auf den
Gateway-Node-Port sowie die GCP-eigenen Health-Check-Bereiche
`35.191.0.0/16` und `130.211.0.0/22` explizit erlaubt werden
([LoadBalancer Service parameters](https://docs.cloud.google.com/kubernetes-engine/docs/concepts/service-load-balancer-parameters)).

**`curl` liefert `404`/`503` von Envoy selbst.** Wie im OpenShift-Fall: das
`Gateway`-Objekt selektiert keine Pods, weil `spec.selector` nicht mit den
tatsaechlichen Pod-Labels des Ingress-Gateway-Deployments uebereinstimmt.
Gleiche Pruefung wie in `readme.md`:
```bash
kubectl -n istio-system get pods -l istio=ingressgateway --show-labels
kubectl -n <namespace> get gateway ovintegration-gateway -o jsonpath='{.spec.selector}'
```

## Weiterfuehrend

- [`readme.md`](readme.md): das OpenShift/OVN-Kubernetes-Original dieses
  Usecases, inkl. `wildcard-ingress.md`/`custom-ca-tls.md` fuer TLS direkt am
  Envoy. Beide Dokumente sind plattformunabhaengig und gelten unveraendert
  auch auf GKE.
- [Istio CNI node agent](https://istio.io/latest/docs/setup/additional-setup/cni/):
  offizielle Doku, u. a. zur `system-node-critical`/`ResourceQuota`-Regel.
- [Using GKE Dataplane V2](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/dataplane-v2):
  Hintergrund zur Standard-CNI moderner GKE-Cluster.
- [About privileged workload admission in Autopilot mode](https://docs.cloud.google.com/kubernetes-engine/docs/concepts/about-autopilot-privileged-workloads),
  [Control privileged workload admission in Autopilot mode](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/run-autopilot-partner-workloads):
  der `AllowlistSynchronizer`-Mechanismus, der Autopilot von OpenShifts
  SCC-Modell und GKE Standards unveraenderten RBAC unterscheidet.
