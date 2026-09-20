# Gesamten externen Traffic ueber den Istio Ingress Gateway fuehren

Die Basis-Installation (`readme.md`) baut pro Demo-Namespace genau eine `Route` mit
automatisch vergebenem Hostnamen (`ovintegration-<namespace>.apps.<cluster-domain>`). Nur
Traffic fuer **diesen einen Hostnamen** landet beim `istio-ingressgateway`-Service - jede
andere `Route` im Cluster geht an ihrer eigenen `Service`-Definition vorbei am Envoy. Dieses
Dokument beschreibt, was zusaetzlich noetig ist, damit der Istio Ingress Gateway zum
**alleinigen externen Eingang des Clusters** wird (statt nur fuer diese eine Demo-Route).

## Warum das nicht automatisch passiert

Der OpenShift-Router (HAProxy) routet ausschliesslich anhand des `Host`-Headers gegen
existierende `Route`-Objekte. Ohne passende `Route` gibt es fuer eine Anfrage keinen Weg zum
Ziel-Service - unabhaengig davon, was innerhalb des Mesh an `Gateway`/`VirtualService`
konfiguriert ist. Der Istio-`Gateway` in `manifests/20-gateway.yaml` matcht zwar bereits
`hosts: ["*"]` (also jeden Host-Header, den Envoy zu sehen bekommt), aber Envoy bekommt nur
die Anfragen zu sehen, die der Router ihm vorher per `Route` zuweist.

## Schritt 1: Wildcard-Route auf den Ingress-Gateway-Service

Statt (oder zusaetzlich zu) einer `Route` pro Demo-Namespace eine einzige Wildcard-Route in
`istio-system`, die alles unterhalb einer Subdomain an den Gateway-Service schickt:

```yaml
# manifests/optional-wildcard-route.yaml (manuell anlegen, nicht Teil von install.sh)
apiVersion: route.openshift.io/v1
kind: Route
metadata:
  name: istio-ingressgateway-wildcard
  namespace: istio-system
spec:
  host: "*.apps.<cluster-domain>"   # ersetzen: cluster-eigene apps-Domain, siehe `oc get ingresscontroller/default -n openshift-ingress-operator -o jsonpath='{.status.domain}'`
  wildcardPolicy: Subdomain
  to:
    kind: Service
    name: istio-ingressgateway
  port:
    targetPort: http2
  tls:
    termination: edge
    insecureEdgeTerminationPolicy: Redirect
```

**Warum `wildcardPolicy: Subdomain` und nicht mehrere einzelne Routes?** Mit einzelnen Routes
muesste fuer jeden neuen Hostnamen (= jede neue App/jeden neuen Namespace) eine eigene `Route`
angelegt werden - der Router selbst waere weiterhin die Stelle, die entscheidet, was uebehaupt
beim Mesh ankommt. Eine Wildcard-Route macht den Router zu einem reinen TLS-Terminator/Pass-
Through fuer die Subdomain und verlagert die eigentliche Host-basierte Routing-Entscheidung
komplett zu Istio (`VirtualService`-Objekte je App), wo sie mit Mesh-Features (Traffic
Splitting, Retries, mTLS zu Backends, ...) kombinierbar ist.

## Schritt 2: Wildcard-Routes am Cluster erlauben

OpenShift lehnt Wildcard-Routes standardmaessig ab (`ROUTER_ALLOW_WILDCARD_ROUTES=false` am
Default-IngressController). Ohne diesen Schritt bleibt die Route aus Schritt 1 im Status
`RouteNotAdmitted` haengen:

```bash
oc patch ingresscontroller/default -n openshift-ingress-operator --type=merge \
  -p '{"spec":{"routeAdmission":{"wildcardPolicy":"WildcardsAllowed"}}}'
```

Das ist eine **Cluster-weite, Cluster-Admin-Aenderung** (IngressController-Operator-Namespace,
nicht der Demo-Namespace) - sie erlaubt Wildcard-Routes fuer den gesamten Cluster, nicht nur
fuer `istio-system`. Quelle: [Ingress Operator - Konfiguration (Red Hat Docs, routeAdmission)](https://docs.redhat.com/en/documentation/openshift_container_platform/4.10/html/networking/configuring-ingress),
[openshift/enhancements: wildcard-admission-policy](https://github.com/openshift/enhancements/blob/master/enhancements/ingress/wildcard-admission-policy.md).

## Schritt 3: Konflikte mit bestehenden Routes anderer Teams

`routeAdmission.namespaceOwnership` steht per Default auf `Strict`: nur ein Namespace darf
einen gegebenen Hostnamen/eine Subdomain fuer sich beanspruchen. Fuer die Wildcard-Route aus
Schritt 1 ist das gewollt - kein anderer Namespace kann euch nachtraeglich die Subdomain
streitig machen. Zu beachten:

- Bereits bestehende, **spezifischere** Routes anderer Apps unter derselben
  `*.apps.<cluster-domain>` bleiben weiterhin vorrangig (der Router matcht die spezifischste
  Route zuerst) und laufen so lange am Mesh vorbei, bis sie entfernt oder durch Istio-
  `VirtualService`-Objekte ersetzt werden. "Alles" bedeutet hier also praktisch: alles, wofuer
  es *keine* speziellere, konkurrierende Route mehr gibt.
- Neue Apps sollten ab diesem Zeitpunkt **keine eigene `Route`** mehr anlegen, sondern nur noch
  `Gateway`/`VirtualService`-Objekte - sonst entsteht wieder ein direkter, am Mesh
  vorbeifuehrender Pfad.

## Schritt 4: NetworkPolicy-Falle bei restriktivem Default-Deny

Der Router laeuft im Namespace `openshift-ingress`, ausserhalb des Mesh, und spricht den
`istio-ingressgateway`-Service ganz normal ueber das Pod-Netzwerk (OVN-Kubernetes) an. Auf
einem frischen Cluster ohne eigene `NetworkPolicy`-Objekte ist das automatisch erlaubt. Gilt im
Cluster jedoch - wie im Architektur-Abschnitt der `readme.md` bereits fuer istiod/Sidecars
vermerkt - ein restriktives Default-Deny, muss zusaetzlich Ingress von `openshift-ingress` auf
die Gateway-Ports (`8080`/`8443`, siehe `oc -n istio-system get svc istio-ingressgateway -o
yaml`) explizit erlaubt werden, sonst liefert der Router bei Anfragen an die Wildcard-Route
weiterhin `503`, obwohl Gateway-Pod und Route selbst gesund sind.

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-openshift-router
  namespace: istio-system
spec:
  podSelector:
    matchLabels:
      istio: ingressgateway
  policyTypes:
    - Ingress
  ingress:
    - from:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: openshift-ingress
```

## Schritt 5: DNS und Erreichbarkeit

Fuer `*.apps.<cluster-domain>` existiert die Wildcard-DNS-Zone in aller Regel bereits (jede
andere Route im Cluster nutzt sie schon), ebenso die externe Erreichbarkeit des Routers
(Firewall/LoadBalancer). Hier ist nichts usecase-Spezifisches zu tun - **ausser** ihr wollt eine
eigene, zusaetzliche Domain statt `apps.<cluster-domain>` verwenden. Dann zusaetzlich noetig:

- Eigener DNS-Wildcard-Eintrag (`*.euer-domain.tld` -> externe IP/LB des Routers).
- `spec.host` in Schritt 1 auf diese Domain anpassen.
- Zertifikat mit passendem SAN, siehe [`custom-ca-tls.md`](custom-ca-tls.md).

## Zusammenfassung: was ist neu, was bleibt

| Bestandteil | Vorher (readme.md) | Nachher (dieses Dokument) |
|---|---|---|
| Route | 1x pro Demo-Namespace, konkreter Host | 1x Wildcard-Route in `istio-system` |
| IngressController | unveraendert | `routeAdmission.wildcardPolicy: WildcardsAllowed` |
| Neue Apps binden sich an | eigene `Route` | `Gateway`/`VirtualService` (kein `Route` mehr noetig) |
| Istio `Gateway`/`VirtualService` | unveraendert (`hosts: ["*"]`) | unveraendert |
| NetworkPolicy | nur relevant bei Default-Deny | zusaetzlich: Ingress von `openshift-ingress` erlauben |
