#!/usr/bin/env bash
set -euo pipefail

NAMESPACE="${1:-ovintegration-demo}"
ROUTE_NAME="ovintegration-${NAMESPACE}"

ROUTE_HOST="$(oc -n istio-system get route "${ROUTE_NAME}" -o jsonpath='{.spec.host}' 2>/dev/null || true)"
if [[ -z "${ROUTE_HOST}" ]]; then
  echo "Route '${ROUTE_NAME}' in Namespace istio-system nicht gefunden. Erst ./install.sh ${NAMESPACE} ausfuehren." >&2
  exit 1
fi

URL="https://${ROUTE_HOST}/"
echo "==> Rufe ${URL} auf (Weg: OpenShift Router -> Route -> Service istio-ingressgateway -> Istio Gateway -> VirtualService -> nginx)"

RESPONSE="$(curl -sk -D - -o /tmp/ovintegration-body.$$ "${URL}")"
STATUS="$(printf '%s' "${RESPONSE}" | head -n1 | awk '{print $2}')"
SERVER_HEADER="$(printf '%s' "${RESPONSE}" | grep -i '^server:' || true)"

echo
echo "=== Antwort ==="
echo "Status: ${STATUS:-FAIL}"
echo "${SERVER_HEADER:-<kein server-Header>}"
echo
cat /tmp/ovintegration-body.$$
rm -f /tmp/ovintegration-body.$$

echo
if [[ "${STATUS}" == "200" ]] && printf '%s' "${SERVER_HEADER}" | grep -qi 'istio-envoy'; then
  echo "==> Erfolgreich: Status 200 und Server-Header 'istio-envoy' beweisen, dass die Antwort"
  echo "    tatsaechlich durch den Istio Ingress Gateway (Envoy) gelaufen ist, nicht direkt von nginx."
else
  echo "==> Unerwartetes Ergebnis. Siehe Troubleshooting in readme.md."
fi
