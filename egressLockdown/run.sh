#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
POD_TEMPLATE="${SCRIPT_DIR}/test/client-pod.yaml"

NAMESPACE="${1:-egress-lockdown-demo}"
INTERNAL_TARGET="http://nginx.${NAMESPACE}.svc.cluster.local/"
EXTERNAL_TARGET="http://github.com/"
ALLOWED_EXTERNAL_TARGET="https://git.gmk.lan:3300/"
POD_NAME="egress-lockdown-test-$(date +%s)"

echo "==> Starte Test-Client-Pod '${POD_NAME}'"
echo "    intern:            ${INTERNAL_TARGET}"
echo "    extern (blockiert):${EXTERNAL_TARGET}"
echo "    extern (erlaubt):  ${ALLOWED_EXTERNAL_TARGET}"

sed \
  -e "s|\${POD_NAME}|${POD_NAME}|g" \
  -e "s|\${NAMESPACE}|${NAMESPACE}|g" \
  -e "s|\${INTERNAL_TARGET}|${INTERNAL_TARGET}|g" \
  -e "s|\${EXTERNAL_TARGET}|${EXTERNAL_TARGET}|g" \
  -e "s|\${ALLOWED_EXTERNAL_TARGET}|${ALLOWED_EXTERNAL_TARGET}|g" \
  "${POD_TEMPLATE}" | kubectl apply -f -

echo "==> Warte auf Abschluss..."
kubectl -n "${NAMESPACE}" wait --for=jsonpath='{.status.phase}'=Succeeded pod/"${POD_NAME}" --timeout=90s

echo "==> Ergebnis:"
kubectl -n "${NAMESPACE}" logs "${POD_NAME}" -c client

echo "==> Räume Test-Client-Pod auf"
kubectl -n "${NAMESPACE}" delete pod "${POD_NAME}" --ignore-not-found

echo
echo "==> Erwartung: interner Aufruf und git.gmk.lan liefern einen HTTP-Status,"
echo "    github.com schlägt fehl (FAIL/000 bzw. Connection reset) - der"
echo "    Sidecar lässt nur Traffic zu Zielen durch, die Istio in seiner"
echo "    Service-Registry kennt (In-Cluster-Services + per ServiceEntry"
echo "    freigegebene externe Hosts wie git.gmk.lan)."
