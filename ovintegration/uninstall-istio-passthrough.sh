#!/usr/bin/env bash
set -euo pipefail

ISTIO_NAMESPACE="istio-system"
CERT_SECRET_NAME="ovintegration-gateway-cert"

echo "==> Entferne Wildcard-Route und zentrales TLS-Gateway aus Namespace '${ISTIO_NAMESPACE}'"
oc -n "${ISTIO_NAMESPACE}" delete route istio-ingressgateway-wildcard --ignore-not-found
oc -n "${ISTIO_NAMESPACE}" delete gateway ovintegration-gateway-tls --ignore-not-found

echo "==> Entferne TLS-Secret '${CERT_SECRET_NAME}' aus Namespace '${ISTIO_NAMESPACE}'"
oc -n "${ISTIO_NAMESPACE}" delete secret "${CERT_SECRET_NAME}" --ignore-not-found

echo "==> Setze Wildcard-Route-Admission am Default-IngressController zurueck (Cluster-weite Aenderung)"
oc patch ingresscontroller/default -n openshift-ingress-operator --type=merge \
  -p '{"spec":{"routeAdmission":null}}'

echo "==> Fertig. Die Basis-Istio-Plattform (istiod, CNI, Gateway-Service) bleibt bestehen,"
echo "    ebenso alle ueber ./install.sh angelegten per-Namespace Demo-Usecases."
echo "    Zum vollstaendigen Entfernen der Plattform zusaetzlich: ./uninstall-istio.sh"
