# Istio Usecases

Sammlung praktischer Istio-Beispiele, jeweils als eigenständiges Verzeichnis mit
Manifesten, Install-/Test-Skripten und eigener `readme.md`.

## Usecases

Jeder Usecase steht für ein wiederkehrendes Betriebsproblem, das sich lösen lässt,
indem man es ins Mesh verlagert statt in jede einzelne Anwendung.

- [`ratelimits/`](ratelimits/readme.md) — Istio Local Rate Limiting am Beispiel eines
  `nginx`-Deployments, abgesichert durch einen `EnvoyFilter` mit Token-Bucket-Limit,
  inklusive Lasttest-Pod zur Verifikation.
  **Usecase:** Schützt einen Service vor Überlast, ohne Rate-Limiting-Code in der
  Anwendung. Das Limit gilt pro Sidecar, das Gesamtlimit wächst also mit der
  Replica-Zahl.
- [`ratelimitService/`](ratelimitService/readme.md) — Istio Global Rate Limiting: ein über
  alle nginx-Replicas gemeinsam geltendes Limit, durchgesetzt via externem
  Envoy-RateLimit-Service (Redis-Backend) statt per-Sidecar-Token-Bucket.
  **Usecase:** Löst genau die Schwäche von `ratelimits`: ein lokales Limit pro
  Sidecar reicht nicht, um z. B. ein vertraglich zugesichertes Kontingent pro Kunde
  durchzusetzen, wenn die Replica-Zahl per Autoscaling schwankt. Der zentrale
  Rate-Limit-Service macht das Limit unabhängig von der Instanzzahl.
- [`namespaceRouting/`](namespaceRouting/readme.md) — Istio Traffic Routing anhand des
  Source Namespace: Clients aus zwei unterschiedlichen Namespaces (`extern`/`intern`)
  rufen denselben Service auf, werden per `VirtualService`/`DestinationRule` aber auf
  unterschiedliche Backend-Pods geroutet.
  **Usecase:** Erlaubt unter derselben URL unterschiedliche Funktionsumfänge für
  unterschiedliche Aufrufer (z. B. reduzierte API für externe Partner, volle API
  intern), ohne dass Clients das an der Adresse erkennen oder die Anwendung selbst
  Herkunfts-Logik einbaut.
- [`faultInjection/`](faultInjection/readme.md) — Istio HTTP Fault Injection: ein
  `VirtualService` injiziert per `x-fault`-Header gezielt Delay (5s) oder Abort
  (HTTP 500) in Requests an ein `nginx`-Deployment, ohne den Service selbst zu ändern.
  **Usecase:** Prüft Timeout-, Retry- und Circuit-Breaker-Konfiguration von Clients
  gegen echtes Fehlverhalten, ohne Testcode im Zielservice und ohne dass ein
  "echter" Fehler erst in Produktion auftreten muss.
- [`externalServiceTls/`](externalServiceTls/readme.md) — Istio TLS Origination für
  Egress-Traffic: `ServiceEntry`, `DestinationRule` (`tls.mode: SIMPLE`) und
  `VirtualService` sorgen dafür, dass der Envoy-Sidecar die TLS-Verbindung zu einem
  externen Dienst (`api.github.com`) selbst aufbaut, während der App-Container nur
  Klartext-HTTP spricht.
  **Usecase:** TLS-Handling (Zertifikate, CA-Bundles, Cipher-Version) wandert aus
  dem Anwendungscode ins Mesh. Das gepinnte CA-Zertifikat ist besonders in
  Offline-/Air-Gap-Umgebungen relevant, die zur Laufzeit kein öffentliches
  CA-Bundle nachladen können.
- [`mirrorLogging/`](mirrorLogging/readme.md) — Istio Traffic Mirroring: ein
  `VirtualService` spiegelt jeden Request an `nginx` zusätzlich (fire-and-forget)
  an einen `logger`-Pod, der Methode, Header und Body protokolliert und dabei den
  `Authorization`-Wert (OAuth-Token) durch `*` ersetzt — der Client bekommt nur die
  Antwort von `nginx` zu sehen.
  **Usecase:** Ermöglicht, eine neue Service-Version mit echtem Produktionstraffic
  zu beobachten oder Requests zu auditieren, ohne Nutzer zu gefährden.
- [`authorizationPolicy/`](authorizationPolicy/readme.md) — Istio AuthorizationPolicy
  Allow/Deny anhand der Source-Workload-Identity: zwei Clients (`client-a`,
  `client-b`) mit jeweils eigenem `ServiceAccount` rufen denselben `backend`-Service
  auf. Per `kubectl apply -f incident.yaml` lässt sich ein Sicherheitsvorfall
  simulieren, der gezielt genau `client-b` (anhand Namespace + ServiceAccount)
  aussperrt, während `client-a` weiterhin zugelassen bleibt.
  **Usecase:** Zeigt, wie sich im Ernstfall (kompromittierter Client) der Zugriff
  eines einzelnen Workloads gezielt sperren lässt, per Deklaration ohne
  Codeänderung oder Redeploy des Backends. Die Identität basiert auf
  mTLS-Zertifikaten (SPIFFE-Identity) statt IP. 
- [`xForwardedFor/`](xForwardedFor/readme.md) — Konfiguration von `X-Forwarded-For`
  am Istio Ingress-Gateway: ein per Gateway-Injection erzeugtes, eigenständiges
  Gateway wird über die Pod-Annotation `proxy.istio.io/config`
  (`gatewayTopology.numTrustedProxies`) so eingestellt, dass es einer definierten
  Anzahl vorgeschalteter Proxy-Hops beim Auswerten von `X-Forwarded-For` vertraut.
  **Usecase:** Ohne korrekt gesetztes `numTrustedProxies` verwirft das Gateway
  entweder die echte Client-IP hinter einem vorgeschalteten Loadbalancer/CDN
  oder übernimmt eine vom Client frei fälschbare IP aus `X-Forwarded-For` —
  beides wirkt sich unmittelbar auf IP-basierte `AuthorizationPolicy`s, Logging
  und Geo-Routing aus. Der Wert muss exakt zur tatsächlichen Hop-Anzahl passen,
  da sowohl ein zu niedriger als auch ein zu hoher Wert kommentarlos auf die
  direkte Peer-Adresse zurückfällt.

- [`egressLockdown/`](egressLockdown/readme.md) — Istio Egress Lockdown: eine
  `Sidecar`-Ressource setzt für einen ganzen Namespace
  `outboundTrafficPolicy.mode: REGISTRY_ONLY`, sodass Workloads darin nur noch
  im Cluster bekannte Ziele (Kubernetes-Services) sowie einen per
  `ServiceEntry` gezielt freigegebenen internen Forgejo-Server
  (`git.gmk.lan:3300`) erreichen — jeder andere Cluster-externe Host wird
  blockiert.
  **Usecase:** Sperrt einen Namespace vollständig gegen Zugriffe außerhalb
  des Clusters — etwa für sensible Workloads, die aus Compliance- oder
  Datenschutzgründen keinerlei ausgehenden Internetzugriff haben dürfen,
  aber dennoch z. B. auf den internen Git-Server zugreifen müssen — ohne
  NetworkPolicies pro Pod pflegen zu müssen und ohne dass die Anwendung
  selbst etwas davon merkt. Interne Cluster-Kommunikation bleibt
  unangetastet.

## Healthcheck

[`istio-healthcheck.py`](istio-healthcheck.py) prüft eine Istio-Installation
clusterweit. Es braucht nur Python ab 3.9 und `oc` oder `kubectl`. `istioctl` und
pip-Pakete sind nicht nötig, das Skript läuft also auch unter Windows mit `oc.exe`.

Das Skript prüft:

- istiod-Deployments je Revision mit Version, Replicas und Restarts
- die neun Kern-CRDs von Istio
- Sidecar-Injector- und Validierungs-Webhooks, deren `caBundle` und Ziel-Service
- Gateway-Deployments, die externe Adresse von LoadBalancer-Services und ob zu
  jeder `Gateway`-Ressource ein passender Pod läuft
- die DaemonSets `istio-cni` und `ztunnel`. Fehlt `istio-cni` auf OpenShift,
  gibt es eine Warnung.
- die Laufzeit der Mesh-Root-CA, der Signing-CA aus `cacerts` oder
  `istio-ca-secret` und aller `credentialName`-Secrets der Gateways. Aus
  Secrets liest das Skript nur die Zertifikats-Keys, nie den privaten Schlüssel.
- STALE-Proxys, Proxys mit anderer Version als istiod und Sidecars, die nicht
  mit istiod verbunden sind
- Pods ohne Sidecar in Namespaces mit Injection, `istio.io/rev`-Labels ohne
  passende Revision, Deployments mit `ReplicaFailure` wie einer fehlenden
  `anyuid`-SCC auf OpenShift, dazu nicht bereite oder oft neu gestartete
  `istio-proxy`-Container

Exit-Code: `0` = OK, `1` = Warnungen, `2` = Fehler.

```bash
# Windows, oc.exe im PATH
python istio-healthcheck.py

# kubectl statt oc, anderer Context
python3 istio-healthcheck.py --cli kubectl --context my-cluster

# nur Probleme ausgeben, Zertifikatswarnung ab 60 Tagen
python3 istio-healthcheck.py --problems-only --cert-warn-days 60

# ohne clusterweite Leserechte: Pods/Deployments/Services/Gateways nur in diesen Namespaces
python3 istio-healthcheck.py -n istio-ingress -n my-app

# ohne pods/exec-Recht in istio-system
python3 istio-healthcheck.py --no-sync
```

Diese Befehle führt das Skript intern aus, `<cli>` steht für `oc` oder `kubectl`:

```bash
<cli> get namespace istio-system -o json
<cli> get namespaces -o json
<cli> get customresourcedefinitions -o name
<cli> get mutatingwebhookconfigurations -o json
<cli> get validatingwebhookconfigurations -o json
<cli> get daemonsets -A -o json
<cli> get deployments -A -o json            # mit -n: je Namespace
<cli> get pods -A -o json
<cli> get services -A -o json
<cli> get gateways.networking.istio.io -A -o json
<cli> get configmap istio-ca-root-cert -n istio-system --ignore-not-found -o 'jsonpath={.data.root-cert\.pem}'
<cli> get secret cacerts -n istio-system --ignore-not-found -o 'jsonpath={.data.ca-cert\.pem}'
<cli> get secret istio-ca-secret -n istio-system --ignore-not-found -o 'jsonpath={.data.ca-cert\.pem}'
<cli> get secret <credentialName> -n <gateway-pod-ns> --ignore-not-found \
      -o 'jsonpath={.data.tls\.crt}{"|"}{.data.cert}{"|"}{.data.ca\.crt}{"|"}{.data.cacert}'
<cli> get pods -n istio-system -l app=istiod -o json
<cli> exec -n istio-system <istiod-pod> -- pilot-discovery request GET /debug/syncz
```

## Voraussetzungen

- laufender Kubernetes-Cluster mit installiertem Istio
- `kubectl` mit gültigem Kontext auf diesen Cluster
  - Falls stattdessen nur `~/oc` (OpenShift-CLI) verfügbar ist:
    `source alias.sh` setzt `kubectl` als Alias auf `~/oc`
