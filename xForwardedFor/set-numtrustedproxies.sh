#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PATCH_TEMPLATE="${SCRIPT_DIR}/manifests/40-numtrustedproxies-patch.yaml"

NAMESPACE="${1:-xff-demo}"
COUNT="${2:-}"

if [[ -z "${COUNT}" ]]; then
  echo "Usage: $0 <namespace> <numTrustedProxies|none>" >&2
  echo "  z.B.: $0 xff-demo 1        # 1 vertrauenswürdiger Proxy-Hop vor diesem Gateway" >&2
  echo "        $0 xff-demo none    # proxy.istio.io/config-Annotation wieder entfernen (Default: 0)" >&2
  exit 1
fi

TMP_PATCH="$(mktemp)"
trap 'rm -f "${TMP_PATCH}"' EXIT

if [[ "${COUNT}" == "none" ]]; then
  echo "==> Entferne proxy.istio.io/config-Annotation vom Gateway in Namespace '${NAMESPACE}' (zurück auf Default numTrustedProxies=0)"
  cat > "${TMP_PATCH}" <<'EOF'
spec:
  template:
    metadata:
      annotations:
        proxy.istio.io/config: null
EOF
else
  echo "==> Setze numTrustedProxies=${COUNT} auf dem Gateway in Namespace '${NAMESPACE}'"
  sed "s|\${NUM_TRUSTED_PROXIES}|${COUNT}|g" "${PATCH_TEMPLATE}" > "${TMP_PATCH}"
fi

kubectl -n "${NAMESPACE}" patch deployment xff-demo-gateway --type merge --patch-file "${TMP_PATCH}"
kubectl -n "${NAMESPACE}" rollout status deployment/xff-demo-gateway --timeout=90s

echo "==> Aktive Konfiguration (Pod-Template des Deployments):"
kubectl -n "${NAMESPACE}" get deployment xff-demo-gateway \
  -o jsonpath='{.spec.template.metadata.annotations.proxy\.istio\.io/config}'
echo
