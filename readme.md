# Istio Usecases

Sammlung praktischer Istio-Beispiele, jeweils als eigenständiges Verzeichnis mit
Manifesten, Install-/Test-Skripten und eigener `readme.md`.

## Usecases

- [`ratelimits/`](ratelimits/readme.md) — Istio Local Rate Limiting am Beispiel eines
  `nginx`-Deployments, abgesichert durch einen `EnvoyFilter` mit Token-Bucket-Limit,
  inklusive Lasttest-Pod zur Verifikation.
- [`ratelimitService/`](ratelimitService/readme.md) — Istio Global Rate Limiting: ein über
  alle nginx-Replicas gemeinsam geltendes Limit, durchgesetzt via externem
  Envoy-RateLimit-Service (Redis-Backend) statt per-Sidecar-Token-Bucket.
- [`namespaceRouting/`](namespaceRouting/readme.md) — Istio Traffic Routing anhand des
  Source Namespace: Clients aus zwei unterschiedlichen Namespaces (`extern`/`intern`)
  rufen denselben Service auf, werden per `VirtualService`/`DestinationRule` aber auf
  unterschiedliche Backend-Pods geroutet.
- [`faultInjection/`](faultInjection/readme.md) — Istio HTTP Fault Injection: ein
  `VirtualService` injiziert per `x-fault`-Header gezielt Delay (5s) oder Abort
  (HTTP 500) in Requests an ein `nginx`-Deployment, ohne den Service selbst zu ändern.
- [`externalServiceTls/`](externalServiceTls/readme.md) — Istio TLS Origination für
  Egress-Traffic: `ServiceEntry`, `DestinationRule` (`tls.mode: SIMPLE`) und
  `VirtualService` sorgen dafür, dass der Envoy-Sidecar die TLS-Verbindung zu einem
  externen Dienst (`api.github.com`) selbst aufbaut, während der App-Container nur
  Klartext-HTTP spricht.
- [`mirrorLogging/`](mirrorLogging/readme.md) — Istio Traffic Mirroring: ein
  `VirtualService` spiegelt jeden Request an `nginx` zusätzlich (fire-and-forget)
  an einen `logger`-Pod, der Methode, Header und Body protokolliert und dabei den
  `Authorization`-Wert (OAuth-Token) durch `*` ersetzt — der Client bekommt nur die
  Antwort von `nginx` zu sehen.

## Voraussetzungen

- laufender Kubernetes-Cluster mit installiertem Istio
- `kubectl` mit gültigem Kontext auf diesen Cluster
  - Falls stattdessen nur `~/oc` (OpenShift-CLI) verfügbar ist:
    `source alias.sh` setzt `kubectl` als Alias auf `~/oc`
