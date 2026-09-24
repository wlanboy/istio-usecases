#!/usr/bin/env bash
# Legt den Namespace vstest an und spielt alle Konflikt-Testfaelle ein.
set -euo pipefail
cd "$(dirname "$0")"

kubectl apply -f manifests/
