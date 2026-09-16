#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MANIFESTS_DIR="${SCRIPT_DIR}/manifests"
NAMESPACE="${1:-ovintegration-demo}"

render() {
  sed "s|\${NAMESPACE}|${NAMESPACE}|g" "$1"
}

if oc get namespace "${NAMESPACE}" >/dev/null 2>&1; then
  echo "==> Namespace '${NAMESPACE}' existiert bereits, Erstellung wird uebersprungen"
else
  echo "==> Erstelle Namespace '${NAMESPACE}'"
  render "${MANIFESTS_DIR}/00-namespace.yaml" | oc apply -f -
fi

echo "==> anyuid-SCC fuer ServiceAccounts in '${NAMESPACE}' (sidecar-injizierte Pods laufen mit UID 1337)"
oc adm policy add-scc-to-group anyuid "system:serviceaccounts:${NAMESPACE}"

echo "==> Wende restliche Manifeste an (nginx-Testziel, Gateway, VirtualService) in Namespace '${NAMESPACE}'"
for f in "${MANIFESTS_DIR}"/*.yaml; do
  name="$(basename "${f}")"
  [[ "${name}" == "00-namespace.yaml" ]] && continue
  render "${f}" | oc apply -f -
done

echo "==> Warte auf nginx-Deployment"
oc -n "${NAMESPACE}" rollout status deployment/nginx --timeout=90s

ROUTE_NAME="ovintegration-${NAMESPACE}"
echo "==> Warte auf Route-Hostname"
for i in $(seq 1 30); do
  ROUTE_HOST="$(oc -n istio-system get route "${ROUTE_NAME}" -o jsonpath='{.spec.host}' 2>/dev/null || true)"
  [[ -n "${ROUTE_HOST}" ]] && break
  sleep 1
done

echo "==> Fertig. Ressourcen in Namespace '${NAMESPACE}':"
oc -n "${NAMESPACE}" get deployment,service,gateway,virtualservice
echo
echo "==> Route: ${ROUTE_NAME} (Namespace istio-system)"
echo "    Host:  ${ROUTE_HOST:-<noch nicht vergeben, siehe: oc -n istio-system get route ${ROUTE_NAME}>}"
echo "    Test:  ./run.sh ${NAMESPACE}"
