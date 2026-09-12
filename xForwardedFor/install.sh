#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MANIFESTS_DIR="${SCRIPT_DIR}/manifests"
NAMESPACE="${1:-xff-demo}"

render() {
  sed "s|\${NAMESPACE}|${NAMESPACE}|g" "$1"
}

if kubectl get namespace "${NAMESPACE}" >/dev/null 2>&1; then
  echo "==> Namespace '${NAMESPACE}' existiert bereits, Erstellung wird übersprungen"
else
  echo "==> Erstelle Namespace '${NAMESPACE}'"
  render "${MANIFESTS_DIR}/00-namespace.yaml" | kubectl apply -f -
fi

echo "==> Wende restliche Manifeste an (eigenes Gateway per Gateway-Injection, echo-Backend) in Namespace '${NAMESPACE}'"
for f in "${MANIFESTS_DIR}"/*.yaml; do
  name="$(basename "${f}")"
  [[ "${name}" == "00-namespace.yaml" ]] && continue
  # 40-numtrustedproxies-patch.yaml ist kein eigenständiges Manifest, sondern eine
  # strategic-merge-Patch-Vorlage für set-numtrustedproxies.sh - wird hier übersprungen
  [[ "${name}" == "40-numtrustedproxies-patch.yaml" ]] && continue
  render "${f}" | kubectl apply -f -
done

echo "==> Warte auf Gateway- und Backend-Deployment"
kubectl -n "${NAMESPACE}" rollout status deployment/xff-demo-gateway --timeout=120s
kubectl -n "${NAMESPACE}" rollout status deployment/echo --timeout=120s

echo "==> Fertig. Ressourcen in Namespace '${NAMESPACE}':"
kubectl -n "${NAMESPACE}" get deploy,svc,gateway,virtualservice
