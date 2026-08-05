#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MANIFESTS_DIR="${SCRIPT_DIR}/manifests"
NAMESPACE="${1:-namespace-routing-demo}"
EXTERN_NS="${NAMESPACE}-extern"
INTERN_NS="${NAMESPACE}-intern"

render() {
  sed \
    -e "s|\${NAMESPACE}|${NAMESPACE}|g" \
    -e "s|\${EXTERN_NS}|${EXTERN_NS}|g" \
    -e "s|\${INTERN_NS}|${INTERN_NS}|g" \
    "$1"
}

ensure_namespace() {
  local ns="$1" file="$2"
  if kubectl get namespace "${ns}" >/dev/null 2>&1; then
    echo "==> Namespace '${ns}' existiert bereits, Erstellung wird übersprungen"
  else
    echo "==> Erstelle Namespace '${ns}'"
    render "${file}" | kubectl apply -f -
  fi
}

ensure_namespace "${NAMESPACE}" "${MANIFESTS_DIR}/00-namespace.yaml"
ensure_namespace "${EXTERN_NS}" "${MANIFESTS_DIR}/01-namespace-extern.yaml"
ensure_namespace "${INTERN_NS}" "${MANIFESTS_DIR}/02-namespace-intern.yaml"

echo "==> Wende restliche Manifeste an (ConfigMaps, Backend-Deployments, Service, DestinationRule, VirtualService) in Namespace '${NAMESPACE}'"
for f in "${MANIFESTS_DIR}"/*.yaml; do
  case "$(basename "${f}")" in
    00-namespace.yaml|01-namespace-extern.yaml|02-namespace-intern.yaml) continue ;;
  esac
  render "${f}" | kubectl apply -f -
done

echo "==> Warte auf Backend-Deployments"
kubectl -n "${NAMESPACE}" rollout status deployment/backend-extern --timeout=120s
kubectl -n "${NAMESPACE}" rollout status deployment/backend-intern --timeout=120s

echo "==> Fertig. Ressourcen in Namespace '${NAMESPACE}':"
kubectl -n "${NAMESPACE}" get deploy,svc,destinationrule,virtualservice

echo
echo "==> Client-Namespaces (istio-injection enabled, für Test-Requests):"
kubectl get namespace "${EXTERN_NS}" "${INTERN_NS}" --show-labels
