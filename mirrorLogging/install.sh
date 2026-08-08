#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MANIFESTS_DIR="${SCRIPT_DIR}/manifests"
NAMESPACE="${1:-mirror-logging-demo}"

render() {
  sed "s|\${NAMESPACE}|${NAMESPACE}|g" "$1"
}

if kubectl get namespace "${NAMESPACE}" >/dev/null 2>&1; then
  echo "==> Namespace '${NAMESPACE}' existiert bereits, Erstellung wird übersprungen"
else
  echo "==> Erstelle Namespace '${NAMESPACE}'"
  render "${MANIFESTS_DIR}/00-namespace.yaml" | kubectl apply -f -
fi

echo "==> Wende restliche Manifeste an (nginx + logger Deployment/Service) in Namespace '${NAMESPACE}'"
for f in "${MANIFESTS_DIR}"/*.yaml; do
  case "$(basename "${f}")" in
    00-namespace.yaml) continue ;;
    # Das Mirroring selbst wird bewusst NICHT hier aktiviert, sondern erst von
    # run.sh - so lässt sich vor/nach dem Aktivieren der Mirror-Regel
    # vergleichen (nginx ist bereits vorher normal per curl erreichbar, nur
    # ohne dass irgendetwas gespiegelt wird).
    20-virtualservice-mirror.yaml) continue ;;
  esac
  render "${f}" | kubectl apply -f -
done

echo "==> Warte auf nginx Deployment"
kubectl -n "${NAMESPACE}" rollout status deployment/nginx --timeout=120s
echo "==> Warte auf logger Deployment"
kubectl -n "${NAMESPACE}" rollout status deployment/logger --timeout=120s

echo "==> Fertig. Ressourcen in Namespace '${NAMESPACE}' (Mirroring noch NICHT aktiv, siehe run.sh):"
kubectl -n "${NAMESPACE}" get deploy,svc,virtualservice
