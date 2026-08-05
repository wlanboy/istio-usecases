# Istio Usecases

Sammlung praktischer Istio-Beispiele, jeweils als eigenständiges Verzeichnis mit
Manifesten, Install-/Test-Skripten und eigener `readme.md`.

## Usecases

- [`ratelimits/`](ratelimits/readme.md) — Istio Local Rate Limiting am Beispiel eines
  `nginx`-Deployments, abgesichert durch einen `EnvoyFilter` mit Token-Bucket-Limit,
  inklusive Lasttest-Pod zur Verifikation.

## Voraussetzungen

- laufender Kubernetes-Cluster mit installiertem Istio
- `kubectl` mit gültigem Kontext auf diesen Cluster
