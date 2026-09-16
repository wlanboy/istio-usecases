#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MANIFESTS_DIR="${SCRIPT_DIR}/manifests"
NAMESPACE="${1:-ovintegration-demo}"

render() {
  sed "s|\${NAMESPACE}|${NAMESPACE}|g" "$1"
}

if ! oc get namespace "${NAMESPACE}" >/dev/null 2>&1; then
  echo "==> Namespace '${NAMESPACE}' existiert nicht, nichts zu tun"
  exit 0
fi

echo "==> Entferne Route 'ovintegration-${NAMESPACE}' aus Namespace istio-system"
oc -n istio-system delete route "ovintegration-${NAMESPACE}" --ignore-not-found

echo "==> Entferne Ressourcen dieses Usecases (Gateway, VirtualService, nginx-Deployment/-Service/-ConfigMap) aus Namespace '${NAMESPACE}'"
for f in $(ls -r "${MANIFESTS_DIR}"/*.yaml); do
  [[ "$(basename "${f}")" == "00-namespace.yaml" ]] && continue
  [[ "$(basename "${f}")" == "22-route.yaml" ]] && continue
  render "${f}" | oc delete -f - --ignore-not-found
done

echo "==> Fertig. Der Namespace '${NAMESPACE}' selbst wurde NICHT geloescht."
echo "    Zum vollstaendigen Entfernen: oc delete namespace ${NAMESPACE}"
