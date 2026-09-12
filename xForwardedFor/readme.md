# X-Forwarded-For (XFF) Usecase

Demonstriert, wie ein Istio Ingress-Gateway den `X-Forwarded-For`-Header
auswertet, und wie man ihm über `numTrustedProxies` beibringt, wie vielen
vorgeschalteten Proxy-Hops (z.B. Cloud-Loadbalancer, CDN) es dabei vertrauen
soll. Dafür wird ein **eigenes, per Gateway-Injection erzeugtes** Ingress-Gateway
in einem eigenen Namespace deployt (nicht der geteilte `istio-ingressgateway`
in `istio-system`), dahinter ein `echo`-Backend (`nginx`), das die für XFF
relevanten Header in seiner Antwort sichtbar macht.

**Usecase:** Ein Client kann den `X-Forwarded-For`-Header frei gefälscht mitschicken.
Ob und wie weit Envoy diesem Header vertraut, um die "echte" Client-Adresse zu
ermitteln (z.B. für IP-basierte `AuthorizationPolicy`s, Geo-Routing oder Logging),
hängt von `numTrustedProxies` ab. Per Default vertraut Envoy **niemandem** und
verwendet nur die direkte TCP-Peer-Adresse — das ist sicher, ignoriert aber jede
echte Proxy-Kette vor dem Gateway. Steht dagegen ein echter Loadbalancer/CDN vor
dem Gateway, muss `numTrustedProxies` exakt auf die Anzahl dieser Hops gesetzt
werden, sonst wird entweder die gefälschte Client-IP durchgereicht (zu hoch
vertraut) oder die echte Client-IP verworfen (zu niedrig vertraut/nicht gesetzt).

## Voraussetzungen

- laufender Kubernetes-Cluster mit installiertem Istio
- `kubectl` mit gültigem Kontext auf diesen Cluster
- Istio-Version mit Unterstützung für [Gateway Injection](https://istio.io/latest/docs/setup/additional-setup/gateway/#deploying-a-gateway) und die
  `proxy.istio.io/config`-Pod-Annotation (getestet mit Istio 1.30)

## Wie XFF funktioniert (Kurzfassung)

- Envoy hängt am Gateway die von ihm direkt gesehene Peer-Adresse an das
  eingehende `X-Forwarded-For` an und setzt zusätzlich den Header
  `x-envoy-external-address` auf die Adresse, die es (abhängig von
  `numTrustedProxies`) als die **vertrauenswürdige** Client-Adresse ermittelt hat.
- `x-envoy-external-address` ist der Header, dem nachgelagerte Services trauen
  sollten (z.B. für IP-basierte Policies) — **nicht** direkt `X-Forwarded-For`,
  da dieser vom Client beliebig vorbelegt werden kann.
- `numTrustedProxies` (Feld `gatewayTopology.numTrustedProxies`, gesetzt über
  die Pod-Annotation `proxy.istio.io/config`) sagt Envoy, wie viele Einträge am
  rechten Ende von `X-Forwarded-For` bereits von vertrauenswürdigen Proxies
  stammen (die Adresse, die Envoy selbst gerade anhängt, zählt dabei mit).
- **Wichtig (empirisch mit diesem Usecase verifiziert):** Der Wert muss exakt
  zur tatsächlichen Anzahl vorgeschalteter Proxies passen. Sowohl ein zu
  niedriger als auch ein zu hoher Wert führen dazu, dass Envoy auf die direkte
  Peer-Adresse zurückfällt, statt der gewünschten Client-IP aus dem
  `X-Forwarded-For`-Header — ein zu hoher Wert schlägt also **nicht** einfach
  fehl, sondern verhält sich still wie `numTrustedProxies: 0`.

Ausführlich beschrieben in der Istio-Doku:
[Configure Gateway Network Topology](https://istio.io/latest/docs/ops/configuration/traffic-management/network-topology/).

## Installation

```bash
./install.sh                     # Standard-Namespace: xff-demo
./install.sh mein-namespace      # eigenen Namespace verwenden
```

Existiert der angegebene Namespace bereits, wird `00-namespace.yaml` übersprungen
(kein erneutes Anlegen/Überschreiben) — alle übrigen Manifeste werden in diesen
bestehenden Namespace appliziert.

Führt intern aus (Platzhalter `${NAMESPACE}` per `sed` ersetzt):

```bash
kubectl get namespace <namespace>                                              # Existenzprüfung
sed "s|\${NAMESPACE}|<namespace>|g" manifests/00-namespace.yaml               | kubectl apply -f -   # nur falls Namespace neu
sed "s|\${NAMESPACE}|<namespace>|g" manifests/05-gateway-serviceaccount.yaml  | kubectl apply -f -
sed "s|\${NAMESPACE}|<namespace>|g" manifests/10-gateway-deployment.yaml     | kubectl apply -f -
sed "s|\${NAMESPACE}|<namespace>|g" manifests/11-gateway-service.yaml        | kubectl apply -f -
sed "s|\${NAMESPACE}|<namespace>|g" manifests/20-gateway.yaml                | kubectl apply -f -
sed "s|\${NAMESPACE}|<namespace>|g" manifests/21-virtualservice.yaml         | kubectl apply -f -
sed "s|\${NAMESPACE}|<namespace>|g" manifests/30-echo-configmap.yaml         | kubectl apply -f -
sed "s|\${NAMESPACE}|<namespace>|g" manifests/31-echo-deployment.yaml        | kubectl apply -f -
sed "s|\${NAMESPACE}|<namespace>|g" manifests/32-echo-service.yaml           | kubectl apply -f -
kubectl -n <namespace> rollout status deployment/xff-demo-gateway --timeout=120s
kubectl -n <namespace> rollout status deployment/echo --timeout=120s
kubectl -n <namespace> get deploy,svc,gateway,virtualservice
```

`manifests/40-numtrustedproxies-patch.yaml` wird von `install.sh` **nicht**
appliziert — es ist keine eigenständige Ressource, sondern eine Patch-Vorlage,
die nur von `set-numtrustedproxies.sh` verwendet wird (s.u.).

### Wie das Gateway entsteht (Gateway Injection)

`manifests/10-gateway-deployment.yaml` enthält bewusst nur einen
Platzhalter-Container (`image: auto`) und die Annotation
`inject.istio.io/templates: "gateway"`. Der Sidecar-Injector von Istio ersetzt
diesen Platzhalter beim Pod-Start durch einen vollwertigen, eigenständigen
Envoy-Ingress-Gateway. Dadurch entsteht ein **eigenes** Gateway nur für diesen
Namespace/Usecase — der geteilte `istio-ingressgateway` in `istio-system`
bleibt unangetastet.

## Baseline testen (`numTrustedProxies` nicht gesetzt)

```bash
./run.sh                             # Standard-Namespace: xff-demo, XFF: 203.0.113.42
./run.sh mein-namespace              # eigener Namespace
./run.sh mein-namespace 198.51.100.7 # eigener Namespace, eigene vorgetäuschte Client-IP
```

Führt intern aus (Platzhalter aus `test/curl-pod.yaml` per `sed` ersetzt):

```bash
sed -e "s|\${POD_NAME}|<generierter-name>|g" \
    -e "s|\${NAMESPACE}|<namespace>|g" \
    -e "s|\${TARGET}|http://xff-demo-gateway.<namespace>.svc.cluster.local/|g" \
    -e "s|\${XFF_VALUE}|203.0.113.42|g" \
    test/curl-pod.yaml | kubectl apply -f -

kubectl -n <namespace> wait --for=jsonpath='{.status.phase}'=Succeeded pod/<generierter-name> --timeout=60s
kubectl -n <namespace> logs <generierter-name>
kubectl -n <namespace> get deployment xff-demo-gateway -o jsonpath='{.spec.template.metadata.annotations.proxy\.istio\.io/config}'
kubectl -n <namespace> delete pod <generierter-name>
```

Der Test-Pod läuft **ohne** Istio-Sidecar (`sidecar.istio.io/inject: "false"`),
er simuliert einen externen Client, der direkt (mit gefälschtem XFF-Header)
gegen das Gateway spricht.

Beispielhafte, real gemessene Ausgabe **ohne** gesetztes `numTrustedProxies`
(Default-Verhalten):

```
==> Antwort des echo-Backends (gesehen NACH Durchlauf durch das Gateway):
remote_addr=127.0.0.6
x_forwarded_for=203.0.113.42,10.244.1.23
x_envoy_external_address=10.244.1.23
==> Aktuelle numTrustedProxies-Konfiguration des Gateways:
(keine proxy.istio.io/config-Annotation gesetzt -> Default: numTrustedProxies=0)
```

`x_envoy_external_address` entspricht hier der Pod-IP des Test-Pods (dem
direkten TCP-Peer) — die gefälschte `203.0.113.42` wird zwar in
`X-Forwarded-For` mitgeführt, aber von Envoy **nicht** als vertrauenswürdige
Client-Adresse übernommen.

## `numTrustedProxies` aktivieren und konfigurieren

```bash
./set-numtrustedproxies.sh                    # Usage-Hinweis
./set-numtrustedproxies.sh xff-demo 1         # 1 vertrauenswürdiger Proxy-Hop
./set-numtrustedproxies.sh xff-demo none      # Annotation wieder entfernen (zurück auf Default 0)
```

Führt intern aus (Platzhalter aus `manifests/40-numtrustedproxies-patch.yaml`
per `sed` ersetzt, dann als strategic-merge-Patch auf das Deployment
angewendet):

```bash
sed "s|\${NUM_TRUSTED_PROXIES}|1|g" manifests/40-numtrustedproxies-patch.yaml > /tmp/patch.yaml
kubectl -n <namespace> patch deployment xff-demo-gateway --type merge --patch-file /tmp/patch.yaml
kubectl -n <namespace> rollout status deployment/xff-demo-gateway --timeout=90s
kubectl -n <namespace> get deployment xff-demo-gateway -o jsonpath='{.spec.template.metadata.annotations.proxy\.istio\.io/config}'
```

Für `none` wird stattdessen ein Patch mit `proxy.istio.io/config: null`
appliziert, was die Annotation aus dem Pod-Template entfernt (JSON-Merge-Patch-
Semantik: `null` löscht das Feld).

Der Patch setzt die Pod-Annotation `proxy.istio.io/config` auf dem
Gateway-Deployment:

```yaml
proxy.istio.io/config: |
  gatewayTopology:
    numTrustedProxies: 1
```

Das löst einen Rollout des Gateway-Pods aus (neue Envoy-Bootstrap-Konfiguration).

### Getestete Ergebnisse (`./run.sh` nach jeweiligem `set-numtrustedproxies.sh`)

| `numTrustedProxies` | `x_envoy_external_address` (bei vorgetäuschtem XFF `203.0.113.42`) |
|---|---|
| nicht gesetzt (Default 0) | Pod-IP des Test-Clients (XFF wird ignoriert) |
| `1` (**korrekt**, genau 1 Hop vor dem Gateway simuliert) | `203.0.113.42` — die vorgetäuschte Client-IP wird korrekt übernommen |
| `2` (zu hoch) | Pod-IP des Test-Clients — fällt still auf das Default-Verhalten zurück |

Beispiel für den korrekten Fall (`numTrustedProxies: 1`):

```
==> Antwort des echo-Backends (gesehen NACH Durchlauf durch das Gateway):
remote_addr=127.0.0.6
x_forwarded_for=203.0.113.42,10.244.1.32
x_envoy_external_address=203.0.113.42
==> Aktuelle numTrustedProxies-Konfiguration des Gateways:
gatewayTopology:
  numTrustedProxies: 1
```

**Praxis-Hinweis:** `numTrustedProxies` muss exakt der Anzahl der Proxy-Hops
entsprechen, die tatsächlich vor dem Istio-Gateway stehen (z.B. `1` für einen
einzelnen Cloud-Loadbalancer, `2` falls zusätzlich noch ein CDN davor sitzt).
Da ein zu hoher Wert nicht auffällt (kein Fehler, nur stilles Zurückfallen auf
die direkte Peer-Adresse), sollte der Wert nach jeder Änderung der
Infrastruktur vor dem Gateway (z.B. Hinzufügen/Entfernen eines CDN) erneut mit
diesem Test verifiziert werden.

## Aufräumen

```bash
./uninstall.sh                   # Standard-Namespace: xff-demo
./uninstall.sh mein-namespace    # eigenen Namespace verwenden
```

Entfernt nur die von diesem Usecase angelegten Ressourcen (Gateway,
VirtualService, Deployments, Services, ConfigMap, ServiceAccount) — der
Namespace selbst bleibt bestehen. Zum vollständigen Entfernen:

```bash
kubectl delete namespace <namespace>   # z.B. xff-demo
```

## Übertragung auf den geteilten `istio-ingressgateway`

Dieser Usecase deployt bewusst ein eigenes Gateway per Gateway-Injection, um
den geteilten `istio-ingressgateway` in `istio-system` nicht zu verändern.
Für den produktiven `istio-ingressgateway` gilt dieselbe Annotation, nur auf
dessen eigenem Deployment (i.d.R. `istio-ingressgateway` im Namespace
`istio-system`):

```bash
kubectl -n istio-system patch deployment istio-ingressgateway --type merge --patch-file /tmp/patch.yaml
```

Alternativ mesh-weit für **alle** Gateways über `IstioOperator`:

```yaml
apiVersion: install.istio.io/v1alpha1
kind: IstioOperator
spec:
  meshConfig:
    defaultConfig:
      gatewayTopology:
        numTrustedProxies: 1
```

**Achtung:** Der geteilte `istio-ingressgateway` wird typischerweise von
mehreren Teams/Anwendungen genutzt — ein Patch dort betrifft den gesamten
eingehenden Cluster-Traffic und sollte vorher abgestimmt und in einer
Nicht-Produktionsumgebung mit genau diesem Test verifiziert werden.
