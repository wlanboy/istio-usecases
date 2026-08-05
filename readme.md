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

## Voraussetzungen

- laufender Kubernetes-Cluster mit installiertem Istio
- `kubectl` mit gültigem Kontext auf diesen Cluster
