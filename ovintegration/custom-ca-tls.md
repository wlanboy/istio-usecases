# Eigene Sub-CA fuer das externe TLS-Zertifikat: Terminierung am Envoy

Die Basis-Installation (`readme.md`) terminiert TLS am OpenShift-Router (`22-route.yaml`,
`termination: edge`). Der Router entschluesselt, der Weg Router -> `istio-ingressgateway` ist
danach Klartext-HTTP. Dieses Dokument beschreibt die Alternative: TLS wird bis zu **Envoy**
(dem Istio Ingress Gateway) durchgereicht und dort mit einem von der eigenen Sub-CA
ausgestellten Zertifikat terminiert. Der Router macht dann nur noch **Passthrough**.

## Warum am Envoy statt am Router terminieren

- **Ein Zertifikat, eine Stelle, ein Secret.** Bei Router-Terminierung liegt das
  Zertifikatsmaterial entweder in jeder einzelnen `Route` oder im cluster-weiten
  IngressController-Default (siehe Alternative unten). Bei Envoy-Terminierung liegt es als
  ganz normales Kubernetes-`Secret` in `istio-system`. Rotation ist ein Secret-Update, kein
  Route-/IngressController-Edit, und laesst sich mit Standard-Kubernetes-Tooling (z. B.
  cert-manager) automatisieren.
- **SNI-basiertes Routing direkt im Mesh.** Envoy kann pro Host/Server-Block unterschiedliche
  Zertifikate ausliefern (mehrere `tls`-Server-Bloecke im `Gateway`), ohne dass der Router davon
  etwas mitbekommen muss. Der Router leitet bei `passthrough` nur anhand der SNI weiter, ohne
  zu entschluesseln.
- **mTLS/Client-Zertifikate moeglich.** Nur wenn Envoy selbst terminiert, kann Istio den TLS-
  Handshake pruefen (`tls.mode: MUTUAL`, Client-Zertifikatsvalidierung gegen eine eigene Sub-CA).
  Der Router kann das bei `edge`-Terminierung nicht.
- **Konsistente Sicherheitsgrenze.** Wenn "Envoy ist der TLS-Endpunkt" als Architekturprinzip
  gilt (z. B. weil der Router-Pfad als nicht vertrauenswuerdig genug gilt oder Compliance
  verlangt, dass TLS nur innerhalb des Mesh-verwalteten Zertifikatsstores endet), ist
  Router-Terminierung grundsaetzlich der falsche Ansatz, unabhaengig vom CA-Thema.

## Voraussetzung: RBAC ist bereits vorhanden

Envoy holt das Zertifikat zur Laufzeit per SDS (Secret Discovery Service) direkt aus der
Kubernetes-API. Dafuer braucht die ServiceAccount der Ingress-Gateway-Pods Lesezugriff auf
`Secret`-Objekte in `istio-system`. Das ist **bereits Teil der Basis-Installation**: der
`istio/gateway`-Helm-Chart legt per Default (`rbac.enabled: true`) automatisch eine `Role` +
`RoleBinding` fuer die eigene ServiceAccount an (`get`/`watch`/`list` auf `secrets`, siehe
`gateway/templates/role.yaml`), sobald `install-istio.sh` einmal durchgelaufen ist. Kein
zusaetzlicher `oc adm policy`-Schritt noetig. Anders als bei `anyuid`/`privileged` gibt es hier
keine SCC-Huerde, weil es sich um ganz normale RBAC-Rechte auf ein Kubernetes-Objekt handelt,
nicht um Pod-Security.

## Schritt 1: TLS-Secret mit der Sub-CA-Kette anlegen

```bash
# Reihenfolge in tls.crt: zuerst Leaf, danach Intermediate/Sub-CA(s), kein separates
# caCertificate-Feld wie bei OpenShift-Routes, alles kommt in eine Datei
cat leaf.crt sub-ca.crt > tls-chain.crt

oc create secret tls ovintegration-gateway-cert \
  --cert=tls-chain.crt --key=leaf.key \
  -n istio-system
```

Das Leaf-Zertifikat (`leaf.crt`) muss ein SAN tragen, das zum verwendeten Hostnamen passt,
z. B. `*.apps.<cluster-domain>` fuer eine gemeinsame Wildcard, oder `ovintegration-<namespace>.
apps.<cluster-domain>` fuer einen einzelnen Demo-Namespace.

**Empfehlung fuer diesen Usecase:** ein einziges Secret mit Wildcard-SAN, gemeinsam genutzt von
allen Demo-Namespaces (Begruendung siehe Schritt 4).

## Schritt 2: Gemeinsames `Gateway`-Objekt mit TLS-Server

Das bestehende `manifests/20-gateway.yaml` wird **pro Demo-Namespace** angelegt und hat nur
einen HTTP-Server auf Port 80. Fuer TLS am Envoy legt man **ein zusaetzliches, zentrales**
`Gateway`-Objekt in `istio-system` an (nicht pro Namespace, Begruendung siehe Schritt 4):

```yaml
# manifests/optional-gateway-tls.yaml (manuell anlegen, nicht Teil von install.sh)
apiVersion: networking.istio.io/v1
kind: Gateway
metadata:
  name: ovintegration-gateway-tls
  namespace: istio-system
spec:
  selector:
    istio: ingressgateway
  servers:
    - port:
        number: 443
        name: https
        protocol: HTTPS
      tls:
        mode: SIMPLE
        credentialName: ovintegration-gateway-cert
      hosts:
        - "*"
```

`credentialName` referenziert **immer** ein Secret im selben Namespace wie die
Gateway-Workload selbst (`istio-system`), unabhaengig davon, in welchem Namespace das
`Gateway`-Objekt liegt. Es gibt keine Cross-Namespace-Secret-Referenz per `credentialName`; der
SDS-Server laeuft als Teil des Gateway-Pods und hat (siehe Voraussetzung oben) nur RBAC fuer
sein eigenes Namespace.

## Schritt 3: `VirtualService` an das zentrale Gateway binden

`manifests/21-virtualservice.yaml` referenziert aktuell das lokale, namespace-eigene Gateway
(`ovintegration-gateway`). Fuer das zentrale TLS-Gateway wird die Cross-Namespace-Syntax
`<namespace>/<name>` verwendet:

```yaml
apiVersion: networking.istio.io/v1
kind: VirtualService
metadata:
  name: ovintegration-nginx-tls
  namespace: ${NAMESPACE}
spec:
  hosts:
    - "*"
  gateways:
    - istio-system/ovintegration-gateway-tls
  http:
    - route:
        - destination:
            host: nginx.${NAMESPACE}.svc.cluster.local
            port:
              number: 80
```

Das bestehende `ovintegration-gateway` (Port 80, HTTP) und die zugehoerige
`ovintegration-nginx`-VirtualService koennen parallel bestehen bleiben, falls weiterhin auch
Klartext-Zugriff (z. B. fuer interne Checks) gewuenscht ist.

## Schritt 4: Route auf `passthrough` umstellen

```yaml
apiVersion: route.openshift.io/v1
kind: Route
metadata:
  name: ovintegration-${NAMESPACE}
  namespace: istio-system
spec:
  to:
    kind: Service
    name: istio-ingressgateway
  port:
    targetPort: https
  tls:
    termination: passthrough
    insecureEdgeTerminationPolicy: Redirect
```

Unterschiede zur bisherigen `22-route.yaml`:
- `port.targetPort: https` statt `http2`: der Router muss jetzt den TLS-Port (443) der
  Gateway-Service ansprechen, nicht den Klartext-Port 80 (die Service-Ports `http2`/`https`
  existieren bereits per Default im `gateway`-Chart, siehe `readme.md`-Troubleshooting).
- `termination: passthrough` statt `edge`: der Router entschluesselt nicht mehr, sondern
  routet anhand der TLS-SNI direkt zur Service-IP durch. `certificate`/`key`/`caCertificate`
  entfallen komplett aus der Route. Das Zertifikat lebt jetzt ausschliesslich im Kubernetes-
  Secret aus Schritt 1.
- `insecureEdgeTerminationPolicy: Redirect` funktioniert weiterhin: Port-80-Anfragen werden
  weiterhin vom Router selbst (im Klartext, vor jeder TLS-Entscheidung) auf `https://` umgeleitet.
  Das betrifft nur den initialen Redirect, nicht den eigentlichen TLS-Traffic.

**Warum ein zentrales Gateway/Secret statt einem pro Demo-Namespace?** Passthrough-Routing
durch den Router UND SIMPLE-TLS-Terminierung durch Envoy laufen beide ueber SNI/Host-Matching.
Da jedes `install.sh <namespace>` bisher ein eigenes `Gateway` mit `hosts: ["*"]` anlegt, wuerde
ein zusaetzlicher TLS-Server pro Namespace mehrere `Gateway`-Objekte erzeugen, die alle
`hosts: ["*"]` auf denselben Envoy-Pods und denselben Port 443 beanspruchen. Uneindeutig, wenn
mehrere Demo-Namespaces gleichzeitig installiert sind (die `readme.md` beschreibt genau dieses
Mehr-Namespace-Szenario als unterstuetzt). Ein zentrales `Gateway` mit einem Wildcard-Zertifikat
in `istio-system`, an das alle `VirtualService`-Objekte der Demo-Namespaces gebunden werden,
vermeidet den Konflikt und spiegelt die bereits bestehende Architektur ("Mehrere
Demo-Namespaces teilen sich denselben Gateway/Router") sauber auf HTTPS.

## Testen

```bash
ROUTE_HOST="$(oc -n istio-system get route ovintegration-<namespace> -o jsonpath='{.spec.host}')"

# TLS-Handshake direkt pruefen (zeigt das ausgelieferte Zertifikat inkl. Chain)
openssl s_client -connect "${ROUTE_HOST}:443" -servername "${ROUTE_HOST}" </dev/null 2>/dev/null \
  | openssl x509 -noout -issuer -subject

# End-to-end
curl -v --cacert root-ca.crt "https://${ROUTE_HOST}/"
```

`openssl s_client` zeigt das von Envoy praesentierte Zertifikat (Issuer sollte die eigene
Sub-CA sein). Damit laesst sich unterscheiden, ob wirklich Envoy terminiert (Issuer = eigene
Sub-CA) oder der Request unbemerkt doch am Router landet (Issuer = OpenShift-Default-CA).

## Troubleshooting

**`curl` liefert weiterhin das alte/OpenShift-Default-Zertifikat.** Die Route terminiert noch
als `edge` statt `passthrough`, oder zeigt noch auf `targetPort: http2`. Pruefen mit
`oc -n istio-system get route ovintegration-<namespace> -o jsonpath='{.spec.tls.termination}
{.spec.port.targetPort}{"\n"}'`.

**Handshake schlaegt fehl / Envoy schliesst die Verbindung.** Das Secret fehlt, hat den
falschen Namen/Namespace, oder `tls.crt` enthaelt die Kette in falscher Reihenfolge (Leaf muss
zuerst kommen). Pruefen mit
`oc -n istio-system logs deploy/istio-ingressgateway | grep -i "ovintegration-gateway-cert\|sds"`.

**`404 no healthy upstream` von Envoy trotz korrektem Zertifikat.** Das `VirtualService` bindet
noch an das falsche/lokale Gateway statt `istio-system/ovintegration-gateway-tls`. Pruefen mit
`oc -n <namespace> get virtualservice ovintegration-nginx-tls -o jsonpath='{.spec.gateways}'`.
