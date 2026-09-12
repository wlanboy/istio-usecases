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

## Voraussetzungen

- laufender Kubernetes-Cluster mit installiertem Istio
- `kubectl` mit gültigem Kontext auf diesen Cluster
  - Falls stattdessen nur `~/oc` (OpenShift-CLI) verfügbar ist:
    `source alias.sh` setzt `kubectl` als Alias auf `~/oc`
