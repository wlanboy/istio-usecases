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

if ! kubectl get namespace "${NAMESPACE}" >/dev/null 2>&1; then
  echo "==> Namespace '${NAMESPACE}' existiert nicht, nichts zu tun"
  exit 0
fi

echo "==> Entferne Ressourcen dieses Usecases (VirtualService, DestinationRule, Deployments, Service, ConfigMaps) aus Namespace '${NAMESPACE}'"
for f in $(ls -r "${MANIFESTS_DIR}"/*.yaml); do
  case "$(basename "${f}")" in
    00-namespace.yaml|01-namespace-extern.yaml|02-namespace-intern.yaml) continue ;;
  esac
  render "${f}" | kubectl delete -f - --ignore-not-found
done

echo "==> Fertig. Die Namespaces '${NAMESPACE}', '${EXTERN_NS}' und '${INTERN_NS}' selbst wurden NICHT gelöscht"
echo "    (Client-Namespaces enthalten sonst keine dauerhaften Ressourcen mehr,"
echo "    Test-Pods werden bereits von run.sh wieder aufgeräumt)."
echo "    Zum vollständigen Entfernen:"
echo "    kubectl delete namespace ${NAMESPACE} ${EXTERN_NS} ${INTERN_NS}"
