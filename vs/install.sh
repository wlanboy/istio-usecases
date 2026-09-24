#!/usr/bin/env bash
# Spielt alle Konflikt-Testfaelle in einen Namespace ein (Default: vstest).
# Ein bestehender Namespace wird unveraendert genutzt, ein fehlender mit
# istio-injection=enabled angelegt.
#   ./install.sh [namespace]
set -euo pipefail
cd "$(dirname "$0")"
NAMESPACE="${1:-vstest}"

if ! kubectl get namespace "$NAMESPACE" >/dev/null 2>&1; then
  kubectl create namespace "$NAMESPACE"
  kubectl label namespace "$NAMESPACE" istio-injection=enabled
fi
kubectl apply -n "$NAMESPACE" -f manifests/
