#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MANIFESTS_DIR="${SCRIPT_DIR}/manifests"
POD_TEMPLATE="${SCRIPT_DIR}/test/client-pod.yaml"

NAMESPACE="${1:-mirror-logging-demo}"
TARGET="http://nginx.${NAMESPACE}.svc.cluster.local"
POD_NAME="mirror-test-client-$(date +%s)"

echo "==> Aktiviere Mirroring (wendet Mirror-VirtualService an, den install.sh bewusst ausgelassen hat)"
sed "s|\${NAMESPACE}|${NAMESPACE}|g" "${MANIFESTS_DIR}/20-virtualservice-mirror.yaml" | kubectl apply -f -

echo "==> Warte kurz, bis die Mirror-Regel per xDS bei allen Sidecars ankommt..."
sleep 3

echo
echo "==> Starte Test-Client-Pod '${POD_NAME}' gegen ${TARGET}"
sed \
  -e "s|\${POD_NAME}|${POD_NAME}|g" \
  -e "s|\${NAMESPACE}|${NAMESPACE}|g" \
  -e "s|\${TARGET}|${TARGET}|g" \
  "${POD_TEMPLATE}" | kubectl apply -f -

kubectl -n "${NAMESPACE}" wait --for=jsonpath='{.status.phase}'=Succeeded pod/"${POD_NAME}" --timeout=90s

echo
echo "==> Antwort des Clients (kommt ausschließlich von PRIMARY nginx, der Mirror beeinflusst sie nicht):"
kubectl -n "${NAMESPACE}" logs "${POD_NAME}" -c client

echo "==> Räume Test-Client-Pod auf"
kubectl -n "${NAMESPACE}" delete pod "${POD_NAME}" --ignore-not-found

echo
echo "==> Warte kurz, bis die gespiegelte Anfrage asynchron beim logger-Pod ankommt (fire-and-forget)..."
sleep 3

LOGGER_POD="$(kubectl -n "${NAMESPACE}" get pod -l app=logger -o jsonpath='{.items[0].metadata.name}')"
echo "==> Logs des logger-Pods '${LOGGER_POD}' (gespiegelte Anfrage, Authorization-Header redigiert):"
kubectl -n "${NAMESPACE}" logs "${LOGGER_POD}" --since=15s
