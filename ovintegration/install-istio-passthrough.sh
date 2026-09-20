#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MANIFESTS_DIR="${SCRIPT_DIR}/manifests-passthrough"

ISTIO_NAMESPACE="istio-system"
CNI_NAMESPACE="kube-system"
GATEWAY_RELEASE="istio-ingressgateway"
CERT_SECRET_NAME="ovintegration-gateway-cert"

render() {
  sed -e "s|\${APPS_DOMAIN}|${APPS_DOMAIN}|g" -e "s|\${CERT_SECRET_NAME}|${CERT_SECRET_NAME}|g" "$1"
}

echo "==> Registriere Helm-Repo 'istio' (https://istio-release.storage.googleapis.com/charts)"
helm repo add istio https://istio-release.storage.googleapis.com/charts >/dev/null
helm repo update istio >/dev/null

if ! oc get namespace "${ISTIO_NAMESPACE}" >/dev/null 2>&1; then
  echo "==> Erstelle Namespace '${ISTIO_NAMESPACE}'"
  oc create namespace "${ISTIO_NAMESPACE}"
fi

echo "==> anyuid-SCC fuer ServiceAccounts in '${ISTIO_NAMESPACE}' (siehe install-istio.sh/readme.md)"
oc adm policy add-scc-to-group anyuid "system:serviceaccounts:${ISTIO_NAMESPACE}"

echo "==> Installiere istio-base (CRDs, Cluster-Rollen) mit platform=openshift"
helm upgrade --install istio-base istio/base -n "${ISTIO_NAMESPACE}" \
  --set platform=openshift \
  --wait

echo "==> Installiere istio-cni"
helm upgrade --install istio-cni istio/cni -n "${CNI_NAMESPACE}" \
  --set platform=openshift

echo "==> privileged-SCC fuer ServiceAccount 'istio-cni' in '${CNI_NAMESPACE}'"
oc adm policy add-scc-to-user privileged -z istio-cni -n "${CNI_NAMESPACE}"

echo "==> Warte auf istio-cni-node DaemonSet"
oc -n "${CNI_NAMESPACE}" rollout status daemonset/istio-cni-node --timeout=120s

echo "==> Installiere istiod (Control Plane) mit platform=openshift"
helm upgrade --install istiod istio/istiod -n "${ISTIO_NAMESPACE}" \
  --set platform=openshift \
  --set pilot.cni.enabled=true \
  --wait

echo "==> Installiere Ingress-Gateway '${GATEWAY_RELEASE}' (ClusterIP)"
helm upgrade --install "${GATEWAY_RELEASE}" istio/gateway -n "${ISTIO_NAMESPACE}" \
  --set platform=openshift \
  --set service.type=ClusterIP \
  --wait

echo "==> Ermittle Cluster-Apps-Domain"
APPS_DOMAIN="$(oc get ingresscontroller/default -n openshift-ingress-operator -o jsonpath='{.status.domain}')"
echo "    Apps-Domain: ${APPS_DOMAIN}"

echo "==> Erlaube Wildcard-Routes am Default-IngressController"
echo "    (Cluster-weite Aenderung, nicht nur '${ISTIO_NAMESPACE}', siehe wildcard-ingress.md Schritt 2)"
oc patch ingresscontroller/default -n openshift-ingress-operator --type=merge \
  -p '{"spec":{"routeAdmission":{"wildcardPolicy":"WildcardsAllowed"}}}'

if oc -n "${ISTIO_NAMESPACE}" get secret "${CERT_SECRET_NAME}" >/dev/null 2>&1; then
  echo "==> TLS-Secret '${CERT_SECRET_NAME}' existiert bereits in '${ISTIO_NAMESPACE}', wird nicht angetastet"
  echo "    (eigenes/von einer echten CA ausgestelltes Zertifikat bleibt erhalten)"
else
  echo "==> Erzeuge selbstsigniertes Wildcard-Zertifikat fuer *.${APPS_DOMAIN}"
  echo "    (nur fuer Demo-Zwecke - fuer produktiven Einsatz stattdessen vorher ein von der eigenen"
  echo "    Sub-CA ausgestelltes Zertifikat als Secret '${CERT_SECRET_NAME}' in '${ISTIO_NAMESPACE}'"
  echo "    anlegen, siehe custom-ca-tls.md Schritt 1)"
  CERT_DIR="$(mktemp -d)"
  trap 'rm -rf "${CERT_DIR}"' EXIT
  openssl req -x509 -newkey rsa:2048 -nodes -days 365 \
    -keyout "${CERT_DIR}/tls.key" -out "${CERT_DIR}/tls.crt" \
    -subj "/CN=*.${APPS_DOMAIN}" \
    -addext "subjectAltName=DNS:*.${APPS_DOMAIN}" >/dev/null 2>&1
  oc create secret tls "${CERT_SECRET_NAME}" \
    --cert="${CERT_DIR}/tls.crt" --key="${CERT_DIR}/tls.key" \
    -n "${ISTIO_NAMESPACE}"
fi

echo "==> Wende zentrales TLS-Gateway und Wildcard-Route an (Namespace ${ISTIO_NAMESPACE})"
for f in "${MANIFESTS_DIR}"/*.yaml; do
  render "${f}" | oc apply -f -
done

echo "==> Warte auf Route-Admission"
ADMITTED=""
for i in $(seq 1 30); do
  ADMITTED="$(oc -n "${ISTIO_NAMESPACE}" get route istio-ingressgateway-wildcard \
    -o jsonpath='{.status.ingress[0].conditions[?(@.type=="Admitted")].status}' 2>/dev/null || true)"
  [[ "${ADMITTED}" == "True" ]] && break
  sleep 1
done

echo
echo "==> Fertig."
echo "    Wildcard-Route: *.${APPS_DOMAIN} -> Service ${GATEWAY_RELEASE} (passthrough) -> Envoy"
echo "    TLS-Terminierung findet am Envoy statt (Zertifikat aus Secret '${CERT_SECRET_NAME}')."
echo "    Route-Status:   ${ADMITTED:-<noch nicht admitted, siehe: oc -n ${ISTIO_NAMESPACE} get route istio-ingressgateway-wildcard>}"
echo
echo "    Damit ein Demo-Usecase (./install.sh <namespace>) ueber DIESES zentrale Gateway erreichbar"
echo "    wird (statt nur ueber seine eigene per-Namespace Route/HTTP-Gateway), muss dessen"
echo "    VirtualService 'gateways: [istio-system/ovintegration-gateway-tls]' referenzieren, siehe"
echo "    custom-ca-tls.md Schritt 3. Die per-Namespace Route/Gateway aus install.sh bleiben davon"
echo "    unberuehrt parallel nutzbar (Klartext-HTTP-Pfad ueber den eigenen Hostnamen)."
echo
echo "    Falls der Router bei Anfragen an die Wildcard-Route 503 liefert, obwohl Gateway-Pod und"
echo "    Route selbst gesund sind: NetworkPolicy-Falle, siehe wildcard-ingress.md Schritt 4."
