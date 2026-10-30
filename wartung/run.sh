#!/usr/bin/env bash
set -euo pipefail

SERVICE_URL="http://service.service.svc.cluster.local/healthz"

# Lässt den Client-Pod (http-tester) über seine eigene API /api/request den
# Service aufrufen und gibt Status, Retry-After und Body der Antwort aus.
call_via_client() {
  local headers="$1"
  curl -s http://client.localhost/api/request \
    -H 'content-type: application/json' \
    -d "{\"url\":\"${SERVICE_URL}\",\"headers\":\"${headers}\"}" \
  | python3 -c 'import json,sys; r=json.load(sys.stdin); print("HTTP-Status:", r["status_code"], "| Retry-After:", r["headers"].get("retry-after", "-"), "| Body:", r["response"])'
}

call_via_ingress() {
  curl -s -o /dev/null -w 'HTTP-Status: %{http_code} | Retry-After: %header{retry-after}\n' "$@" http://service.localhost/healthz
}

echo "==> client -> service (mesh-intern, ${SERVICE_URL})"
call_via_client ""

echo "==> client -> service mit Header 'x-wartung-bypass: true'"
call_via_client "x-wartung-bypass: true"

echo "==> Browser/curl -> http://service.localhost/healthz (Ingress-Gateway)"
call_via_ingress

echo "==> Browser/curl -> http://service.localhost/healthz mit Header 'x-wartung-bypass: true'"
call_via_ingress -H 'x-wartung-bypass: true'

echo
if kubectl -n service get virtualservice service-wartung >/dev/null 2>&1; then
  echo "==> Wartungsmodus ist AKTIV: ohne Bypass-Header 503 + Retry-After: 300, mit Bypass-Header 200."
else
  echo "==> Wartungsmodus ist NICHT aktiv: alle Aufrufe erwarten 200."
fi
