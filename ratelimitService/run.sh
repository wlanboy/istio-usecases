#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
POD_TEMPLATE="${SCRIPT_DIR}/test/loadtest-pod.yaml"

NAMESPACE="${1:-ratelimit-service-demo}"
REQUESTS="${2:-30}"
POD_NAME="loadtest-$(date +%s)"
TARGET="http://nginx.${NAMESPACE}.svc.cluster.local"

echo "==> Starting load-test pod '${POD_NAME}' against ${TARGET} (${REQUESTS} requests)"

sed \
  -e "s|\${POD_NAME}|${POD_NAME}|g" \
  -e "s|\${NAMESPACE}|${NAMESPACE}|g" \
  -e "s|\${TARGET}|${TARGET}|g" \
  -e "s|\${REQUESTS}|${REQUESTS}|g" \
  "${POD_TEMPLATE}" | kubectl apply -f -

echo "==> Waiting for load-test to finish..."
kubectl -n "${NAMESPACE}" wait --for=jsonpath='{.status.phase}'=Succeeded pod/"${POD_NAME}" --timeout=90s

echo "==> Results:"
kubectl -n "${NAMESPACE}" logs "${POD_NAME}"

echo
echo "==> Summary of HTTP status codes:"
kubectl -n "${NAMESPACE}" logs "${POD_NAME}" | grep -E '^HTTP/' | awk '{print $2}' | sort | uniq -c | sort -rn

echo "==> Cleaning up load-test pod"
kubectl -n "${NAMESPACE}" delete pod "${POD_NAME}" --ignore-not-found
