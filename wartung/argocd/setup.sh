#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

"${SCRIPT_DIR}/login.sh"

# Optional andere Git-Quelle, z.B. Fork oder Branch:
#   REPO_URL=https://github.com/<user>/istio-usecases.git REVISION=mein-branch ./setup.sh
# Gilt für wartung-root (--repo, --revision) und für die Kind-Apps (Helm-Werte
# repoURL, targetRevision).
source_args=()
if [[ -n "${REPO_URL:-}" || -n "${REVISION:-}" ]]; then
  repo="${REPO_URL:-https://github.com/wlanboy/istio-usecases.git}"
  revision="${REVISION:-main}"
  echo "==> Git-Quelle: ${repo} @ ${revision}"
  source_args=(--repo "${repo}" --revision "${revision}"
    --helm-set repoURL="${repo}" --helm-set targetRevision="${revision}")
fi

# Eine vorhandene wartung-root bleibt unverändert, damit ein erneuter Aufruf
# den Wartungsschalter nicht zurücksetzt.
if argocd app get wartung-root >/dev/null 2>&1; then
  echo "==> wartung-root existiert schon, wird nicht neu angelegt"
else
  echo "==> Lege Root-App wartung-root an"
  argocd app create -f "${SCRIPT_DIR}/root-app.yaml" "${source_args[@]}"
fi

echo "==> Warte auf wartung-root"
argocd app wait wartung-root --sync --health --timeout 180 >/dev/null

echo "==> Warte auf die Kind-Apps"
argocd app wait wartung-client wartung-service wartung-healthz-cron --sync --health --timeout 180 >/dev/null

argocd app list
