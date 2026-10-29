# Istio Egress Gateway auf OpenShift (Open-Source-Istio, OVN-Kubernetes)

Diese Anleitung baut auf der Basis-Installation aus [`../readme.md`](../readme.md) auf
(istio-base, istio-cni, istiod per Upstream-Helm-Charts mit `platform=openshift`). Sie fuegt
einen Istio **Egress Gateway** hinzu, leitet den Traffic eines Demo-Namespace zu `github.com`
darueber und sorgt mit OVN-Kubernetes-Mitteln dafuer, dass der Gateway der einzige Weg aus dem
Cluster ist.

## Wozu ein Egress Gateway

Ohne Egress Gateway geht jeder Pod direkt vom eigenen Node ins Netz. Die Quell-IP ist die IP
des Nodes, auf dem der Pod gerade laeuft, und jeder Sidecar entscheidet selbst, wohin er darf.
Ein Egress Gateway buendelt den ausgehenden Traffic in wenigen, dafuer abgestellten Envoy-Pods.
Das bringt drei Dinge:

- **Ein Kontrollpunkt.** Welche externen Hosts erreichbar sind, steht in einem `Gateway`-Objekt
  in einem Namespace, den das Plattform-Team verwaltet.
- **Ein Logpunkt.** Das Access-Log des Gateways zeigt jede ausgehende Verbindung mit Quell-Pod,
  SNI und Ziel.
- **Eine feste Quell-IP.** Mit einer OVN-Kubernetes-`EgressIP` verlassen alle Verbindungen den
  Cluster mit derselben IP. Die Perimeter-Firewall schaltet dann genau diese IP frei statt
  aller Node-IPs. Fuer viele Firewall-Teams ist das der eigentliche Grund fuer das ganze Setup.

**Istio allein erzwingt nichts.** Das Umleiten zum Gateway macht der Sidecar des Clients. Ein
Pod ohne Sidecar oder ein Prozess, der die iptables-/nftables-Umleitung umgeht, geht weiterhin
direkt raus. Die Istio-Doku zu Egress Gateways sagt das im Abschnitt "Additional security
considerations" selbst. Erzwingen muss es die Netzwerkschicht
darunter, auf OpenShift also OVN-Kubernetes mit `EgressFirewall` oder `NetworkPolicy`
(Schritt 5).

## Architektur

```
Namespace egress-demo (istio-injection: enabled, EgressFirewall: Deny 0.0.0.0/0)
  curl-Pod --https://github.com--> istio-proxy (Sidecar)
                                       |
                                       | VirtualService "github-via-egressgateway", Regel gateways: mesh
                                       | TLS-Passthrough, SNI github.com, Port 443
                                       v
Namespace istio-egress (EgressFirewall: Allow github.com:443, sonst Deny)
  Service istio-egressgateway (ClusterIP) -> Envoy-Pods (Label istio=egressgateway)
                                       |
                                       | Gateway "egress-github" (Port 443, TLS PASSTHROUGH)
                                       | VirtualService-Regel gateways: istio-egress/egress-github
                                       v
                          OVN-Kubernetes SNAT auf EgressIP (feste Quell-IP)
                                       |
                                       v
                                   github.com:443

  curl-nosidecar-Pod --https://github.com--> direkt Richtung Internet
                                       |
                                       x  EgressFirewall im Namespace egress-demo verwirft das Paket
```

Zwei Namespaces mit getrennten Rollen:

| Namespace | Inhalt | Wer verwaltet |
|---|---|---|
| `istio-egress` | Egress-Gateway-Deployment, `Gateway`-Objekt, Access-Log, EgressFirewall, EgressIP-Zuordnung | Plattform-Team |
| `egress-demo` | Workloads, `ServiceEntry`, `DestinationRule`, `VirtualService`, EgressFirewall | App-Team |

**Warum ein eigener Namespace `istio-egress` statt `istio-system`?** `EgressFirewall`,
`EgressIP`-Namespace-Selector und `NetworkPolicy` wirken pro Namespace. In `istio-system` laufen
auch istiod und der Ingress Gateway. Eine `Deny 0.0.0.0/0`-Firewall dort wuerde istiod vom
API-Server abschneiden. Ausserdem verwirft OVN-Kubernetes im Shared-Gateway-Modus laut
Red-Hat-Doku auch Antwortpakete auf eingehende Verbindungen, wenn die EgressFirewall deren
Ziel-IP sperrt, was den Ingress Gateway treffen kann. Die Red-Hat-Doku nutzt fuer ihr
Gateway-API-Beispiel ebenfalls einen eigenen Egress-Namespace.

## Voraussetzungen

- Basis-Plattform aus [`../readme.md`](../readme.md) Schritt 1 ist installiert
  (`./install-istio.sh`). Pruefen:
  ```bash
  helm list -n istio-system
  helm list -n kube-system
  oc -n istio-system get pods -l app=istiod
  ```
- Cluster-Admin-Rechte (SCC-Vergabe, `EgressIP` ist cluster-scoped, Node-Labels).
- OVN-Kubernetes als Netzwerk-Plugin (ab OpenShift 4.17 immer der Fall):
  ```bash
  oc get network.config/cluster -o jsonpath='{.status.networkType}{"\n"}'   # erwartet: OVNKubernetes
  ```
- Fuer Schritt 7 (EgressIP): eine freie IP im Subnetz der Nodes und eine Plattform, auf der
  OVN-Kubernetes EgressIP unterstuetzt (Bare Metal, vSphere, RHOSP, AWS, Google Cloud, Azure,
  IBM Z/Power, Nutanix).
- Alle Befehle unten laufen aus dem Verzeichnis `ovintegration/egress-gateway/`. Die Manifeste
  liegen in `manifests/` und verwenden fest den Demo-Namespace `egress-demo`. Nur die
  EgressIP in `manifests/40-egressip.yaml` muss vor Schritt 7 angepasst werden.

```bash
cd ovintegration/egress-gateway
```

## Schritt 1: Egress Gateway installieren

```bash
oc create namespace istio-egress
oc adm policy add-scc-to-group anyuid system:serviceaccounts:istio-egress

helm upgrade --install istio-egressgateway istio/gateway -n istio-egress \
  --set platform=openshift \
  --set service.type=ClusterIP \
  --wait

oc apply -f manifests/05-telemetry-accesslog.yaml
```

Was dabei passiert:

- **Derselbe Chart wie beim Ingress Gateway.** `istio/gateway` kennt keinen Unterschied zwischen
  Ingress und Egress. Ob ein Gateway eingehenden oder ausgehenden Traffic verarbeitet, legen
  erst die `Gateway`- und `VirtualService`-Objekte fest. Fuer einen Egress Gateway reicht
  `service.type=ClusterIP`. Ihn sprechen nur Sidecars im Cluster an, eine Route oder ein
  LoadBalancer waere ueberfluessig.
- **Label `istio: egressgateway`.** Wie beim Ingress Gateway leitet der Chart das Label aus dem
  Release-Namen ab und entfernt das Praefix `istio-`. Aus `istio-egressgateway` wird
  `istio: egressgateway`. Darauf selektieren das `Gateway`-Objekt (Schritt 3) und die `EgressIP`
  (Schritt 7).
- **Gateway-Injection.** Das Deployment des Charts traegt `inject.istio.io/templates: gateway`
  und `sidecar.istio.io/inject: "true"`. istiod injiziert dadurch den Envoy als Gateway statt
  als Sidecar. Das ist dieselbe Methode, die Red Hat fuer OpenShift Service Mesh 3 beschreibt,
  dort mit einem von Hand geschriebenen Deployment. Pruefen mit
  `helm template istio-egressgateway istio/gateway -n istio-egress --set service.type=ClusterIP`.
- **Ports 80/443 ohne Root.** Der Chart setzt die Pod-Sysctl
  `net.ipv4.ip_unprivileged_port_start=0`. Sie gilt seit Kubernetes 1.22 als "safe sysctl" und
  wird von `restricted-v2` nicht blockiert. Der Container laeuft mit `runAsNonRoot`, ohne
  Capabilities, und bindet trotzdem 443 direkt. Service-Port und Pod-Port sind deshalb beide 443.
- **`anyuid`** ist hier wie bei `istio-system` im readme ein Sicherheitsnetz fuer die UID des
  injizierten Proxys, siehe [`../readme.md`](../readme.md), Abschnitt "Warum anyuid-SCC?".
- **Access-Log nur fuer den Gateway.** `05-telemetry-accesslog.yaml` ist eine
  `Telemetry`-Ressource im Namespace `istio-egress` mit dem eingebauten Provider `envoy`. Die
  Red-Hat-Doku schaltet stattdessen mesh-weit `meshConfig.accessLogFile=/dev/stdout` ein. Mit
  Helm waere das `--set meshConfig.accessLogFile=/dev/stdout` am `istiod`-Release.

Pruefen:

```bash
oc -n istio-egress get pods,svc --show-labels
oc -n istio-egress rollout status deployment/istio-egressgateway
```

Der Pod muss `1/1 Running` sein und die Labels `app=istio-egressgateway,istio=egressgateway`
tragen.

## Schritt 2: Demo-Namespace mit Test-Clients

```bash
oc apply -f manifests/00-namespace.yaml
oc adm policy add-scc-to-group anyuid system:serviceaccounts:egress-demo

oc apply -f manifests/10-curl.yaml
oc apply -f manifests/11-curl-nosidecar.yaml
oc -n egress-demo rollout status deployment/curl
oc -n egress-demo rollout status deployment/curl-nosidecar
```

- `curl` laeuft mit Sidecar (`2/2`). Mit ihm pruefen die Tests, ob Traffic ueber den Gateway geht.
- `curl-nosidecar` laeuft ohne Sidecar (`1/1`, Pod-Label `sidecar.istio.io/inject: "false"`).
  Er steht fuer einen Workload, der Istio umgeht, und zeigt, ob die Sperre in Schritt 5 greift.

Ausgangszustand, noch ohne Egress-Konfiguration:

```bash
oc -n egress-demo exec deploy/curl -c curl -- \
  curl -sS -o /dev/null -w '%{http_code}\n' --max-time 10 https://github.com
oc -n egress-demo exec deploy/curl-nosidecar -- \
  curl -sS -o /dev/null -w '%{http_code}\n' --max-time 10 https://github.com
```

Beide liefern `200`. Der Sidecar laesst unbekannte Ziele standardmaessig durch
(`outboundTrafficPolicy: ALLOW_ANY`), beide Verbindungen gehen direkt vom Node ins Internet.

## Schritt 3: Traffic zu github.com ueber den Gateway leiten

```bash
oc apply -f manifests/20-serviceentry-github.yaml
oc apply -f manifests/21-gateway-egress.yaml
oc apply -f manifests/22-destinationrule-egressgateway.yaml
oc apply -f manifests/23-virtualservice-github.yaml
```

Die vier Objekte und ihre Aufgabe:

**`ServiceEntry` `github`** (`egress-demo`). Traegt `github.com:443` als `MESH_EXTERNAL` mit
Protokoll `TLS` und `resolution: DNS` in die Service-Registry ein. Erst dadurch kann ein
`VirtualService` `github.com` als Ziel verwenden, und der Egress Gateway weiss, wie er den Host
aufloest. Ohne `exportTo` ist der Eintrag mesh-weit sichtbar, also auch fuer den Gateway in
`istio-egress`.

**`Gateway` `egress-github`** (`istio-egress`). Oeffnet am Egress-Gateway-Envoy einen Listener
auf 443 fuer SNI `github.com` mit `tls.mode: PASSTHROUGH`:

```yaml
spec:
  selector:
    istio: egressgateway
  servers:
    - port: {number: 443, name: tls, protocol: TLS}
      hosts: [github.com]
      tls:
        mode: PASSTHROUGH
```

`PASSTHROUGH` heisst: Envoy liest nur die SNI aus dem TLS-ClientHello und leitet den
verschluesselten Strom weiter. Der Handshake laeuft Ende-zu-Ende zwischen `curl` und
`github.com`, der Gateway sieht keine HTTP-Pfade oder Header. Wer am Gateway HTTP-Details sehen
oder TLS selbst aufbauen will, braucht TLS-Origination, siehe
[`../../externalServiceTls/`](../../externalServiceTls/).

**`DestinationRule` `egressgateway-for-github`** (`egress-demo`). Definiert ein Subset `github`
auf `istio-egressgateway.istio-egress.svc.cluster.local`. In diesem Beispiel ohne
`trafficPolicy`. Hier haengt man spaeter Einstellungen fuer die Strecke Sidecar zu Gateway an,
etwa `tls.mode: ISTIO_MUTUAL` oder Connection-Pool-Limits. Upstream-Istio und Red Hat verwenden
dasselbe Muster.

**`VirtualService` `github-via-egressgateway`** (`egress-demo`). Ist an zwei "Gateways"
gebunden und enthaelt pro Abschnitt der Strecke eine Regel:

```yaml
  gateways:
    - mesh                          # alle Sidecars im Mesh
    - istio-egress/egress-github    # der Egress Gateway
  tls:
    - match: [{gateways: [mesh], port: 443, sniHosts: [github.com]}]
      route:
        - destination:
            host: istio-egressgateway.istio-egress.svc.cluster.local
            subset: github
            port: {number: 443}
    - match: [{gateways: [istio-egress/egress-github], port: 443, sniHosts: [github.com]}]
      route:
        - destination:
            host: github.com
            port: {number: 443}
```

Regel 1 wirkt im Sidecar des `curl`-Pods. Verbindungen mit SNI `github.com` auf Port 443 gehen
zum Egress-Gateway-Service statt direkt zur aufgeloesten github.com-IP. Regel 2 wirkt im
Egress Gateway. Was dort mit SNI `github.com` ankommt, geht zum externen Host aus dem
`ServiceEntry`.

Test:

```bash
oc -n egress-demo exec deploy/curl -c curl -- \
  curl -sS -o /dev/null -w '%{http_code}\n' --max-time 10 https://github.com
oc -n istio-egress logs deploy/istio-egressgateway --tail=5
```

Erwartet: `200` und im Gateway-Log eine neue Zeile mit `outbound|443||github.com` und
`github.com` als SNI. Steht dort nichts, ging die Verbindung am Gateway vorbei, siehe
Troubleshooting.

## Schritt 4: Was jetzt noch fehlt

```bash
# 1) Client ohne Sidecar
oc -n egress-demo exec deploy/curl-nosidecar -- \
  curl -sS -o /dev/null -w '%{http_code}\n' --max-time 10 https://github.com

# 2) Client mit Sidecar, aber ein Ziel ohne Egress-Konfiguration
oc -n egress-demo exec deploy/curl -c curl -- \
  curl -sS -o /dev/null -w '%{http_code}\n' --max-time 10 https://api.github.com
```

Beide liefern weiterhin `200`, und im Gateway-Log erscheint keine Zeile. Fall 1 kennt Istio gar
nicht. Fall 2 geht ueber den `PassthroughCluster` des Sidecars direkt raus, weil
`ALLOW_ANY` gilt und fuer `api.github.com` kein `VirtualService` existiert. Bis hierhin ist der
Egress Gateway nur ein Angebot, keine Pflicht.

## Schritt 5: Direkten Egress aus dem App-Namespace sperren

### Variante A: EgressFirewall (OpenShift-nativ, empfohlen)

```bash
oc apply -f manifests/30-egressfirewall-app.yaml
oc -n egress-demo get egressfirewall default
```

```yaml
apiVersion: k8s.ovn.org/v1
kind: EgressFirewall
metadata:
  name: default            # Pflichtname, maximal eine EgressFirewall pro Namespace
  namespace: egress-demo
spec:
  egress:
    - type: Deny
      to:
        cidrSelector: 0.0.0.0/0
```

Eine einzige Regel genuegt, weil OVN-Kubernetes das Cluster-Netz aus einer CIDR-Regel
automatisch herausnimmt. Ueberschneidet `cidrSelector` das Pod-Netz (z. B. `10.128.0.0/14`),
erzeugt der Controller eine ACL der Form
`ip4.dst == 0.0.0.0/0 && ip4.dst != 10.128.0.0/14` (Quelle:
`go-controller/pkg/ovn/controller/egressfirewall/egressfirewall.go` in ovn-kubernetes,
`getV4ClusterSubnetsExclusion`). Die ACL greift nach der Service-Uebersetzung. Deshalb gilt:

| Ziel | Ergebnis |
|---|---|
| Egress Gateway, istiod, DNS (`dns-default`), andere Pods und ClusterIP-Services mit Pod-Backends | erlaubt |
| Kubernetes-API-Server (`kubernetes.default`, Backends im Host-Netz) | gesperrt |
| alles ausserhalb des Clusters | gesperrt |

Braucht ein Workload im Namespace den API-Server, vor der Deny-Regel eine `Allow`-Regel fuer
die Control-Plane-IPs einfuegen (`oc get endpoints kubernetes -n default`). Bei Dual-Stack
zusaetzlich eine Deny-Regel fuer `::/0`.

### Variante B: NetworkPolicy

```bash
oc apply -f manifests/alternative-networkpolicy-app.yaml
```

Statt sperren, was nach draussen geht, erlaubt die Policy nur explizit aufgezaehlte Ziele:
eigener Namespace, DNS, istiod (TCP 15012) und die Egress-Gateway-Pods (TCP 443). Das ist der
Weg, den die Istio-Doku generisch empfiehlt, und er sperrt auch Verbindungen in andere
Namespaces. Zu beachten:

- **DNS auf Port 5353.** Der Service `dns-default` in `openshift-dns` lauscht auf 53, die
  CoreDNS-Pods aber auf 5353. OVN-Kubernetes wertet NetworkPolicy nach der Service-Uebersetzung
  auf Pod-Ebene aus, also muss 5353 freigegeben sein. Das ist OpenShift-spezifisch. Mit Port 53 schlaegt jede Namensaufloesung fehl.
- **Mehr Pflege.** Jedes weitere interne Ziel (andere Namespaces, Datenbanken) braucht eine
  eigene Regel.

Beide Varianten koennen zusammen aktiv sein. Fuer diese Demo reicht eine.

### Test nach Schritt 5

```bash
# ueber den Gateway: weiterhin erlaubt
oc -n egress-demo exec deploy/curl -c curl -- \
  curl -sS -o /dev/null -w '%{http_code}\n' --max-time 10 https://github.com

# ohne Sidecar: jetzt gesperrt
oc -n egress-demo exec deploy/curl-nosidecar -- \
  curl -sS -o /dev/null -w '%{http_code}\n' --max-time 10 https://github.com

# Ziel ohne Gateway-Route: jetzt gesperrt
oc -n egress-demo exec deploy/curl -c curl -- \
  curl -sS -o /dev/null -w '%{http_code}\n' --max-time 10 https://api.github.com
```

Erwartet: `200`, dann zweimal `000` mit Timeout-Meldung (`curl: (28) ...`). Die EgressFirewall
verwirft die Pakete still, deshalb kommt kein Connection-Refused, sondern ein Timeout.

Fall 3 laesst sich mit `outboundTrafficPolicy: REGISTRY_ONLY` zusaetzlich schon im Sidecar
abfangen. Dann scheitert er sofort statt nach dem Timeout, und das Proxy-Log zeigt
`BlackHoleCluster`. Per Namespace geht das mit einer `Sidecar`-Ressource wie in
[`../../egressLockdown/`](../../egressLockdown/readme.md), mesh-weit mit
`--set meshConfig.outboundTrafficPolicy.mode=REGISTRY_ONLY` am `istiod`-Release. Das ist eine
Komfort- und Diagnosehilfe, keine Sicherheitsgrenze, denn Pods ohne Sidecar betrifft es nicht.

## Schritt 6: Den Gateway selbst auf github.com beschraenken

```bash
oc apply -f manifests/31-egressfirewall-gateway.yaml
oc -n istio-egress get egressfirewall default -o yaml
```

```yaml
spec:
  egress:
    - type: Allow
      to:
        dnsName: github.com
      ports:
        - {protocol: TCP, port: 443}
    - type: Deny
      to:
        cidrSelector: 0.0.0.0/0
```

Schritt 5 stellt sicher, dass Workloads nur ueber den Gateway rauskommen. Schritt 6 legt fest,
wohin der Gateway darf. Legt jemand spaeter ein weiteres `Gateway`/`VirtualService`-Paar fuer
einen anderen Host an, routet Istio den Traffic zwar zum Gateway, die Firewall laesst ihn dort
aber nicht raus. Neue Ziele brauchen damit zwei Freigaben: Istio-Objekte im App-Namespace und
eine Firewall-Regel im Plattform-Namespace.

OVN-Kubernetes wertet die Regeln von oben nach unten aus, die erste passende gilt. Der Gateway erreicht
istiod und DNS weiterhin, weil sie im Pod-Netz liegen (siehe Schritt 5). Den API-Server braucht
er nicht.

Hinweise zu `dnsName`-Regeln laut Red-Hat-Doku:

- OVN-Kubernetes loest den Namen selbst auf und aktualisiert die IPs nach TTL, spaetestens alle
  30 Minuten. Der Envoy im Gateway loest unabhaengig davon auf. Bei Hosts mit haeufig
  wechselnden IPs kann es kurzzeitig auseinanderlaufen. Fuer Ziele mit festen Netzen ist
  `cidrSelector` robuster.
- Wildcards (`*.github.com`) gibt es nur ueber den `DNSNameResolver`, der als Technology
  Preview markiert ist.

Test, ohne den Gateway selbst anzufassen. Ein Wegwerf-Pod in `istio-egress` unterliegt derselben
namespaceweiten Firewall:

```bash
oc -n istio-egress run fw-test --rm -i --restart=Never --image=curlimages/curl:8.11.0 \
  --command -- curl -sS -o /dev/null -w '%{http_code}\n' --max-time 10 https://github.com
oc -n istio-egress run fw-test --rm -i --restart=Never --image=curlimages/curl:8.11.0 \
  --command -- curl -sS -o /dev/null -w '%{http_code}\n' --max-time 10 https://api.github.com
```

Erwartet: `200`, dann `000` (Timeout).

## Schritt 7: Feste Quell-IP mit EgressIP

```bash
# 1. Nodes markieren, die Egress-IPs tragen duerfen (vor dem EgressIP-Objekt!)
oc label node <node-1> k8s.ovn.org/egress-assignable=""
oc label node <node-2> k8s.ovn.org/egress-assignable=""

# 2. EgressIP anlegen (vorher 192.0.2.10 in manifests/40-egressip.yaml durch eine freie IP
#    im Node-Subnetz ersetzen)
oc apply -f manifests/40-egressip.yaml

# 3. Zuordnung pruefen
oc get egressip istio-egressgateway -o yaml
```

```yaml
apiVersion: k8s.ovn.org/v1
kind: EgressIP
metadata:
  name: istio-egressgateway        # cluster-scoped, kein Namespace
spec:
  egressIPs:
    - 192.0.2.10
  namespaceSelector:
    matchLabels:
      kubernetes.io/metadata.name: istio-egress
  podSelector:
    matchLabels:
      istio: egressgateway
```

OVN-Kubernetes setzt fuer alle Pods, auf die beide Selektoren passen, die Quell-IP ausgehender
Verbindungen per SNAT auf die EgressIP. Dazu leitet es den Traffic ueber den Node, der die IP
gerade traegt. Nur die Egress-Gateway-Pods bekommen sie, nicht der ganze Cluster.

- `status.items` im YAML zeigt, welchem Node die IP zugewiesen ist. `oc get egressip` ohne
  `-o yaml` zeigt bei mehreren IPs nur eine an.
- Erst Nodes labeln, dann das Objekt anlegen. Sonst landen laut Red-Hat-Doku alle IPs auf dem
  ersten gelabelten Node.
- Faellt der Node aus, verschiebt OVN-Kubernetes die IP auf einen anderen Node mit
  `egress-assignable`-Label. Die Erkennung dauert Sekunden
  (`reachabilityTotalTimeoutSeconds` im `egressIPConfig` des Network-Operators).
- Passt ein Pod auf mehrere `EgressIP`-Objekte, ist nicht festgelegt, welche IP er nutzt. Keine
  zweite EgressIP mit ueberlappenden Selektoren anlegen.
- In Public Clouds begrenzt der Provider die IPs pro Node, siehe Node-Annotation
  `cloud.network.openshift.io/egress-ipconfig`. Auf AWS duerfen Control-Plane-Nodes keine
  Egress-IPs tragen.
- Die HPA des Charts skaliert den Gateway zwischen 1 und 5 Replicas. Alle Replicas teilen sich
  dieselbe EgressIP, weil der `podSelector` auf das Label und nicht auf einzelne Pods zielt.

Zum Testen auf dem Node, der laut `status.items[].node` die IP traegt, den ausgehenden Traffic
mitschneiden und parallel `curl` aus Schritt 3 ausfuehren:

```bash
oc debug node/<egress-node> -- chroot /host \
  tcpdump -nn -i br-ex -c 5 "src host 192.0.2.10 and tcp port 443"

# zweites Terminal
oc -n egress-demo exec deploy/curl -c curl -- \
  curl -sS -o /dev/null -w '%{http_code}\n' --max-time 10 https://github.com
```

Erscheinen Pakete mit der EgressIP als Quelle, stimmt die SNAT-Kette. In der Perimeter-Firewall
muss jetzt genau diese IP fuer `github.com:443` freigeschaltet sein.

## Abgleich mit der Red-Hat-Dokumentation

Red Hat beschreibt den Egress Gateway fuer OpenShift Service Mesh 3 im Kapitel "Directing
outbound traffic through a gateway" der Gateways-Doku. OSSM 3 basiert auf Upstream-Istio
(Sail Operator). Die Unterschiede liegen in Installation und Beispielwerten, nicht im
Mechanismus.

| Thema | Red Hat OSSM 3 | Diese Anleitung |
|---|---|---|
| Control Plane | `Istio`- und `IstioCNI`-Ressource ueber den Operator | Helm-Charts `istio/base`, `istio/cni`, `istio/istiod` mit `platform=openshift` |
| Gateway-Deployment | Deployment, Service, ServiceAccount, Role, RoleBinding von Hand, Annotation `inject.istio.io/templates: gateway` | Helm-Chart `istio/gateway`, erzeugt dieselben Objekte mit derselben Annotation |
| Gateway-Namespace | Platzhalter `<gateway_namespace>`, im Gateway-API-Beispiel `egress-gateway` | `istio-egress` |
| Unprivilegierte Ports | Sysctl `net.ipv4.ip_unprivileged_port_start: "0"`, `runAsNonRoot`, alle Capabilities gedroppt | identisch, vom Chart gesetzt |
| Beispielziel | `docs.redhat.com`, HTTP Port 80 | `github.com`, TLS-Passthrough Port 443 (wie istio.io "Egress Gateways", HTTPS-Abschnitt) |
| Objekte | `ServiceEntry`, `Gateway`, `DestinationRule` mit Subset, `VirtualService` mit Regeln fuer `mesh` und Gateway | gleiches Muster |
| Access-Log | `spec.values.meshConfig.accessLogFile: /dev/stdout` am `Istio`-Objekt (mesh-weit) | `Telemetry`-Ressource nur in `istio-egress` |
| Gateway API | eigene Variante mit `Gateway` (Klasse `istio`) und zwei `HTTPRoute`s; Pflicht im Ambient-Modus | nicht verwendet, Sidecar-Modus mit Istio-API |
| Erzwingen, EgressIP | im Gateways-Kapitel nicht behandelt | Schritte 5 bis 7 nach der OpenShift-Netzwerk-Doku (EgressFirewall, EgressIP) |

Ab OpenShift 4.19 verwaltet der Ingress Operator die Gateway-API-CRDs. Eine
`GatewayClass` mit `controllerName: openshift.io/gateway-controller/v1` loest die Installation von
OSSM 3 aus und kollidiert mit dieser Installation (siehe Voraussetzungen in
[`../readme.md`](../readme.md)). Wer die Gateway-API-Variante mit Upstream-Istio nutzt, bleibt bei
der von istiod angelegten Klasse `istio`.

OpenShift Service Mesh 2.x konfigurierte den Egress Gateway ueber `spec.gateways.egress` im
`ServiceMeshControlPlane`. Das ist fuer Upstream-Istio per Helm nicht relevant.

## Troubleshooting

**Schritt 3: `curl` liefert `200`, aber das Gateway-Log bleibt leer.** Die Verbindung ging
direkt raus.

```bash
oc -n egress-demo get serviceentry,destinationrule,virtualservice
istioctl -n egress-demo proxy-config listener deployment/curl --port 443
istioctl -n egress-demo proxy-config cluster deployment/curl | grep -E 'github|egressgateway'
```

Im Listener auf 443 muss eine Filter-Chain fuer SNI `github.com` auf
`outbound|443|github|istio-egressgateway.istio-egress.svc.cluster.local` zeigen. Fehlt sie, `sniHosts` und `port` im `VirtualService` pruefen und ob `curl` wirklich
`2/2` Container hat.

**Schritt 3: `curl` endet mit `(35) ... SSL_ERROR_SYSCALL` oder Connection reset.** Der Sidecar
schickt zum Gateway, der Gateway findet aber keine passende Regel.

```bash
istioctl -n istio-egress proxy-config listener deployment/istio-egressgateway
oc -n istio-egress get gateway egress-github -o yaml
oc -n istio-egress get pods --show-labels
```

Haeufige Ursachen: `selector` im `Gateway` passt nicht zum Pod-Label (`istio: egressgateway`, nicht
`istio: istio-egressgateway`), der zweite `match` im `VirtualService` nennt das Gateway ohne
Namespace-Praefix (`egress-github` statt `istio-egress/egress-github`), oder `ServiceEntry` bzw.
`VirtualService` sind per `exportTo` nicht fuer `istio-egress` sichtbar. Letzteres auch dann,
wenn mesh-weit `defaultServiceExportTo` oder `defaultVirtualServiceExportTo` auf `.` steht.

**Nach Schritt 5 scheitert auch `curl` mit Sidecar an `github.com`.**

- Der Traffic lief schon vorher nicht ueber den Gateway, sondern direkt raus, und die
  EgressFirewall sperrt ihn jetzt zu Recht. Das Gateway-Log aus Schritt 3 pruefen und den ersten
  Troubleshooting-Punkt durchgehen. An der EgressFirewall selbst liegt es selten. OVN-Kubernetes
  nimmt das Pod-Netz bei jeder CIDR-Regel aus, die es ueberschneidet, auch bei `10.0.0.0/8`.
- Nur bei Variante B: DNS mit Port 53 statt 5353 freigegeben, oder Selektoren fuer istiod
  (`app: istiod`) bzw. Gateway (`istio: egressgateway`) passen nicht.
  ```bash
  oc -n egress-demo exec deploy/curl -c curl -- nslookup github.com
  oc -n egress-demo get networkpolicy egress-only-via-gateway -o yaml
  ```

**Nach Schritt 6 scheitert `curl` mit Sidecar, obwohl Schritt 5 lief.** Die `dnsName`-Regel kennt
noch nicht alle IPs, die der Gateway-Envoy fuer `github.com` aufgeloest hat:

```bash
oc -n istio-egress get egressfirewall default -o yaml        # status: EgressFirewall Rules applied
oc get dnsnameresolver -A                                     # nur bei aktivem DNSNameResolver
istioctl -n istio-egress proxy-config endpoint deployment/istio-egressgateway | grep github
```

**EgressFirewall-Status ist nicht `EgressFirewall Rules applied`.** Name ist nicht
`default`, es gibt schon eine zweite EgressFirewall im Namespace, oder eine Regel kombiniert
`cidrSelector` und `dnsName`. Laut Red-Hat-Doku verwirft OVN-Kubernetes dann im Zweifel allen
externen Traffic des Namespace.

**EgressIP hat keinen Node in `status.items`.** Kein Node traegt
`k8s.ovn.org/egress-assignable`, die IP liegt nicht im Node-Subnetz, oder die Cloud-Kapazitaet
ist erschoepft:

```bash
oc get nodes -l k8s.ovn.org/egress-assignable
oc get node <node> -o jsonpath='{.metadata.annotations.cloud\.network\.openshift\.io/egress-ipconfig}'
oc -n openshift-ovn-kubernetes logs deploy/ovnkube-control-plane -c ovnkube-cluster-manager | grep -i egressip
```

## Aufraeumen

In umgekehrter Reihenfolge:

```bash
oc delete egressip istio-egressgateway
oc label node <node-1> k8s.ovn.org/egress-assignable-
oc label node <node-2> k8s.ovn.org/egress-assignable-

oc -n istio-egress delete egressfirewall default
oc -n egress-demo delete egressfirewall default --ignore-not-found
oc -n egress-demo delete networkpolicy egress-only-via-gateway --ignore-not-found

oc -n egress-demo delete virtualservice github-via-egressgateway
oc -n egress-demo delete destinationrule egressgateway-for-github
oc -n egress-demo delete serviceentry github
oc -n istio-egress delete gateway egress-github

oc adm policy remove-scc-from-group anyuid system:serviceaccounts:egress-demo
oc delete namespace egress-demo

oc -n istio-egress delete telemetry egress-access-log
helm uninstall istio-egressgateway -n istio-egress
oc adm policy remove-scc-from-group anyuid system:serviceaccounts:istio-egress
oc delete namespace istio-egress
```

Die Basis-Plattform (`istio-system`, `istio-cni`) bleibt bestehen. Entfernen mit
`../uninstall-istio.sh`, siehe [`../readme.md`](../readme.md).

## Quellen

- Red Hat OpenShift Service Mesh 3.1, Gateways, Kapitel 1.2.1 "Gateway injection" und
  Kapitel 3 "Directing outbound traffic through a gateway":
  https://docs.redhat.com/en/documentation/red_hat_openshift_service_mesh/3.1/html-single/gateways/index
- OpenShift Container Platform 4.21, Network security, Kapitel 5 "Egress Firewall":
  https://docs.redhat.com/en/documentation/openshift_container_platform/4.21/html/network_security/egress-firewall
- OpenShift Container Platform 4.20, OVN-Kubernetes network plugin, "Configuring an egress IP
  address":
  https://docs.redhat.com/en/documentation/openshift_container_platform/4.20/html/ovn-kubernetes_network_plugin/configuring-egress-ips-ovn
- Istio, Egress Gateways: https://istio.io/latest/docs/tasks/traffic-management/egress/egress-gateway/
- ovn-kubernetes, Ausnahme des Cluster-Netzes in EgressFirewall-ACLs:
  https://github.com/ovn-kubernetes/ovn-kubernetes/blob/master/go-controller/pkg/ovn/controller/egressfirewall/egressfirewall.go
