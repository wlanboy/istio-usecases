#!/usr/bin/env bash
# Prueft, ob virtualservice.py genau die in test/expected.tsv erwarteten Befunde meldet.
#   ./test.sh            liest VirtualServices/DestinationRules aus dem Cluster (Namespace vstest)
#   ./test.sh --offline  liest direkt die YAML-Dateien aus manifests/ (kein Cluster noetig)
set -euo pipefail
cd "$(dirname "$0")"

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

SOURCE_ARGS=()
if [[ "${1:-}" == "--offline" ]]; then
  SOURCE_ARGS=(--file manifests/)
fi

python3 virtualservice.py vstest "${SOURCE_ARGS[@]}" --format tsv | cut -f2,3 | sort > "$TMP/actual.tsv" || true
grep -v '^#' test/expected.tsv | sort > "$TMP/expected.tsv"

if diff -u "$TMP/expected.tsv" "$TMP/actual.tsv"; then
  echo "OK: alle $(wc -l < "$TMP/expected.tsv") erwarteten Befunde gefunden, keine zusaetzlichen."
else
  echo "FEHLER: Abweichung zwischen erwarteten (-) und gefundenen (+) Befunden." >&2
  exit 1
fi
