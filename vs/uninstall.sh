#!/usr/bin/env bash
# Entfernt nur die Testfall-Ressourcen, der Namespace selbst bleibt bestehen.
#   ./uninstall.sh [namespace]
set -euo pipefail
cd "$(dirname "$0")"
NAMESPACE="${1:-vstest}"

kubectl delete -n "$NAMESPACE" -f manifests/ --ignore-not-found
