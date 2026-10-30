#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

"${SCRIPT_DIR}/login.sh"

echo "==> Lösche wartung-root mit allen Kind-Apps, Ressourcen und Namespaces"
argocd app delete wartung-root --cascade --yes --wait

echo "==> Warte, bis die Namespaces client und service weg sind"
kubectl wait --for=delete namespace/client namespace/service --timeout=120s 2>/dev/null || true

argocd app list
