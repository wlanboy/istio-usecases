# Mesh Federation zwischen OpenShift und Google Cloud (GKE)

Dieses Dokument baut auf zwei bereits laufenden, unabhaengigen Istio-Installationen auf:
[`readme.md`](readme.md) (Open-Source-Istio auf OpenShift/OVN-Kubernetes) und
[`readme-googlecloud.md`](readme-googlecloud.md) (dieselbe Istio-Version auf GKE Standard). Es
verbindet beide zu **zwei eigenstaendigen Meshes mit Mesh Federation**: jede Seite behaelt ihre
eigene Kontrollebene (`istiod`), ihre eigene Sub-CA und ihre eigene administrative Hoheit. Nur
explizit freigegebene Services werden fuer die Gegenseite ueberhaupt sichtbar.

Die Architekturentscheidung *warum* hier Mesh Federation (statt Multi-Primary oder
Primary-Remote) die richtige Wahl ist, wurde bereits in [`istio-multi-cloud.md`](../istio-multi-cloud.md)
ausfuehrlich hergeleitet (Governance ueber explizite Export-/Import-Regeln statt automatischem,
default-allow Service-Merge; kein API-Server-Zugriff der Gegenseite noetig). Dieses Dokument
wiederholt diese Herleitung nicht, sondern macht sie fuer die konkrete Kombination
OpenShift + GKE mit **eigenen Sub-CAs** und den tatsaechlichen Kommandos umsetzbar.

## Reifegrad-Hinweis

Der hier verwendete Federation-Controller [`openshift-service-mesh/federation`](https://github.com/openshift-service-mesh/federation)
ist ein junges Community-Projekt (Teil des OpenShift-Service-Mesh-3/Sail-Umfelds), **kein**
GA-Produkt: kein GitHub-Release/Tag vorhanden, Stand dieser Pruefung (September 2026) letzter
Commit auf `master` am 2025-03-20 (`255ad0c`), Controller-Image
`quay.io/maistra-dev/federation-controller:latest` mit demselben Build-Datum. Fuer diesen
Demo-Usecase unproblematisch, vor jedem produktiven Einsatz aber explizit pruefen, ob das
Projekt zwischenzeitlich weitergefuehrt/ersetzt wurde. Empfehlung: Chart nicht von `master`,
sondern von einem gepinnten Commit-SHA klonen (siehe Installation).

## Trust-Modell: gemeinsame Root-CA, zwei eigene Sub-CAs

Der Controller bringt **keinen** Mechanismus mit, um Trust-Bundles zwischen zwei unabhaengig
verwurzelten CAs auszutauschen. Wörtlich aus dem Projekt-README:

> "This controller does not provide any mechanism to share trust bundles between meshes using
> different CAs. It can only enable mTLS communication between meshes when all clusters use the
> same root CA or use SPIRE with enabled trust bundle federation."

"Eigene Sub-CAs" (Plural, wie gewuenscht) und trotzdem funktionierendes cross-Mesh-mTLS ueber
diesen Controller schliessen sich also nicht aus, muessen aber richtig zusammengesetzt werden:
**ein gemeinsamer Root, zwei eigene Intermediates** - Option 1 aus der Trust-Tabelle in
`istio-multi-cloud.md`. Jede Seite bekommt eine eigene Sub-CA mit eigenem Signing-Key (eigene
Rotation, eigene Kompromittierungs-Blast-Radius), beide haengen aber am selben Root-Zertifikat.
Das ist keine Ausweich-Loesung, sondern das vom Projekt selbst vorgesehene Modell (siehe
`examples/README.md` im Federation-Repo, Abschnitt "Common root and trust domain").

**Wirklich getrennte Roots** (kein gemeinsamer Vertrauensanker) sind mit diesem Controller nur
ueber SPIRE + Trust-Domain-Federation moeglich (`examples/spire/` im Repo) - deutlich mehr
Betriebsaufwand (eigener SPIRE-Server, Workload-Attestation) und nicht Teil dieses Dokuments.

## Voraussetzungen

- OpenShift-Seite: `readme.md` vollstaendig durchgelaufen (`istiod` laeuft mit
  `pilot.cni.enabled=true`), Cluster-Admin-Rechte.
- GKE-Seite: `readme-googlecloud.md` vollstaendig durchgelaufen (GKE **Standard**, nicht
  Autopilot, siehe dortige Begruendung), Cluster-Admin-Rechte.
- `openssl`, `helm` >= 3.x, `git`, `kubectl`/`oc` mit Kontext auf beide Cluster gleichzeitig
  verfuegbar (in den Beispielen unten als `oc-openshift`/`kubectl-gke`/`helm-openshift`/`helm-gke`
  bezeichnet, analog zu den `keast`/`kwest`-Aliases im Federation-Repo).
- Beide Federation-Gateways muessen von der jeweils anderen Seite aus erreichbar sein: die
  OpenShift-Route (automatisch unter der Apps-Wildcard-Domain, siehe Schritt 3) und die externe
  IP des GKE-`LoadBalancer`-Service. Sitzt die OpenShift-Seite hinter keinem oeffentlich
  erreichbaren Router (z. B. On-Prem ohne Cloud-LB), gilt dieselbe Netzwerk-Ueberlegung
  (VPN/WireGuard-Tunnel) wie in `istio-multi-cloud.md`, Abschnitt "Das eigentliche Problem:
  Netzwerk-Konnektivitaet". Sind beide Cluster ohnehin oeffentlich erreichbar (typischer Fall bei
  Cloud-gehostetem OpenShift), reicht eine eng gefasste Firewall-Regel plus mTLS.

## Architektur

```
OpenShift-Mesh (network: openshift-network)          GKE-Mesh (network: gke-network)
istio-system                                          istio-system
+-----------------------------------+                +-----------------------------------+
| istiod (eigene Sub-CA: Root+SubCA-A)|                | istiod (eigene Sub-CA: Root+SubCA-B)|
|                                     |                |                                     |
| federation-controller (Sidecar)    |<--- gRPC ------>| federation-controller (Sidecar)    |
|   - lokal: exportedServiceSet      |  FederatedService|   - lokal: exportedServiceSet      |
|   - remote: gke (ingressType=istio)|   Discovery      |   - remote: openshift              |
|                                     |                |     (ingressType=openshift-router)  |
| federation-ingress-gateway         |<=== TLS ========>| federation-ingress-gateway          |
|   Service ClusterIP, Port 15443    |  AUTO_PASSTHROUGH |   Service LoadBalancer, Port 15443  |
|   + vom Controller erzeugte        |  (Ende-zu-Ende    |   (direkt oeffentlich erreichbar,   |
|     OpenShift-Route (passthrough)  |   mTLS, eigene    |    kein Router davor)               |
|                                     |   Sub-CA je Seite)|                                     |
| nginx (Namespace ovintegration-demo)|                | nginx (Namespace ovintegration-demo)|
|   Service-Label export-service=true |----exportiert-->|   automatisch als ServiceEntry      |
|                                     |                |   importiert, sobald Peer+Export    |
+-----------------------------------+                +-----------------------------------+
```

Zentraler Unterschied zu Multi-Primary/Primary-Remote (siehe `istio-multi-cloud.md`): kein
Cluster braucht Lesezugriff auf den Kubernetes-API-Server der Gegenseite. Die
Federation-Controller sprechen ausschliesslich per gRPC ueber das Federation-Gateway (Port 15443,
selbes Gateway wie der eigentliche Datenverkehr, per SNI unterschieden).

## Installation

### Schritt 1: Root-CA und zwei eigene Sub-CAs erzeugen

```bash
mkdir -p ~/ovintegration-federation-ca && cd ~/ovintegration-federation-ca
wget https://raw.githubusercontent.com/istio/istio/release-1.31/tools/certs/common.mk
wget https://raw.githubusercontent.com/istio/istio/release-1.31/tools/certs/Makefile.selfsigned.mk

# Einmalig: gemeinsamer Root, den NUR wir kontrollieren (nicht das Root einer bestehenden PKI)
make -f Makefile.selfsigned.mk \
  ROOTCA_CN="ovintegration Root CA" ROOTCA_ORG=ovintegration.example \
  root-ca

# Eigene Sub-CA fuer OpenShift (eigener Signing-Key, eigenes Zertifikat, gemeinsamer Root)
make -f Makefile.selfsigned.mk \
  INTERMEDIATE_CN="ovintegration OpenShift Sub-CA" INTERMEDIATE_ORG=ovintegration.example \
  openshift-cacerts

# Eigene Sub-CA fuer GKE (eigener Signing-Key, eigenes Zertifikat, gemeinsamer Root)
make -f Makefile.selfsigned.mk \
  INTERMEDIATE_CN="ovintegration GKE Sub-CA" INTERMEDIATE_ORG=ovintegration.example \
  gke-cacerts

make -f common.mk clean
```

Ergebnis: zwei Verzeichnisse `openshift/` und `gke/`, jeweils mit `ca-key.pem` (eigener,
nicht geteilter Signing-Key), `ca-cert.pem` (eigenes Sub-CA-Zertifikat), `root-cert.pem`
(identisch in beiden, der gemeinsame Anker) und `cert-chain.pem` (`ca-cert.pem` + `root-cert.pem`,
in dieser Reihenfolge - Istios eigene Konvention, siehe `Makefile.selfsigned.mk`).

**`ca-key.pem` ist das eigentlich Schuetzenswerte hier.** Beide Dateien getrennt aufbewahren
(z. B. Vault/Secret-Manager je Umgebung), niemals beide Sub-CA-Keys am selben Ort wie den
Root-Key ablegen, sobald dieses Demo-Setup produktiv naeher betrachtet wird.

### Schritt 2: Sub-CA in beide bereits laufenden `istiod`-Installationen einspielen

`readme.md`/`readme-googlecloud.md` haben `istiod` bisher **ohne** eigene CA installiert (Istio
generiert dabei automatisch eine selbstsignierte, pro Installation eigene Root - genau das, was
Mesh Federation ohne SPIRE nicht ueberbruecken kann). Das muss jetzt nachgeholt werden:

```bash
# OpenShift
oc-openshift create secret generic cacerts -n istio-system \
  --from-file=root-cert.pem=openshift/root-cert.pem \
  --from-file=ca-cert.pem=openshift/ca-cert.pem \
  --from-file=ca-key.pem=openshift/ca-key.pem \
  --from-file=cert-chain.pem=openshift/cert-chain.pem
oc-openshift -n istio-system rollout restart deployment/istiod
oc-openshift -n istio-system rollout status deployment/istiod --timeout=120s

# GKE
kubectl-gke create secret generic cacerts -n istio-system \
  --from-file=root-cert.pem=gke/root-cert.pem \
  --from-file=ca-cert.pem=gke/ca-cert.pem \
  --from-file=ca-key.pem=gke/ca-key.pem \
  --from-file=cert-chain.pem=gke/cert-chain.pem
kubectl-gke -n istio-system rollout restart deployment/istiod
kubectl-gke -n istio-system rollout status deployment/istiod --timeout=120s
```

`istiod` erkennt das Secret `cacerts` (fester Name, feste Namespace-Erwartung `istio-system`)
automatisch beim Start und verwendet es statt der selbstsignierten Default-CA
([Plugin CA Certificates](https://istio.io/latest/docs/tasks/security/cert-management/plugin-ca-cert/)).
Bereits laufende Sidecars holen sich neue Zertifikate von ihrer eigenen `istiod` ueber SDS
innerhalb der normalen Zertifikats-TTL automatisch nach; fuer den Demo-Usecase reicht ein
`kubectl rollout restart deployment -n ovintegration-demo nginx`, um das sofort zu erzwingen.

Zusaetzlich braucht jede Seite einen eigenen Netzwerk-Namen (fuer die spaeter vom Controller
erzeugten `ServiceEntry`/`DestinationRule`-Objekte, verhindert dass importierte und lokale
Instanzen desselben Servicenamens verwechselt werden). Das ist ein `helm upgrade` auf die
bestehende, bereits laufende `istiod`-Installation, idempotent:

```bash
# OpenShift
helm-openshift upgrade --install istiod istio/istiod -n istio-system \
  --set platform=openshift --set pilot.cni.enabled=true \
  --set global.network=openshift-network --wait

# GKE
helm-gke upgrade --install istiod istio/istiod -n istio-system \
  --set platform=gke --set pilot.cni.enabled=true \
  --set global.network=gke-network --wait
```

### Schritt 3: Federation-Ingress-Gateway ausrollen

Ein dediziertes, zusaetzliches Gateway pro Cluster, nur fuer Federation-Verkehr (Port 15443,
TLS Auto-Passthrough) - unabhaengig vom bestehenden `istio-ingressgateway` aus `readme.md`/
`readme-googlecloud.md`. Manifest-Vorlage (auf beiden Clustern strukturell gleich, nur
`type`/`network` unterscheiden sich):

```yaml
# manifests-federation/openshift/federation-ingress-gateway.yaml (manuell anlegen)
apiVersion: v1
kind: Service
metadata:
  name: federation-ingress-gateway
  namespace: istio-system
  labels:
    app: federation-ingress-gateway
    topology.istio.io/network: openshift-network
spec:
  type: ClusterIP   # OpenShift: die Route (Schritt 4, vom Controller erzeugt) macht das extern erreichbar
  selector:
    app: federation-ingress-gateway
  ports:
    - port: 15443
      name: tls-passthrough
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: federation-ingress-gateway
  namespace: istio-system
spec:
  selector:
    matchLabels:
      app: federation-ingress-gateway
  template:
    metadata:
      annotations:
        inject.istio.io/templates: gateway
      labels:
        app: federation-ingress-gateway
        sidecar.istio.io/inject: "true"
    spec:
      containers:
        - name: istio-proxy
          image: auto
          env:
            - name: ISTIO_META_REQUESTED_NETWORK_VIEW
              value: openshift-network
```

Fuer GKE identisch, aber `type: LoadBalancer` (kein Router davor, muss direkt erreichbar sein,
siehe `local.ingressType: istio`-Semantik unten) und `topology.istio.io/network`/
`ISTIO_META_REQUESTED_NETWORK_VIEW` auf `gke-network`:

```yaml
# manifests-federation/gke/federation-ingress-gateway.yaml (manuell anlegen), Service-Teil:
apiVersion: v1
kind: Service
metadata:
  name: federation-ingress-gateway
  namespace: istio-system
  labels:
    app: federation-ingress-gateway
    topology.istio.io/network: gke-network
spec:
  type: LoadBalancer
  selector:
    app: federation-ingress-gateway
  ports:
    - port: 15443
      name: tls-passthrough
# Deployment-Teil identisch zur OpenShift-Variante, nur ISTIO_META_REQUESTED_NETWORK_VIEW: gke-network
```

```bash
oc-openshift apply -f manifests-federation/openshift/federation-ingress-gateway.yaml
kubectl-gke apply -f manifests-federation/gke/federation-ingress-gateway.yaml
```

Auf OpenShift wird hier **bewusst noch keine Route angelegt** - das uebernimmt der
Federation-Controller selbst (naechster Schritt), sofern `ingressType: openshift-router` gesetzt
ist. Das ist ein Unterschied zum manuellen Vorgehen in `custom-ca-tls.md`.

### Schritt 4: Federation-Controller installieren

Das Chart wird (mangels Release, siehe Reifegrad-Hinweis) direkt aus einem gepinnten Commit
installiert:

```bash
git clone https://github.com/openshift-service-mesh/federation.git
cd federation
git checkout 255ad0cb5f0617fcb1ddb8c1e9ba930b3bc727e1   # Stand September 2026, siehe Reifegrad-Hinweis
```

Werte-Datei fuer OpenShift (`values-openshift.yaml`):

```yaml
federation:
  meshPeers:
    local:
      name: openshift
      gateways:
        ingress:
          selector:
            app: federation-ingress-gateway
      ingressType: openshift-router   # Controller legt selbst eine passthrough-Route an
    remotes:
      - name: gke
        ingressType: istio            # GKE-Gateway ist direkt per LoadBalancer erreichbar
        network: gke-network
        port: 15443
        # addresses wird unten per --set gesetzt (GKE-LB-IP steht sofort zur Verfuegung)
  exportedServiceSet:
    rules:
      - type: LabelSelector
        labelSelectors:
          - matchLabels:
              export-service: "true"
```

Werte-Datei fuer GKE (`values-gke.yaml`):

```yaml
federation:
  meshPeers:
    local:
      name: gke
      gateways:
        ingress:
          selector:
            app: federation-ingress-gateway
      ingressType: istio               # eigenes Gateway ist selbst direkt erreichbar (LoadBalancer)
    remotes:
      - name: openshift
        ingressType: openshift-router  # Peer haengt hinter dem OpenShift-Router, SNI entsprechend anpassen
        network: openshift-network
        port: 443                      # Route terminiert auf 443, nicht auf den internen Gateway-Port 15443
        # addresses wird unten per --set gesetzt (OpenShift-Route existiert erst nach der Installation unten)
  exportedServiceSet:
    rules:
      - type: LabelSelector
        labelSelectors:
          - matchLabels:
              export-service: "true"
```

**Reihenfolge wegen der Henne-Ei-Situation:** die GKE-`LoadBalancer`-IP steht sofort zur
Verfuegung (kein Controller noetig), die OpenShift-Route wird dagegen erst *vom* OpenShift-seitigen
Controller beim Start angelegt. Deshalb zuerst OpenShift installieren (mit der schon bekannten
GKE-IP), danach die dabei entstandene Route auslesen und erst dann GKE installieren:

```bash
GKE_LB_IP=$(kubectl-gke get svc federation-ingress-gateway -n istio-system \
  -o jsonpath='{.status.loadBalancer.ingress[0].ip}')

helm-openshift install openshift-federation ./chart -n istio-system \
  --values values-openshift.yaml \
  --set "federation.meshPeers.remotes[0].addresses[0]=${GKE_LB_IP}"

oc-openshift -n istio-system rollout status deployment/openshift-federation-controller --timeout=90s

OPENSHIFT_FEDERATION_HOST=$(oc-openshift -n istio-system get route \
  -l app=federation-ingress-gateway -o jsonpath='{.items[0].spec.host}')

helm-gke install gke-federation ./chart -n istio-system \
  --values values-gke.yaml \
  --set "federation.meshPeers.remotes[0].addresses[0]=${OPENSHIFT_FEDERATION_HOST}"
```

Ueberpruefen, dass beide Controller die Peer-Beziehung als verbunden melden:

```bash
oc-openshift -n istio-system logs deploy/openshift-federation-controller | grep -i "connect\|peer"
kubectl-gke -n istio-system logs deploy/gke-federation-controller | grep -i "connect\|peer"
```

### Schritt 5: Demo-Service exportieren

Der bestehende `nginx`-Demo-Service aus `readme.md`/`readme-googlecloud.md` wird per Label
exportiert - genau die Regel aus `exportedServiceSet` oben:

```bash
oc-openshift -n ovintegration-demo label service nginx export-service=true
```

Der GKE-seitige Controller erhaelt die Aenderung automatisch per gRPC-Subscription und legt
selbststaendig einen passenden `ServiceEntry` in `ovintegration-demo` an - **es gibt bei diesem
Controller keinen zusaetzlichen, separaten Import-Schritt** auf der Konsumentenseite. Das ist ein
Unterschied zum generischen Schema in `istio-multi-cloud.md` (dort als eigener Schritt 8
"Import-Regeln auf der Konsumentenseite" beschrieben): dieser konkrete Controller kennt nur eine
Export-Allowlist, keine zusaetzliche Import-Allowlist. Governance findet vollstaendig auf der
exportierenden Seite statt.

```bash
kubectl-gke -n ovintegration-demo get serviceentry -o wide
```

Genauso in die andere Richtung, falls auch GKE-Dienste nach OpenShift exportiert werden sollen:

```bash
kubectl-gke -n ovintegration-demo label service nginx export-service=true
```

### Schritt 6: mTLS und Autorisierung

Der Controller richtet **nur fuer sich selbst** `PeerAuthentication`/`AuthorizationPolicy` ein
(damit ausschliesslich der jeweils konfigurierte Peer-Controller mit ihm sprechen darf). Fuer den
eigentlichen Anwendungsverkehr ist das bewusst **nicht** automatisiert:

> "Controllers DO NOT enforce any authorization policy at the mesh boundaries [...] it is highly
> recommended to deny all traffic by default and allow only selected services."

```bash
# Mesh-weit STRICT mTLS, beide Cluster
cat <<'EOF' | oc-openshift apply -f -
apiVersion: security.istio.io/v1beta1
kind: PeerAuthentication
metadata:
  name: default
  namespace: istio-system
spec:
  mtls:
    mode: STRICT
EOF

cat <<'EOF' | kubectl-gke apply -f -
apiVersion: security.istio.io/v1beta1
kind: PeerAuthentication
metadata:
  name: default
  namespace: istio-system
spec:
  mtls:
    mode: STRICT
EOF
```

Zusaetzlich, analog zur Empfehlung in `istio-multi-cloud.md` ("Betrieb und Sicherheit"): eine
`AuthorizationPolicy` im Zielnamespace, die eingehenden Traffic zum exportierten Service auf die
erwartete Peer-Identitaet einschraenkt, statt sich allein auf die Sichtbarkeit der Export-Regel zu
verlassen.

## Testen

```bash
# Von GKE aus den importierten OpenShift-Service ansprechen (ServiceEntry macht ihn lokal
# unter demselben Hostnamen wie in readme.md ansprechbar)
kubectl-gke -n ovintegration-demo run curl-test --rm -it --image=curlimages/curl --restart=Never -- \
  curl -sv http://nginx.ovintegration-demo.svc.cluster.local/

# Access-Log am OpenShift-seitigen Federation-Gateway pruefen: Anfrage sollte ueber Port 15443
# ankommen (kein Klartext), UPSTREAM_HOST zeigt auf den lokalen nginx-Pod
oc-openshift -n istio-system logs deploy/federation-ingress-gateway --tail=10
```

Erwartung: `curl` liefert die bekannte nginx-Antwort, das Federation-Gateway-Log zeigt eine
TLS-Verbindung mit SNI, die auf `nginx.ovintegration-demo` verweist, `istioctl proxy-config
endpoint` im GKE-Cluster listet den importierten Endpoint mit Netzwerk-Label
`openshift-network`.

## Troubleshooting

**Peer bleibt "not connected", Controller-Logs zeigen TLS-Handshake-Fehler auf Port 15443.**
Meist unterschiedliche Sub-CAs ohne gemeinsamen Root (Schritt 1 uebersprungen/falsch), oder
`cacerts`-Secret wurde erst nach dem letzten `istiod`-Start angelegt (Rollout-Restart in Schritt 2
vergessen). Pruefen mit `openssl verify -CAfile root-cert.pem openshift/ca-cert.pem` und
`openssl verify -CAfile root-cert.pem gke/ca-cert.pem` - beide muessen `OK` liefern.

**OpenShift-Route fuer `federation-ingress-gateway` existiert nicht.** `ingressType` in
`values-openshift.yaml` steht nicht auf `openshift-router`, oder der Controller-Pod ist noch nicht
bereit. Pruefen mit `oc-openshift -n istio-system get route -l app=federation-ingress-gateway`
und `oc-openshift -n istio-system logs deploy/openshift-federation-controller`.

**Export funktioniert von OpenShift nach GKE, aber nicht umgekehrt.** `remotes[].ingressType` auf
GKE-Seite kontrolliert, wie ausgehende Verbindungen *zum* OpenShift-Peer behandelt werden
(SNI-Anpassung fuer den Router), und ist unabhaengig vom eigenen `local.ingressType`. Beide
Richtungen einzeln in der jeweiligen `values-*.yaml` pruefen - eine korrekte lokale Konfiguration
sagt nichts ueber die Konfiguration der `remotes`-Sektion aus.

**`ServiceEntry` wird nicht angelegt, obwohl das Service-Label gesetzt ist.**
`exportedServiceSet.labelSelectors` matcht das Label nicht (Tippfehler,
`export-service: "true"` als String, nicht als Boolean), oder der Peer ist laut Schritt 4 noch
nicht verbunden. Reihenfolge: erst Peer-Verbindung, dann Export-Label, nicht umgekehrt.

**`curl` haengt/Timeout trotz sichtbarem `ServiceEntry`.** `AuthorizationPolicy` im Zielnamespace
blockt (siehe Schritt 6 - der Controller autorisiert nur sich selbst, nicht die Anwendung), oder
die Firewall vor dem GKE-`LoadBalancer` laesst den OpenShift-seitigen Quell-Bereich nicht zu
(siehe Voraussetzungen).

## Betrieb und Sicherheit

- Root-CA-Kompromittierung betrifft **beide** Sub-CAs gleichzeitig, trotz getrennter Keys - das
  ist der Preis fuer "gemeinsamer Root" gegenueber echtem SPIRE-Modell. Root-Key entsprechend
  isoliert aufbewahren (nicht auf demselben Rechner/Secret-Store wie die Sub-CA-Keys).
  Rotation des Roots betrifft koordiniert beide Cluster, Rotation einer Sub-CA nur die eigene
  Seite.
- `image.tag: latest` im Chart mangels Releases (siehe Reifegrad-Hinweis) - fuer reproduzierbare
  Deployments den zum geklonten Commit passenden Image-Digest fixieren
  (`quay.io/maistra-dev/federation-controller@sha256:...`, siehe Quay-Tag-API).
- Die Export-Liste (Schritt 5) ist die eigentliche Sicherheitsgrenze dieses Setups, analog zur
  Empfehlung in `istio-multi-cloud.md`: review-pflichtig behandeln, regelmaessig gegen tatsaechlich
  noch benoetigte Eintraege pruefen.
- Firewall vor dem GKE-`LoadBalancer` so eng wie moeglich fassen (OpenShift-Router-Quell-IPs statt
  `0.0.0.0/0`), auch wenn mTLS + Peer-Authentifizierung das Gateway bereits zusaetzlich absichern.

## Aufraeumen

```bash
helm-openshift uninstall openshift-federation -n istio-system
helm-gke uninstall gke-federation -n istio-system

oc-openshift -n istio-system delete -f manifests-federation/openshift/federation-ingress-gateway.yaml
kubectl-gke -n istio-system delete -f manifests-federation/gke/federation-ingress-gateway.yaml

oc-openshift -n ovintegration-demo label service nginx export-service-
kubectl-gke -n ovintegration-demo label service nginx export-service-

oc-openshift -n istio-system delete secret cacerts
kubectl-gke -n istio-system delete secret cacerts
```

Das Entfernen von `cacerts` allein rotiert `istiod` nicht automatisch zurueck auf eine
selbstsignierte CA; dafuer zusaetzlich `rollout restart deployment/istiod` auf beiden Seiten.

## Weiterfuehrend

- [`istio-multi-cloud.md`](../istio-multi-cloud.md): die Architekturentscheidung
  (Multi-Primary/Primary-Remote/Federation) und das generische Netzwerk-/Trust-Modell, auf dem
  dieses Dokument aufbaut.
- [`readme.md`](readme.md) / [`readme-googlecloud.md`](readme-googlecloud.md): die beiden
  Basis-Installationen, die dieses Dokument voraussetzt.
- [`custom-ca-tls.md`](custom-ca-tls.md): dieselbe "eigene Sub-CA"-Idee, dort fuer die externe
  Ingress-TLS-Terminierung statt fuer die Mesh-interne Workload-CA - unabhaengige, parallel
  nutzbare Anwendung derselben PKI.
- [openshift-service-mesh/federation](https://github.com/openshift-service-mesh/federation):
  Projekt-Quelle dieses Dokuments, inkl. `examples/openshift/` (Grundlage der Manifeste oben) und
  `examples/spire/` (Alternative bei wirklich getrennten Root-CAs).
- [Plugin CA Certificates](https://istio.io/latest/docs/tasks/security/cert-management/plugin-ca-cert/):
  offizielle Doku zum `cacerts`-Secret-Mechanismus aus Schritt 2.
