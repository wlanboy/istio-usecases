#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
POD_TEMPLATE="${SCRIPT_DIR}/test/client-pod.yaml"

NAMESPACE="${1:-fault-injection-demo}"
TARGET="http://nginx.${NAMESPACE}.svc.cluster.local"

run_client() {
  local fault_header="$1"
  local pod_name="fault-test-${fault_header}-$(date +%s)"

  echo
  echo "==> Starte Test-Client-Pod '${pod_name}' gegen ${TARGET} (x-fault: ${fault_header})"

  sed \
    -e "s|\${POD_NAME}|${pod_name}|g" \
    -e "s|\${NAMESPACE}|${NAMESPACE}|g" \
    -e "s|\${TARGET}|${TARGET}|g" \
    -e "s|\${FAULT_HEADER}|${fault_header}|g" \
    "${POD_TEMPLATE}" | kubectl apply -f -

  echo "==> Warte auf Abschluss (bei 'delay' bis zu ~10s wegen injiziertem Fixed-Delay von 5s)..."
  kubectl -n "${NAMESPACE}" wait --for=jsonpath='{.status.phase}'=Succeeded pod/"${pod_name}" --timeout=90s

  echo "==> Ergebnis:"
  kubectl -n "${NAMESPACE}" logs "${pod_name}" -c client

  echo "==> Räume Test-Client-Pod auf"
  kubectl -n "${NAMESPACE}" delete pod "${pod_name}" --ignore-not-found
}

run_client "none"
run_client "delay"
run_client "abort"

echo
echo "==> Erwartung: 'none' liefert HTTP 200 ohne nennenswerte Verzögerung,"
echo "    'delay' liefert HTTP 200 nach ca. 5s Delay, 'abort' liefert sofort HTTP 500."
