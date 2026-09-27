#!/usr/bin/env bash
# Prueft, ob virtualservice.py genau die in test/expected.tsv erwarteten Befunde meldet.
#   ./test.sh [namespace]  liest VirtualServices/DestinationRules aus dem Cluster (Default: vstest)
#   ./test.sh --offline    liest direkt die YAML-Dateien aus manifests/ (kein Cluster noetig)
set -euo pipefail
cd "$(dirname "$0")"

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

PYTHON=(python3)
SOURCE_ARGS=()
if [[ "${1:-}" == "--offline" ]]; then
  shift
  SOURCE_ARGS=(--file manifests/)
  # --file braucht PyYAML; ist es nicht installiert, stellt uv es temporaer bereit.
  if ! python3 -c 'import yaml' 2>/dev/null && command -v uv >/dev/null; then
    PYTHON=(uv run -q --no-project --with pyyaml python3)
  fi
fi
NAMESPACE="${1:-vstest}"

# Exit-Code 1 bedeutet "ERROR-Befunde gefunden" und ist hier erwartet.
# Jeder andere Code ungleich 0 (Traceback, kubectl-Fehler, fehlendes PyYAML) bricht ab.
rc=0
"${PYTHON[@]}" virtualservice.py "$NAMESPACE" "${SOURCE_ARGS[@]}" > "$TMP/output.txt" || rc=$?
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

# Offline zusaetzlich: Ladefehler muessen Exit-Code 2 liefern, nicht 1 (= ERROR-Befunde)
# und nicht einen Traceback.
if (( ${#SOURCE_ARGS[@]} )); then
  expect_rc() {
    local want=$1 desc=$2; shift 2
    local got=0
    "${PYTHON[@]}" virtualservice.py "$NAMESPACE" "$@" > /dev/null 2> "$TMP/stderr.txt" || got=$?
    if (( got != want )) || grep -q Traceback "$TMP/stderr.txt"; then
      echo "FEHLER: $desc: Exit-Code $got statt $want" >&2
      cat "$TMP/stderr.txt" >&2
      exit 1
    fi
  }
  printf 'kind: VirtualService\nmetadata: {name: x\n' > "$TMP/broken.yaml"
  printf -- '- keine\n- Ressource\n' > "$TMP/scalar.yaml"
  printf 'kind: VirtualService\nmetadata: {namespace: %s}\nspec: {hosts: [a]}\n' \
    "$NAMESPACE" > "$TMP/noname.yaml"
  printf 'kind: VirtualService\nmetadata: {name: x, namespace: anderer-ns}\n' > "$TMP/otherns.yaml"
  # kind: List (wie aus kubectl get -o yaml) muss aufgeloest werden: der Catch-All
  # vor der zweiten Route ergibt einen ERROR-Befund, also Exit-Code 1.
  cat > "$TMP/list.yaml" <<EOF
kind: List
items:
  - kind: VirtualService
    metadata: {name: list-vs}
    spec:
      hosts: [list-svc]
      http:
        - route: [{destination: {host: list-svc}}]
        - match: [{uri: {prefix: /a}}]
          route: [{destination: {host: list-svc}}]
EOF
  expect_rc 2 "kaputtes YAML" --file "$TMP/broken.yaml"
  expect_rc 2 "fehlende Datei" --file "$TMP/fehlt.yaml"
  expect_rc 2 "Datei ohne VS/DR" --file "$TMP/scalar.yaml"
  expect_rc 2 "Ressource ohne Namen" --file "$TMP/noname.yaml"
  expect_rc 2 "nur Ressourcen eines anderen Namespace" --file "$TMP/otherns.yaml"
  expect_rc 1 "kind: List" --file "$TMP/list.yaml"
  echo "OK: Ladefehler liefern Exit-Code 2, kind: List wird gelesen."
fi
