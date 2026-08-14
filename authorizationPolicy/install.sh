#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MANIFESTS_DIR="${SCRIPT_DIR}/manifests"
NAMESPACE="${1:-authz-policy-demo}"

render() {
  sed "s|\${NAMESPACE}|${NAMESPACE}|g" "$1"
}

if kubectl get namespace "${NAMESPACE}" >/dev/null 2>&1; then
  echo "==> Namespace '${NAMESPACE}' existiert bereits, Erstellung wird übersprungen"
else
  echo "==> Erstelle Namespace '${NAMESPACE}'"
  render "${MANIFESTS_DIR}/00-namespace.yaml" | kubectl apply -f -
fi

echo "==> Wende restliche Manifeste an (backend + client-a + client-b, je mit eigenem ServiceAccount) in Namespace '${NAMESPACE}'"
for f in "${MANIFESTS_DIR}"/*.yaml; do
  [[ "$(basename "${f}")" == "00-namespace.yaml" ]] && continue
  render "${f}" | kubectl apply -f -
done

echo "==> Warte auf Deployments"
kubectl -n "${NAMESPACE}" rollout status deployment/backend --timeout=120s
kubectl -n "${NAMESPACE}" rollout status deployment/client-a --timeout=120s
kubectl -n "${NAMESPACE}" rollout status deployment/client-b --timeout=120s

echo "==> Fertig. Ressourcen in Namespace '${NAMESPACE}':"
kubectl -n "${NAMESPACE}" get deploy,svc,serviceaccount

echo
echo "==> Noch KEINE AuthorizationPolicy aktiv - beide Clients duerfen aktuell auf 'backend' zugreifen."
echo "    Vorfall simulieren: kubectl apply -f incident.yaml"
