#!/usr/bin/env bash

set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 <run-dir>" >&2
  exit 1
fi

RUN_DIR="$1"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
FIRST_RESULTS="${RUN_DIR}/agent_results_0000_0281.jsonl"
REST_RESULTS="${RUN_DIR}/agent_results_0282_1054.jsonl"

while [[ ! -f "${FIRST_RESULTS}" ]]; do
  sleep 60
done

while [[ ! -f "${RUN_DIR}/run_metadata.json" ]]; do
  sleep 60
done

if [[ ! -f "${REST_RESULTS}" ]]; then
  python "${REPO_ROOT}/test_cc/scripts/run_claude_code.py" \
    --run-dir "${RUN_DIR}" \
    --row-start 282 \
    --max-concurrency 8 \
    --result-file "$(basename "${REST_RESULTS}")" \
    --allow-unsafe-host-execution
fi

if [[ ! -f "${RUN_DIR}/summary.json" ]]; then
  python "${REPO_ROOT}/test_cc/scripts/score_livecodebench.py" \
    --run-dir "${RUN_DIR}" \
    --allow-unsafe-local-eval
fi
