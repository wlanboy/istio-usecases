# Primary-Primary Service Mesh zwischen OpenShift und Google Cloud (GKE) auf unterschiedlichen Netzwerken

Dieses Dokument baut wie [`federation.md`](federation.md) auf zwei bereits laufenden,
unabhaengigen Istio-Installationen auf: [`readme.md`](readme.md) (Open-Source-Istio auf
OpenShift/OVN-Kubernetes) und [`readme-googlecloud.md`](readme-googlecloud.md) (dieselbe
Istio-Version auf GKE Standard). Anders als dort werden beide Seiten hier aber nicht zu zwei
eigenstaendigen, nur punktuell verbundenen Meshes zusammengeschaltet, sondern zu **einem
einzigen Mesh mit zwei gleichberechtigten Kontrollebenen** - der Topologie, die die
Istio-Dokumentation "Multi-Primary on different networks" nennt
([Install Multi-Primary on different networks](https://istio.io/latest/docs/setup/install/multicluster/multi-primary_multi-network/)).
Jeder Cluster behaelt seine eigene, voll funktionsfaehige `istiod`, aber beide teilen sich
dieselbe `meshID` und dieselbe Trust-Domain. Services mit identischem Namespace und Namen werden
**automatisch clusterübergreifend zusammengefuehrt** - ohne Export-/Import-Schritt.

## Wann dieses Dokument statt `federation.md`

Die Architekturentscheidung wurde bereits in [`istio-multi-cloud.md`](../istio-multi-cloud.md)
in der Vergleichstabelle "Multi-Primary, Primary-Remote oder Mesh Federation" hergeleitet und
dort explizit **gegen** Multi-Primary fuer den Fall "gezielt ausgewaehlte Dienste sichtbar,
Rest strikt isoliert" entschieden, weil Multi-Primary standardmaessig **default-allow** ist:
jeder Namespace beider Cluster wird beobachtet, jeder Service mit gleichem Namespace/Namen
automatisch ueber die Cluster-Grenze zusammengefuehrt. Dieses Dokument existiert trotzdem, weil
Multi-Primary einen Vorteil bietet, den Federation grundsaetzlich nicht hat: **automatisches,
transparentes Cross-Cluster-Failover** ohne eigene Export-/Import-Pflege pro Service - sinnvoll,
wenn beide Cluster tatsaechlich denselben Anwendungs-Bestand redundant betreiben sollen (aktiv-aktiv
HA ueber Cloud-Grenzen hinweg), nicht nur einzelne Dienste punktuell teilen wollen.

**Governance-Konsequenz, falls hier eingesetzt:** ohne Gegenmassnahme ist jeder Service in jedem
Namespace beider Cluster fuer die jeweils andere Seite sichtbar. Schritt 5 unten
(`discoverySelectors`) schraenkt das auf explizit markierte Namespaces ein - ist aber ein
zentraler, mesh-weiter Schalter pro Kontrollebene, kein Service-granularer Allowlist-Mechanismus
wie die Export-Regeln in `federation.md`. Wer eine harte, Service-fuer-Service gepruefte
Sichtbarkeitsgrenze braucht, ist mit `federation.md` weiterhin besser bedient.

## Voraussetzungen

- OpenShift-Seite: `readme.md` vollstaendig durchgelaufen (`istiod` laeuft mit
  `pilot.cni.enabled=true`), Cluster-Admin-Rechte.
- GKE-Seite: `readme-googlecloud.md` vollstaendig durchgelaufen (GKE **Standard**, nicht
  Autopilot), Cluster-Admin-Rechte.
- `openssl`, `helm` >= 3.x, `istioctl` (Release 1.31, passend zu den Helm-Charts aus `readme.md`/
  `readme-googlecloud.md`) sowie `oc`/`kubectl` mit Kontext auf beide Cluster gleichzeitig
  verfuegbar (unten als `oc-openshift`/`kubectl-gke`/`helm-openshift`/`helm-gke` bezeichnet,
  analog zu `federation.md`).
- **Beidseitiger Zugriff auf den Kubernetes-API-Server der Gegenseite.** Das ist der zentrale
  Unterschied zu `federation.md`: Multi-Primary synchronisiert die Service-Registry ueber
  sogenannte Remote Secrets - jede Kontrollebene braucht Lesezugriff (List/Watch auf Services,
  Endpoints, Nodes, Pods) auf den API-Server der Gegenseite. Beide API-Server muessen also vom
  jeweils anderen Cluster aus erreichbar sein (Netzwerkpfad **und** RBAC), nicht nur die
  East-West-Gateways.
- Beide East-West-Gateways muessen von der jeweils anderen Seite aus per **direktem L4-Zugriff**
  erreichbar sein - nicht ueber einen HTTP(S)-Layer-7-Proxy (siehe Architektur-Abschnitt unten,
  Warum-nicht-Route). Bei On-Prem-OpenShift ohne Cloud-Load-Balancer bedeutet das: `NodePort`
  plus ein Netzwerkpfad zu den Node-IPs, z. B. der WireGuard-Tunnel aus `istio-multi-cloud.md`,
  Abschnitt "Das eigentliche Problem: Netzwerk-Konnektivitaet". Bei Cloud-gehostetem OpenShift
  (ROSA/ARO/selbstverwaltet auf einer Cloud mit LoadBalancer-Integration) reicht stattdessen ein
  gewoehnlicher `LoadBalancer`-Service, analog zur GKE-Seite.

## Architektur

```
OpenShift-Mesh (cluster: openshift, network: openshift-network)   GKE-Mesh (cluster: gke, network: gke-network)
istio-system                                                      istio-system
+--------------------------------------+                          +--------------------------------------+
| istiod (meshID: ovintegration-mesh)  |<==== Remote Secret ======>| istiod (meshID: ovintegration-mesh)  |
|   lokal: eigener API-Server           |   (Lesezugriff auf       |   lokal: eigener API-Server           |
|   remote: gke-Cluster via Remote      |    Services/Endpoints/   |   remote: openshift-Cluster via       |
|   Secret, Service-Merge automatisch   |    Pods/Nodes)           |   Remote Secret, Service-Merge        |
|                                        |                          |   automatisch                         |
| istio-eastwestgateway                 |<==== mTLS TLS-Passthrough====>| istio-eastwestgateway             |
|   Service NodePort (kein Cloud-LB)    |  Port 15443, SNI "*.local"|   Service LoadBalancer               |
|   ISTIO_META_REQUESTED_NETWORK_VIEW:  |  (AUTO_PASSTHROUGH,      |   (direkt oeffentlich erreichbar)     |
|   openshift-network                   |   kein Router/Route      |   ISTIO_META_REQUESTED_NETWORK_VIEW:  |
|                                        |   dazwischen)             |   gke-network                         |
| nginx (Namespace ovintegration-demo)  |                          | nginx (Namespace ovintegration-demo)  |
|   automatisch Teil der gemeinsamen    |----- automatisch --------->|   automatisch als Endpoint des        |
|   Service-Registry, sobald Namespace  |   Service-Merge, kein    |   gemeinsamen Service sichtbar,       |
|   von discoverySelectors erfasst wird |   Export-Schritt noetig  |   gleicher Name+Namespace = ein Dienst|
+--------------------------------------+                          +--------------------------------------+
```

Zentraler Unterschied zu Mesh Federation: **beide** Kontrollebenen brauchen Lesezugriff auf den
Kubernetes-API-Server der jeweils anderen Seite (Remote Secret), und Services werden ohne
expliziten Export-Schritt automatisch zusammengefuehrt, sobald `discoverySelectors` (Schritt 5)
sie nicht ausschliesst.

### Warum das East-West-Gateway nicht wie in `federation.md` ueber eine Route laeuft

Der Ingress-Gateway in `readme.md` und das Federation-Gateway in `federation.md` funktionieren
hinter einer OpenShift-Route, weil in beiden Faellen die TLS-SNI, mit der ein Client verbindet,
ein **stabiler, vorher bekannter Hostname** ist (die generierte Route-URL bzw. die vom
Federation-Controller konfigurierte Peer-Adresse) - genau das, worauf der Router seine
Passthrough-Weiterleitung stuetzt (SNI muss `spec.host` der Route entsprechen).

Das East-West-Gateway fuer Multi-Primary funktioniert anders: das `Gateway`-Objekt aus Schritt 3
matcht auf `hosts: ["*.local"]`, und die SNI, mit der der jeweils andere Cluster tatsaechlich
verbindet, ist eine von Envoy intern erzeugte, pro Zielservice wechselnde Zeichenkette (AUTO_PASSTHROUGH-Konvention),
kein stabiler, extern registrierbarer Hostname. Ein vorgeschalteter Layer-7/Router-Proxy kann
diese SNI nicht gegen einen festen Route-Host matchen. Die Istio-Dokumentation selbst nennt das
explizit: "Layer 7 load balancers terminate TLS and are incompatible with `AUTO_PASSTHROUGH`"
([Install Multi-Primary on different networks](https://istio.io/latest/docs/setup/install/multicluster/multi-primary_multi-network/)).
Das East-West-Gateway braucht deshalb einen echten L4-Pfad direkt zum Service (`LoadBalancer`
oder `NodePort`), keine `Route`.

## Installation

### Schritt 1: Root-CA und zwei eigene Sub-CAs

Identisches Vorgehen wie in [`federation.md`](federation.md), Schritt 1 (gemeinsamer Root, zwei
eigene Intermediates - Option 1 aus der Trust-Tabelle in `istio-multi-cloud.md`). Fuer
Multi-Primary ist ein gemeinsamer Vertrauensanker nicht nur eine Option, sondern praktisch
zwingend: automatische Service-Zusammenfuehrung verlangt uebereinstimmende SPIFFE-Identitaeten
(`spiffe://cluster.local/ns/<ns>/sa/<sa>`), was einen gemeinsamen Trust-Anchor voraussetzt.

```bash
mkdir -p ~/ovintegration-primary-ca && cd ~/ovintegration-primary-ca
wget https://raw.githubusercontent.com/istio/istio/release-1.31/tools/certs/common.mk
wget https://raw.githubusercontent.com/istio/istio/release-1.31/tools/certs/Makefile.selfsigned.mk

make -f Makefile.selfsigned.mk \
  ROOTCA_CN="ovintegration Root CA" ROOTCA_ORG=ovintegration.example \
  root-ca

make -f Makefile.selfsigned.mk \
  INTERMEDIATE_CN="ovintegration OpenShift Sub-CA" INTERMEDIATE_ORG=ovintegration.example \
  openshift-cacerts

make -f Makefile.selfsigned.mk \
  INTERMEDIATE_CN="ovintegration GKE Sub-CA" INTERMEDIATE_ORG=ovintegration.example \
  gke-cacerts

make -f common.mk clean
```

Details zu den erzeugten Dateien (`ca-key.pem`, `ca-cert.pem`, `root-cert.pem`,
`cert-chain.pem`) und zur sicheren Aufbewahrung: siehe `federation.md`, Schritt 1.

### Schritt 2: Sub-CA, Netzwerk-Label und Mesh-Identitaet einspielen

Das `cacerts`-Secret ist identisch zu `federation.md`, Schritt 2:

```bash
# OpenShift
oc-openshift create secret generic cacerts -n istio-system \
  --from-file=root-cert.pem=openshift/root-cert.pem \
  --from-file=ca-cert.pem=openshift/ca-cert.pem \
  --from-file=ca-key.pem=openshift/ca-key.pem \
  --from-file=cert-chain.pem=openshift/cert-chain.pem

# GKE
kubectl-gke create secret generic cacerts -n istio-system \
  --from-file=root-cert.pem=gke/root-cert.pem \
  --from-file=ca-cert.pem=gke/ca-cert.pem \
  --from-file=ca-key.pem=gke/ca-key.pem \
  --from-file=cert-chain.pem=gke/cert-chain.pem
```

Zusaetzlich braucht jede Seite fuer Multi-Primary drei aufeinander abgestimmte Werte
([Install Multi-Primary on different networks](https://istio.io/latest/docs/setup/install/multicluster/multi-primary_multi-network/)):

- `global.meshID`: **identisch** auf beiden Seiten (`ovintegration-mesh`) - markiert beide
  Kontrollebenen als Teil desselben Mesh.
- `global.multiCluster.clusterName`: **je Cluster eindeutig** (`openshift`/`gke`) - Istio nutzt
  das u. a. fuer Locality-Load-Balancing und zur Unterscheidung in Kiali/Zipkin.
- `global.network`: **je Cluster eindeutig** (`openshift-network`/`gke-network`), wie bereits in
  `federation.md` verwendet.

Zusaetzlich der Namespace-Label `topology.istio.io/network`, den die automatische
Multi-Network-Endpoint-Erkennung auswertet:

```bash
oc-openshift label namespace istio-system topology.istio.io/network=openshift-network
kubectl-gke label namespace istio-system topology.istio.io/network=gke-network
```

`helm upgrade` uebernimmt keine vorher gesetzten `--set`-Werte automatisch - alle bereits in
`readme.md`/`readme-googlecloud.md` gesetzten Flags muessen hier erneut mitgegeben werden:

```bash
# OpenShift
helm-openshift upgrade --install istiod istio/istiod -n istio-system \
  --set platform=openshift --set pilot.cni.enabled=true \
  --set global.meshID=ovintegration-mesh \
  --set global.multiCluster.clusterName=openshift \
  --set global.network=openshift-network --wait
oc-openshift -n istio-system rollout restart deployment/istiod
oc-openshift -n istio-system rollout status deployment/istiod --timeout=120s

# GKE
helm-gke upgrade --install istiod istio/istiod -n istio-system \
  --set platform=gke --set pilot.cni.enabled=true \
  --set global.meshID=ovintegration-mesh \
  --set global.multiCluster.clusterName=gke \
  --set global.network=gke-network --wait
kubectl-gke -n istio-system rollout restart deployment/istiod
kubectl-gke -n istio-system rollout status deployment/istiod --timeout=120s
```

Der `rollout restart` erzwingt, dass `istiod` das neue `cacerts`-Secret sofort verwendet (siehe
Erklaerung in `federation.md`, Schritt 2). Bereits laufende Sidecars (`nginx` aus `readme.md`/
`readme-googlecloud.md`) holen sich neue Zertifikate automatisch nach; fuer den Demo-Usecase
erzwingt `kubectl rollout restart deployment -n ovintegration-demo nginx` das sofort.

### Schritt 3: East-West-Gateway ausrollen

Anders als `federation.md` (dort ein manuell geschriebenes Deployment/Service-Manifest) reicht
hier der bereits vorhandene `istio/gateway`-Helm-Chart direkt: der Wert `networkGateway` setzt in
einem Rutsch die vier East-West-Ports (15021, 15443, 15012, 15017), das
`topology.istio.io/network`-Label auf Service **und** Deployment sowie die Env-Variable
`ISTIO_META_REQUESTED_NETWORK_VIEW`
([`gateway`-Chart, `templates/service.yaml`/`templates/deployment.yaml`](https://github.com/istio/istio/blob/release-1.31/manifests/charts/gateway/templates/service.yaml),
Release 1.31 gegen den Chart-Quellcode geprueft).

```bash
# OpenShift: kein Cloud-LB vorausgesetzt (siehe Voraussetzungen), daher NodePort
helm-openshift install istio-eastwestgateway istio/gateway -n istio-system \
  --set platform=openshift \
  --set networkGateway=openshift-network \
  --set service.type=NodePort --wait
oc-openshift -n istio-system rollout status deployment/istio-eastwestgateway --timeout=90s

# GKE: LoadBalancer, direkt oeffentlich erreichbar
helm-gke install istio-eastwestgateway istio/gateway -n istio-system \
  --set platform=gke \
  --set networkGateway=gke-network \
  --set service.type=LoadBalancer --wait
kubectl-gke -n istio-system rollout status deployment/istio-eastwestgateway --timeout=90s
```

Die Standard-`anyuid`-SCC-Gruppenzuweisung aus `readme.md`, Schritt 1
(`system:serviceaccounts:istio-system`) deckt die ServiceAccount dieses zusaetzlichen Gateways
bereits ab - keine weitere SCC-Vergabe noetig, anders als beim `istio-cni`-DaemonSet.

Danach in beiden Clustern das `Gateway`-Objekt anlegen, das dem East-West-Gateway den
AUTO_PASSTHROUGH-Modus fuer `*.local`-Hosts zuweist (Original:
[`samples/multicluster/expose-services.yaml`](https://github.com/istio/istio/blob/release-1.31/samples/multicluster/expose-services.yaml)):

```bash
cat <<'EOF' | oc-openshift apply -n istio-system -f -
apiVersion: networking.istio.io/v1
kind: Gateway
metadata:
  name: cross-network-gateway
spec:
  selector:
    istio: eastwestgateway
  servers:
    - port:
        number: 15443
        name: tls
        protocol: TLS
      tls:
        mode: AUTO_PASSTHROUGH
      hosts:
        - "*.local"
EOF

kubectl-gke apply -n istio-system -f - <<'EOF'
apiVersion: networking.istio.io/v1
kind: Gateway
metadata:
  name: cross-network-gateway
spec:
  selector:
    istio: eastwestgateway
  servers:
    - port:
        number: 15443
        name: tls
        protocol: TLS
      tls:
        mode: AUTO_PASSTHROUGH
      hosts:
        - "*.local"
EOF
```

Von den vier Ports, die der Helm-Chart ueber `networkGateway` konfiguriert, wird fuer
Multi-Primary tatsaechlich nur **15443** gebraucht (der eigentliche Datenverkehr). 15012/15017
sind nur bei Primary-Remote relevant (Zugriff eines ohne eigene Kontrollebene laufenden Remote-
Clusters auf die entfernte `istiod`) und bleiben hier ungenutzt, aber unschaedlich offen.

Erreichbarkeit pruefen:

```bash
# OpenShift: NodePort ermitteln, dazu eine erreichbare Node-IP (z. B. ueber den WireGuard-Tunnel)
oc-openshift -n istio-system get svc istio-eastwestgateway \
  -o jsonpath='{.spec.ports[?(@.name=="tls")].nodePort}{"\n"}'

# GKE: externe IP des LoadBalancer
kubectl-gke -n istio-system get svc istio-eastwestgateway \
  -o jsonpath='{.status.loadBalancer.ingress[0].ip}{"\n"}'
```

### Schritt 4: Remote Secrets austauschen

Der einzige Schritt in diesem Dokument, der `istioctl` statt `helm` braucht - es gibt dafuer
keinen Helm-Chart-Ersatz. `istioctl create-remote-secret` liest das Token der
`istio-reader-service-account`, die der `istio/base`-Chart bereits standardmaessig anlegt
(`global.enableReaderRBAC` ist per Default `true`,
[`base`-Chart, `templates/reader-serviceaccount.yaml`](https://github.com/istio/istio/blob/release-1.31/manifests/charts/base/templates/reader-serviceaccount.yaml)),
und baut daraus ein kubeconfig-basiertes Secret, das die jeweils andere Kontrollebene zum
Lesezugriff auf diesen API-Server verwendet:

```bash
istioctl create-remote-secret --context="<openshift-context>" --name=openshift \
  | oc-openshift apply -f -

istioctl create-remote-secret --context="<gke-context>" --name=gke \
  | kubectl-gke apply -f -
```

Beide `istiod` erkennen das neue Secret (Label `istio/multiCluster: "true"`, fester Namespace
`istio-system`) automatisch ueber ihren laufenden Secret-Watcher, ein Neustart ist nicht noetig.
Verbindung pruefen:

```bash
oc-openshift -n istio-system logs deploy/istiod | grep -i "adding cluster\|remote cluster"
kubectl-gke -n istio-system logs deploy/istiod | grep -i "adding cluster\|remote cluster"
```

### Schritt 5: `discoverySelectors` setzen (empfohlen, siehe Governance-Hinweis oben)

Ohne diesen Schritt beobachtet jede Kontrollebene **alle** Namespaces der Gegenseite. Um das auf
bewusst freigegebene Namespaces einzuschraenken, ein Label vergeben und beide `istiod` per
`meshConfig.discoverySelectors` darauf einschraenken
([Discovery selectors in Istio 1.10](https://tetrate.io/blog/discovery-selectors),
[Istio-Blog: discovery selectors](https://istio.io/latest/blog/2021/discovery-selectors/)):

```bash
oc-openshift label namespace ovintegration-demo multicluster=shared
kubectl-gke label namespace ovintegration-demo multicluster=shared
```

```yaml
# values-discovery-selectors.yaml, auf beiden Seiten identisch per --values mitgegeben
meshConfig:
  discoverySelectors:
    - matchLabels:
        multicluster: shared
```

```bash
helm-openshift upgrade istiod istio/istiod -n istio-system --reuse-values \
  --values values-discovery-selectors.yaml --wait
helm-gke upgrade istiod istio/istiod -n istio-system --reuse-values \
  --values values-discovery-selectors.yaml --wait
```

`--reuse-values` erhaelt hier bewusst die in Schritt 2 gesetzten Werte (`meshID`, `network`,
`clusterName`, `platform`) - anders als beim expliziten Neusetzen in Schritt 2/3, weil hier
gezielt nur ein zusaetzlicher Wert ergaenzt wird. Ist `discoverySelectors` einmal gesetzt, bleibt
jeder Namespace ohne das Label `multicluster=shared` auf beiden Seiten unsichtbar fuer die
Gegenseite - unabhaengig davon, ob er einen gleichnamigen Service enthaelt.

## Testen

Offizieller Istio-HelloWorld-Test fuer Cross-Cluster-Load-Balancing
([Verify the installation](https://istio.io/latest/docs/setup/install/multicluster/verify/)),
angepasst auf `oc`/`kubectl`:

```bash
oc-openshift create namespace sample
oc-openshift label namespace sample istio-injection=enabled multicluster=shared
kubectl-gke create namespace sample
kubectl-gke label namespace sample istio-injection=enabled multicluster=shared

git clone --depth 1 --branch release-1.31 https://github.com/istio/istio.git /tmp/istio-samples

oc-openshift apply -n sample -f /tmp/istio-samples/samples/helloworld/helloworld.yaml -l service=helloworld
oc-openshift apply -n sample -f /tmp/istio-samples/samples/helloworld/helloworld.yaml -l version=v1
kubectl-gke apply -n sample -f /tmp/istio-samples/samples/helloworld/helloworld.yaml -l service=helloworld
kubectl-gke apply -n sample -f /tmp/istio-samples/samples/helloworld/helloworld.yaml -l version=v2

oc-openshift apply -n sample -f /tmp/istio-samples/samples/curl/curl.yaml

for i in $(seq 1 6); do
  oc-openshift exec -n sample -c curl \
    "$(oc-openshift get pod -n sample -l app=curl -o jsonpath='{.items[0].metadata.name}')" \
    -- curl -sS helloworld.sample:5000/hello
done
```

Erwartung: die Antworten wechseln zwischen `Hello version: v1, instance: helloworld-v1-...`
(laeuft nur auf OpenShift) und `Hello version: v2, instance: helloworld-v2-...` (laeuft nur auf
GKE), obwohl der `curl`-Aufruf ausschliesslich lokal gegen `helloworld.sample:5000` auf der
OpenShift-Seite geht - der Service ist durch die automatische Zusammenfuehrung ein einziger
logischer Dienst mit Endpoints aus beiden Clustern.

## Troubleshooting

**`istiod`-Logs zeigen keinen `remote cluster`-Eintrag, obwohl Schritt 4 durchgelaufen ist.**
Remote-Secret wurde im falschen Cluster appliziert (Verwechslung von `--context` und
Ziel-`kubectl`/`oc`), oder das Secret hat nicht das erwartete Label. Pruefen mit:
```bash
oc-openshift -n istio-system get secret -l istio/multiCluster=true
kubectl-gke -n istio-system get secret -l istio/multiCluster=true
```

**Cross-Cluster-Endpoints tauchen in `istioctl proxy-config endpoint` nicht auf, Remote Secret
und East-West-Gateway sehen beide unauffaellig aus.** Meistens `discoverySelectors` (Schritt 5)
ausgeschlossen, weil der Ziel-Namespace das Label nicht traegt, oder `topology.istio.io/network`
auf dem `istio-system`-Namespace der Gegenseite fehlt (Schritt 2). Pruefen mit:
```bash
istioctl proxy-config endpoint <pod>.<namespace> --context="<context>" | grep <service>
oc-openshift get namespace ovintegration-demo --show-labels
kubectl-gke get namespace istio-system --show-labels
```

**TLS-Handshake-Fehler zwischen den East-West-Gateways, Envoy-Logs zeigen `TLS error`.** Wie in
`federation.md`: unterschiedliche Sub-CAs ohne gemeinsamen Root, oder `cacerts` wurde nach dem
letzten `istiod`-Start angelegt (Rollout-Restart in Schritt 2 fehlt). Pruefen mit
`openssl verify -CAfile root-cert.pem openshift/ca-cert.pem` und das GKE-Pendant.

**`curl` haengt/Timeout trotz sichtbarem Remote-Endpoint.** Der L4-Pfad zum East-West-Gateway der
Gegenseite fehlt (Firewall vor dem GKE-`LoadBalancer`, oder der `NodePort` auf OpenShift ist ueber
den WireGuard-Tunnel nicht erreichbar) - unabhaengig vom Mesh-Zustand selbst. Direkt gegen Port
15443 testen (ohne Istio, reiner TCP-Connect-Test genuegt zur Eingrenzung):
```bash
nc -zv <eastwest-gateway-adresse> 15443
```

**`Error: unknown flag: --networkGateway` oder aehnliches beim Helm-Install in Schritt 3.**
Chart-Version zu alt - `networkGateway` existiert erst seit einer neueren Chart-Generation des
`istio/gateway`-Charts. Gegen den in `readme.md` verwendeten Helm-Repo/Release 1.31 geprueft;
bei abweichender, aelterer Chart-Version notfalls `ISTIO_META_REQUESTED_NETWORK_VIEW`,
`topology.istio.io/network`-Labels und die vier Ports manuell per `--set env[0].name=...` bzw.
eigenem Manifest setzen (Vorlage: das `federation-ingress-gateway`-Manifest in `federation.md`,
Schritt 3).

## Betrieb und Sicherheit

- **API-Server-Exposition ist die groesste zusaetzliche Angriffsflaeche gegenueber
  `federation.md`.** Jeder Remote-Secret-Token erlaubt Lesezugriff (List/Watch) auf Services,
  Endpoints, Pods und Nodes der Gegenseite. Beide API-Server-Endpunkte entsprechend eng fassen
  (idealerweise nur ueber denselben privaten Netzwerkpfad wie das East-West-Gateway erreichbar,
  nicht ueber das offene Internet), Remote-Secret-Tokens wie Cluster-Admin-Zugangsdaten
  behandeln.
- `discoverySelectors` (Schritt 5) ist hier der Governance-Mechanismus - anders als die
  Export-Liste in `federation.md` aber **kein Service-granularer Allowlist-Schritt**, sondern ein
  zentraler, pro Kontrollebene gesetzter Filter. Eine vergessene Namespace-Kennzeichnung fuehrt
  zu stillschweigendem Default-Allow fuer genau diesen Namespace, nicht zu einem sichtbaren
  Fehler. Regelmaessig gegen die tatsaechlich erwuenschten Namespaces pruefen.
- Root-CA-Kompromittierung betrifft beide Sub-CAs gleichzeitig (siehe `federation.md`, Betrieb
  und Sicherheit) - hier zusaetzlich kritischer, weil kompromittierte Workload-Identitaeten bei
  Multi-Primary automatisch mesh-weit als vertrauenswuerdig gelten, nicht nur gegenueber
  explizit exportierten Services.
- Beide Kontrollebenen ueberwachen, nicht nur eine: bei Multi-Primary bleibt zwar (anders als bei
  Primary-Remote) jeder Cluster bei Verbindungsverlust zur Gegenseite fuer sich funktionsfaehig,
  aber Cross-Cluster-Failover faellt beim Ausfall **eines** East-West-Gateways fuer alle
  gemeinsam betriebenen Services gleichzeitig aus, nicht nur fuer einzelne exportierte Dienste
  wie bei Federation.
- Firewall vor dem GKE-`LoadBalancer` (Port 15443, plus API-Server-Port) so eng wie moeglich
  fassen (OpenShift-seitige Quell-Adressen/Tunnel-Subnetz statt `0.0.0.0/0`), analog zur
  Empfehlung in `federation.md`/`istio-multi-cloud.md`.

## Aufraeumen

```bash
oc-openshift -n istio-system delete secret -l istio/multiCluster=true
kubectl-gke -n istio-system delete secret -l istio/multiCluster=true

oc-openshift -n istio-system delete -f - <<'EOF'
apiVersion: networking.istio.io/v1
kind: Gateway
metadata:
  name: cross-network-gateway
EOF
kubectl-gke -n istio-system delete -f - <<'EOF'
apiVersion: networking.istio.io/v1
kind: Gateway
metadata:
  name: cross-network-gateway
EOF

helm-openshift uninstall istio-eastwestgateway -n istio-system
helm-gke uninstall istio-eastwestgateway -n istio-system

oc-openshift -n istio-system delete secret cacerts
kubectl-gke -n istio-system delete secret cacerts
oc-openshift -n istio-system rollout restart deployment/istiod
kubectl-gke -n istio-system rollout restart deployment/istiod

oc-openshift delete namespace sample
kubectl-gke delete namespace sample
```

`meshID`/`network`/`clusterName` aus Schritt 2 bleiben nach diesem Cleanup in der `istiod`-Helm-
Release bestehen (idempotente Werte, stoeren eine spaetere Einzelcluster-Nutzung nicht); zum
vollstaendigen Rueckbau auf den Stand von `readme.md`/`readme-googlecloud.md` zusaetzlich ohne
diese drei Werte erneut `helm upgrade` ausfuehren.

## Weiterfuehrend

- [`istio-multi-cloud.md`](../istio-multi-cloud.md): die Architekturentscheidung
  (Multi-Primary/Primary-Remote/Federation), inkl. der Begruendung, warum Federation fuer reine
  Teil-Service-Freigaben meist die bessere Wahl ist.
- [`federation.md`](federation.md): die Alternative mit Default-Deny-Governance statt
  automatischem Service-Merge, inkl. identischem Trust-Setup (Schritt 1/2 hier).
- [`readme.md`](readme.md) / [`readme-googlecloud.md`](readme-googlecloud.md): die beiden
  Basis-Installationen, die dieses Dokument voraussetzt.
- [Install Multi-Primary on different networks](https://istio.io/latest/docs/setup/install/multicluster/multi-primary_multi-network/):
  offizielle Referenz fuer die hier verwendeten Schritte.
- [Verify the installation](https://istio.io/latest/docs/setup/install/multicluster/verify/):
  Quelle des HelloWorld-Tests in "Testen".
- [Discovery selectors in Istio 1.10](https://tetrate.io/blog/discovery-selectors),
  [Istio-Blog: discovery selectors](https://istio.io/latest/blog/2021/discovery-selectors/):
  Hintergrund zu Schritt 5.
