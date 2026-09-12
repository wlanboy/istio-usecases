#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
POD_TEMPLATE="${SCRIPT_DIR}/test/curl-pod.yaml"

NAMESPACE="${1:-xff-demo}"
XFF_VALUE="${2:-203.0.113.42}"
POD_NAME="xff-curltest-$(date +%s)"
TARGET="http://xff-demo-gateway.${NAMESPACE}.svc.cluster.local/"

echo "==> Sende Request über das Gateway mit vorgetäuschtem 'X-Forwarded-For: ${XFF_VALUE}'"

sed \
  -e "s|\${POD_NAME}|${POD_NAME}|g" \
  -e "s|\${NAMESPACE}|${NAMESPACE}|g" \
  -e "s|\${TARGET}|${TARGET}|g" \
  -e "s|\${XFF_VALUE}|${XFF_VALUE}|g" \
  "${POD_TEMPLATE}" | kubectl apply -f -

echo "==> Warte auf Testergebnis..."
kubectl -n "${NAMESPACE}" wait --for=jsonpath='{.status.phase}'=Succeeded pod/"${POD_NAME}" --timeout=60s

echo "==> Antwort des echo-Backends (gesehen NACH Durchlauf durch das Gateway):"
kubectl -n "${NAMESPACE}" logs "${POD_NAME}"

echo "==> Aktuelle numTrustedProxies-Konfiguration des Gateways:"
kubectl -n "${NAMESPACE}" get deployment xff-demo-gateway \
  -o jsonpath='{.spec.template.metadata.annotations.proxy\.istio\.io/config}' | grep -A1 gatewayTopology \
  || echo "(keine proxy.istio.io/config-Annotation gesetzt -> Default: numTrustedProxies=0)"

echo "==> Räume Test-Pod auf"
kubectl -n "${NAMESPACE}" delete pod "${POD_NAME}" --ignore-not-found
