#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_URL="http://service.service.svc.cluster.local/healthz"

echo "==> Aktiviere Wartungsmodus für 'service' (VirtualServices aus manifestwartung/)"
kubectl apply -f "${SCRIPT_DIR}/manifestwartung/"

echo "==> Warte, bis istiod die Routen an den Client-Sidecar verteilt hat (max. 30s)"
for _ in $(seq 1 30); do
  status=$(curl -s http://client.localhost/api/request \
    -H 'content-type: application/json' \
    -d "{\"url\":\"${SERVICE_URL}\"}" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["status_code"])')
  if [[ "${status}" == "503" ]]; then
    echo "==> Wartungsmodus ist aktiv: client -> service liefert 503."
    exit 0
  fi
  sleep 1
done

echo "==> Client bekommt nach 30s noch HTTP ${status} statt 503, siehe Troubleshooting in readme.md" >&2
exit 1
