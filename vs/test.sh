#!/usr/bin/env bash
# Prueft, ob virtualservice.py genau die in test/expected.tsv erwarteten Befunde meldet.
#   ./test.sh            liest VirtualServices/DestinationRules aus dem Cluster (Namespace vstest)
#   ./test.sh --offline  liest direkt die YAML-Dateien aus manifests/ (kein Cluster noetig)
set -euo pipefail
cd "$(dirname "$0")"

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

PYTHON=(python3)
SOURCE_ARGS=()
if [[ "${1:-}" == "--offline" ]]; then
  SOURCE_ARGS=(--file manifests/)
  # --file braucht PyYAML; ist es nicht installiert, stellt uv es temporaer bereit.
  if ! python3 -c 'import yaml' 2>/dev/null && command -v uv >/dev/null; then
    PYTHON=(uv run -q --no-project --with pyyaml python3)
  fi
fi

# Exit-Code 1 bedeutet "ERROR-Befunde gefunden" und ist hier erwartet.
# Jeder andere Code ungleich 0 (Traceback, kubectl-Fehler, fehlendes PyYAML) bricht ab.
rc=0
"${PYTHON[@]}" virtualservice.py vstest "${SOURCE_ARGS[@]}" > "$TMP/output.txt" || rc=$?
if (( rc > 1 )); then
  echo "FEHLER: virtualservice.py ist mit Exit-Code $rc abgebrochen." >&2
  exit "$rc"
fi

sed -nE 's/^\[[A-Z ]+\] +([A-Z-]+) +(.*)/\1\t\2/p' "$TMP/output.txt" | sed 's/, /,/g' \
  | sort > "$TMP/actual.tsv"
grep -v '^#' test/expected.tsv | sort > "$TMP/expected.tsv"

if diff -u "$TMP/expected.tsv" "$TMP/actual.tsv"; then
  echo "OK: alle $(wc -l < "$TMP/expected.tsv") erwarteten Befunde gefunden, keine zusaetzlichen."
else
  echo "FEHLER: Abweichung zwischen erwarteten (-) und gefundenen (+) Befunden." >&2
  exit 1
fi
