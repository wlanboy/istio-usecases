#!/usr/bin/env bash
set -euo pipefail
kubectl delete namespace vstest --ignore-not-found
