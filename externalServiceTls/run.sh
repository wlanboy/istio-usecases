#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
POD_TEMPLATE="${SCRIPT_DIR}/test/client-pod.yaml"

NAMESPACE="${1:-external-tls-demo}"
TARGET="http://api.github.com/"
POD_NAME="tls-origination-test-$(date +%s)"

echo "==> Starte Test-Client-Pod '${POD_NAME}' gegen ${TARGET}"

sed \
  -e "s|\${POD_NAME}|${POD_NAME}|g" \
  -e "s|\${NAMESPACE}|${NAMESPACE}|g" \
  -e "s|\${TARGET}|${TARGET}|g" \
  "${POD_TEMPLATE}" | kubectl apply -f -

echo "==> Warte auf Abschluss..."
kubectl -n "${NAMESPACE}" wait --for=jsonpath='{.status.phase}'=Succeeded pod/"${POD_NAME}" --timeout=90s

echo "==> Ergebnis:"
kubectl -n "${NAMESPACE}" logs "${POD_NAME}" -c client

echo "==> Räume Test-Client-Pod auf"
kubectl -n "${NAMESPACE}" delete pod "${POD_NAME}" --ignore-not-found

echo
echo "==> Erwartung: HTTP-Status 200 mit echtem JSON-Body (Feld 'current_user_url')."
echo "    Ein HTTP-Status 301 würde bedeuten, dass die Anfrage unverändert im"
echo "    Klartext auf Port 80 bei GitHub ankam (keine TLS-Origination durch Istio)."
