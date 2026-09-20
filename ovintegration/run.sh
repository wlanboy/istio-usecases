#!/usr/bin/env bash
set -euo pipefail

NAMESPACE="${1:-ovintegration-demo}"
ROUTE_NAME="ovintegration-${NAMESPACE}"

test_path() {
  local label="$1" url="$2"
  echo "==> [${label}] Rufe ${url} auf"

  local response status server_header
  response="$(curl -sk -D - -o "/tmp/ovintegration-body.$$" "${url}")"
  status="$(printf '%s' "${response}" | head -n1 | awk '{print $2}')"
  server_header="$(printf '%s' "${response}" | grep -i '^server:' || true)"

  echo "    Status: ${status:-FAIL}   ${server_header:-<kein server-Header>}"
  cat "/tmp/ovintegration-body.$$"
  rm -f "/tmp/ovintegration-body.$$"
  echo

  if [[ "${status}" == "200" ]] && printf '%s' "${server_header}" | grep -qi 'istio-envoy'; then
    echo "    OK: Status 200 und Server-Header 'istio-envoy' beweisen, dass die Antwort tatsaechlich"
    echo "    durch den Istio Ingress Gateway (Envoy) gelaufen ist, nicht direkt von nginx."
  else
    echo "    Unerwartetes Ergebnis. Siehe Troubleshooting in readme.md."
  fi
}

ROUTE_HOST="$(oc -n istio-system get route "${ROUTE_NAME}" -o jsonpath='{.spec.host}' 2>/dev/null || true)"
if [[ -z "${ROUTE_HOST}" ]]; then
  echo "Route '${ROUTE_NAME}' in Namespace istio-system nicht gefunden. Erst ./install.sh ${NAMESPACE} ausfuehren." >&2
  exit 1
fi

echo "=== Weg A: eigene Route (istio-system/${ROUTE_NAME}), TLS-Terminierung am Router (edge) ==="
test_path "Weg A" "https://${ROUTE_HOST}/"
echo

if oc -n istio-system get route istio-ingressgateway-wildcard >/dev/null 2>&1; then
  APPS_DOMAIN="$(oc get ingresscontroller/default -n openshift-ingress-operator -o jsonpath='{.status.domain}')"
  WILDCARD_TEST_HOST="${NAMESPACE}-passthrough.${APPS_DOMAIN}"

  echo "=== Weg B: Wildcard-Route (istio-system/istio-ingressgateway-wildcard), TLS-Terminierung"
  echo "    am Envoy (passthrough) ==="
  echo "    Testhost '${WILDCARD_TEST_HOST}' hat bewusst KEINE eigene OpenShift-Route, damit die"
  echo "    Anfrage nicht wieder von der spezifischeren Route aus Weg A abgefangen wird, sondern"
  echo "    tatsaechlich ueber die Wildcard-Route (wildcardPolicy: Subdomain) laeuft."
  test_path "Weg B" "https://${WILDCARD_TEST_HOST}/"
else
  echo "=== Weg B (TLS am Envoy) uebersprungen ==="
  echo "    Route 'istio-ingressgateway-wildcard' nicht gefunden. Zum Aktivieren:"
  echo "    ./install-istio-passthrough.sh"
fi
