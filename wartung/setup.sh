#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> Lege Namespaces 'client' und 'service' an (istio-injection=enabled)"
kubectl apply -f "${SCRIPT_DIR}/manifests/00-namespaces.yaml"

echo "==> Installiere Helm-Chart 'service' in Namespace 'service'"
helm upgrade --install service "${SCRIPT_DIR}/charts/service" --namespace service

echo "==> Installiere Helm-Chart 'client' in Namespace 'client'"
helm upgrade --install client "${SCRIPT_DIR}/charts/client" --namespace client

echo "==> Warte auf die Deployments"
kubectl -n service rollout status deployment/service --timeout=120s
kubectl -n client rollout status deployment/client --timeout=120s

echo "==> Lege CronJob 'healthz-cron' in Namespace 'client' an"
kubectl apply -f "${SCRIPT_DIR}/cron/cronjob.yaml"

echo "==> Fertig."
kubectl -n service get deploy,svc,gateway,virtualservice
kubectl -n client get deploy,svc,gateway,virtualservice,cronjob
echo
echo "    Client-UI:  http://client.localhost"
echo "    Service-UI: http://service.localhost"
echo "    Ziel-URL im Client: http://service.service.svc.cluster.local/healthz"
