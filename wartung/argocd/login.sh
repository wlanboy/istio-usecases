#!/usr/bin/env bash
# Meldet die argocd-CLI mit dem initialen admin-Passwort am lokalen Argo CD an.
# Argo CD läuft mit server.insecure=true hinter dem Istio Ingress-Gateway
# (argocd.localhost), daher --plaintext und --grpc-web. Ohne ":80" hängt
# argocd login mit "gRPC connection not ready".
set -euo pipefail

ARGOCD_SERVER="${ARGOCD_SERVER:-argocd.localhost:80}"

echo "==> Login bei Argo CD (${ARGOCD_SERVER}) als admin"
password=$(argocd admin initial-password -n argocd | head -n1)
argocd login "${ARGOCD_SERVER}" --plaintext --grpc-web --username admin --password "${password}"
