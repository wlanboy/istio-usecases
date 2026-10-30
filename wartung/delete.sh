#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> Entferne einen ggf. noch aktiven Wartungsmodus"
kubectl delete -f "${SCRIPT_DIR}/manifestwartung/" --ignore-not-found

echo "==> Lösche CronJob 'healthz-cron' und seine Jobs"
kubectl delete -f "${SCRIPT_DIR}/cron/cronjob.yaml" --ignore-not-found

echo "==> Deinstalliere Helm-Charts 'client' und 'service'"
helm uninstall client --namespace client --ignore-not-found
helm uninstall service --namespace service --ignore-not-found

echo "==> Lösche Namespaces 'client' und 'service'"
kubectl delete -f "${SCRIPT_DIR}/manifests/00-namespaces.yaml" --ignore-not-found
