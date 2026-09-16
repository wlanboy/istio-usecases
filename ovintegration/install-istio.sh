#!/usr/bin/env bash
set -euo pipefail

ISTIO_NAMESPACE="istio-system"
CNI_NAMESPACE="kube-system"
GATEWAY_RELEASE="istio-ingressgateway"

echo "==> Registriere Helm-Repo 'istio' (https://istio-release.storage.googleapis.com/charts)"
helm repo add istio https://istio-release.storage.googleapis.com/charts >/dev/null
helm repo update istio >/dev/null

if ! oc get namespace "${ISTIO_NAMESPACE}" >/dev/null 2>&1; then
  echo "==> Erstelle Namespace '${ISTIO_NAMESPACE}'"
  oc create namespace "${ISTIO_NAMESPACE}"
fi

echo "==> anyuid-SCC fuer ServiceAccounts in '${ISTIO_NAMESPACE}' (Istio-Proxy laeuft fest mit UID 1337,"
echo "    die Standard-SCC 'restricted-v2' erlaubt aber nur eine projektspezifisch zugewiesene UID-Range)"
oc adm policy add-scc-to-group anyuid "system:serviceaccounts:${ISTIO_NAMESPACE}"

echo "==> Installiere istio-base (CRDs, Cluster-Rollen) mit platform=openshift"
helm upgrade --install istio-base istio/base -n "${ISTIO_NAMESPACE}" \
  --set platform=openshift \
  --wait

echo "==> Installiere istio-cni (ersetzt privilegierte istio-init-Init-Container durch einen"
echo "    einzelnen privilegierten DaemonSet-Pod pro Node; auf OpenShift ueber Multus als"
echo "    NetworkAttachmentDefinition eingehaengt, siehe readme.md)"
helm upgrade --install istio-cni istio/cni -n "${CNI_NAMESPACE}" \
  --set platform=openshift

echo "==> privileged-SCC fuer ServiceAccount 'istio-cni' in '${CNI_NAMESPACE}'"
echo "    (DaemonSet-Pod braucht Node-Rechte, um Pod-Netzwerk-Namespaces umzukonfigurieren)"
oc adm policy add-scc-to-user privileged -z istio-cni -n "${CNI_NAMESPACE}"

echo "==> Warte auf istio-cni-node DaemonSet"
oc -n "${CNI_NAMESPACE}" rollout status daemonset/istio-cni-node --timeout=120s

echo "==> Installiere istiod (Control Plane) mit platform=openshift"
helm upgrade --install istiod istio/istiod -n "${ISTIO_NAMESPACE}" \
  --set platform=openshift \
  --set pilot.cni.enabled=true \
  --wait

echo "==> Installiere Ingress-Gateway '${GATEWAY_RELEASE}' (ClusterIP - Route/Router uebernimmt"
echo "    die externe Erreichbarkeit, keine Cloud-LoadBalancer-Abhaengigkeit noetig)"
helm upgrade --install "${GATEWAY_RELEASE}" istio/gateway -n "${ISTIO_NAMESPACE}" \
  --set platform=openshift \
  --set service.type=ClusterIP \
  --wait

echo "==> Fertig. Status der Control Plane:"
oc -n "${ISTIO_NAMESPACE}" get pods
oc -n "${CNI_NAMESPACE}" get pods -l k8s-app=istio-cni-node
