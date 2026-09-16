#!/usr/bin/env bash
set -euo pipefail

ISTIO_NAMESPACE="istio-system"
CNI_NAMESPACE="kube-system"
GATEWAY_RELEASE="istio-ingressgateway"

echo "==> Entferne Helm-Releases (Reihenfolge umgekehrt zur Installation)"
helm uninstall "${GATEWAY_RELEASE}" -n "${ISTIO_NAMESPACE}" || true
helm uninstall istiod -n "${ISTIO_NAMESPACE}" || true
helm uninstall istio-cni -n "${CNI_NAMESPACE}" || true
helm uninstall istio-base -n "${ISTIO_NAMESPACE}" || true

echo "==> Entferne SCC-Bindings"
oc adm policy remove-scc-from-group anyuid "system:serviceaccounts:${ISTIO_NAMESPACE}" || true
oc adm policy remove-scc-from-user privileged -z istio-cni -n "${CNI_NAMESPACE}" || true

echo "==> Hinweis: CRDs von istio-base wurden bewusst NICHT geloescht (Helm-Verhalten)."
echo "    Falls wirklich nichts mehr von Istio uebrig bleiben soll:"
echo "      oc get crd -o name | grep istio.io | xargs -r oc delete"
echo "      oc delete namespace ${ISTIO_NAMESPACE}"
