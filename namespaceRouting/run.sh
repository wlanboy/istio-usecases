#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
POD_TEMPLATE="${SCRIPT_DIR}/test/client-pod.yaml"

NAMESPACE="${1:-namespace-routing-demo}"
EXTERN_NS="${NAMESPACE}-extern"
INTERN_NS="${NAMESPACE}-intern"
TARGET="http://backend.${NAMESPACE}.svc.cluster.local"

run_client() {
  local client_ns="$1"
  local pod_name="routing-test-$(date +%s)-${RANDOM}"

  echo
  echo "==> Starte Test-Client-Pod '${pod_name}' in Namespace '${client_ns}' gegen ${TARGET}"

  sed \
    -e "s|\${POD_NAME}|${pod_name}|g" \
    -e "s|\${NAMESPACE}|${client_ns}|g" \
    -e "s|\${TARGET}|${TARGET}|g" \
    "${POD_TEMPLATE}" | kubectl apply -f -

  echo "==> Warte auf Abschluss..."
  kubectl -n "${client_ns}" wait --for=jsonpath='{.status.phase}'=Succeeded pod/"${pod_name}" --timeout=90s

  echo "==> Ergebnis:"
  kubectl -n "${client_ns}" logs "${pod_name}" -c client

  echo "==> Räume Test-Client-Pod auf"
  kubectl -n "${client_ns}" delete pod "${pod_name}" --ignore-not-found
}

run_client "${EXTERN_NS}"
run_client "${INTERN_NS}"

echo
echo "==> Erwartung: Der Client aus '${EXTERN_NS}' bekommt die Antwort des EXTERN-Pods,"
echo "    der Client aus '${INTERN_NS}' die Antwort des INTERN-Pods."
