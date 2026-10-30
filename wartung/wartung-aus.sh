#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_URL="http://service.service.svc.cluster.local/healthz"

echo "==> Beende Wartungsmodus für 'service' (entferne VirtualServices aus manifestwartung/)"
kubectl delete -f "${SCRIPT_DIR}/manifestwartung/" --ignore-not-found

echo "==> Warte, bis istiod die Routen an den Client-Sidecar verteilt hat (max. 30s)"
for _ in $(seq 1 30); do
  status=$(curl -s http://client.localhost/api/request \
    -H 'content-type: application/json' \
    -d "{\"url\":\"${SERVICE_URL}\"}" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["status_code"])')
  if [[ "${status}" == "200" ]]; then
    break
  fi
  sleep 1
done

if [[ "${status}" != "200" ]]; then
  echo "==> Client bekommt nach 30s noch HTTP ${status} statt 200, siehe Troubleshooting in readme.md" >&2
  echo "==> CronJobs mit Label wartung=service bleiben angehalten." >&2
  exit 1
fi
echo "==> Wartungsmodus ist aus: client -> service liefert 200."

# Erst jetzt fortsetzen: der Client-Sidecar hat die Wartungsroute nicht mehr,
# der erste Lauf läuft also nicht mehr in die 503.
echo "==> Setze CronJobs mit Label wartung=service fort (spec.suspend=false)"
kubectl get cronjob -A -l wartung=service \
  -o jsonpath='{range .items[*]}{.metadata.namespace}{" "}{.metadata.name}{"\n"}{end}' \
  | while read -r ns name; do
      kubectl -n "${ns}" patch cronjob "${name}" --type merge -p '{"spec":{"suspend":false}}'
    done
