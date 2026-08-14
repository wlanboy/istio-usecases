#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MANIFESTS_DIR="${SCRIPT_DIR}/manifests"
NAMESPACE="${1:-authz-policy-demo}"

render() {
  sed "s|\${NAMESPACE}|${NAMESPACE}|g" "$1"
}

if ! kubectl get namespace "${NAMESPACE}" >/dev/null 2>&1; then
  echo "==> Namespace '${NAMESPACE}' existiert nicht, nichts zu tun"
  exit 0
fi

echo "==> Entferne ggf. aktive incident.yaml (AuthorizationPolicy) aus Namespace '${NAMESPACE}'"
kubectl -n "${NAMESPACE}" delete authorizationpolicy block-client-b-incident --ignore-not-found

echo "==> Entferne Ressourcen dieses Usecases (Deployments, Services, ServiceAccounts, ConfigMap) aus Namespace '${NAMESPACE}'"
for f in $(ls -r "${MANIFESTS_DIR}"/*.yaml); do
  [[ "$(basename "${f}")" == "00-namespace.yaml" ]] && continue
  render "${f}" | kubectl delete -f - --ignore-not-found
done

echo "==> Fertig. Der Namespace '${NAMESPACE}' selbst wurde NICHT gelöscht"
echo "    (könnte auch von anderen Usecases mitgenutzt werden)."
echo "    Zum vollständigen Entfernen: kubectl delete namespace ${NAMESPACE}"
