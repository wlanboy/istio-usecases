#!/usr/bin/env bash
set -euo pipefail

NAMESPACE="${1:-authz-policy-demo}"
TARGET="http://backend.${NAMESPACE}.svc.cluster.local"

call() {
  local client="$1"
  local pod
  pod="$(kubectl -n "${NAMESPACE}" get pod -l app="${client}" -o jsonpath='{.items[0].metadata.name}')"

  echo
  echo "==> Request von '${client}' (ServiceAccount '${client}', Pod ${pod}) an ${TARGET}"
  kubectl -n "${NAMESPACE}" exec "${pod}" -c client -- \
    curl -s -o /dev/null -w 'HTTP-Status: %{http_code}\n' "${TARGET}"
}

call client-a
call client-b

echo
if kubectl -n "${NAMESPACE}" get authorizationpolicy block-client-b-incident >/dev/null 2>&1; then
  echo "==> incident.yaml ist aktiv: 'client-a' erwartet 200, 'client-b' erwartet 403."
else
  echo "==> incident.yaml ist NICHT aktiv: beide Clients erwarten 200."
fi
