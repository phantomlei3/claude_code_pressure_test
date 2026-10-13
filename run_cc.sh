#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${SCRIPT_DIR}"
RUNS_ROOT="${CC_RUNS_ROOT:-${REPO_ROOT}/test_cc/runs}"
DEFAULT_RUN_DIR="${RUNS_ROOT}/lcb1055_hf_20260830_011500"
RUN_DIR_INPUT="${1:-${CC_RUN_DIR:-${DEFAULT_RUN_DIR}}}"
STRESS_RUNNER="${CC_STRESS_RUNNER:-${REPO_ROOT}/test_cc/scripts/run_claude_code_stress.py}"
PYTHON_BIN="${PYTHON_BIN:-python}"
SESSIONS="${CC_SESSIONS:-16}"
TURN_TIMEOUT_SEC="${CC_TURN_TIMEOUT_SEC:-7200}"
AUTOCOMPACT="${CC_AUTOCOMPACT:-512k}"
CONTEXT_PAYLOAD_CHARS="${CC_CONTEXT_PAYLOAD_CHARS:-16384}"

export API_FORCE_IDLE_TIMEOUT=false
export API_TIMEOUT_MS=1200000

if [[ ! "${CONTEXT_PAYLOAD_CHARS}" =~ ^(0|[1-9][0-9]*)$ ]] ||
   (( CONTEXT_PAYLOAD_CHARS > 200000 )); then
  echo "CC_CONTEXT_PAYLOAD_CHARS must be between 0 and 200000" >&2
  exit 1
fi

if [[ "${AUTOCOMPACT}" =~ ^([1-9][0-9]*)([kKmM]?)$ ]]; then
  AUTOCOMPACT_TOKENS="${BASH_REMATCH[1]}"
  case "${BASH_REMATCH[2]}" in
    [kK]) AUTOCOMPACT_TOKENS="${AUTOCOMPACT_TOKENS}000" ;;
    [mM]) AUTOCOMPACT_TOKENS="${AUTOCOMPACT_TOKENS}000000" ;;
  esac
else
  echo "CC_AUTOCOMPACT must be a token count such as 512k or 512000: ${AUTOCOMPACT}" >&2
  exit 1
fi
if (( AUTOCOMPACT_TOKENS < 100000 || AUTOCOMPACT_TOKENS > 1000000 )); then
  echo "CC_AUTOCOMPACT must be between 100k and 1M tokens: ${AUTOCOMPACT}" >&2
  exit 1
fi

export CLAUDE_CODE_MAX_CONTEXT_TOKENS="${CLAUDE_CODE_MAX_CONTEXT_TOKENS:-${CC_MAX_CONTEXT_TOKENS:-${AUTOCOMPACT_TOKENS}}}"
export CLAUDE_CODE_AUTO_COMPACT_WINDOW="${CLAUDE_CODE_AUTO_COMPACT_WINDOW:-${AUTOCOMPACT_TOKENS}}"
export CLAUDE_AUTOCOMPACT_PCT_OVERRIDE="${CLAUDE_AUTOCOMPACT_PCT_OVERRIDE:-${CC_AUTOCOMPACT_PCT:-80}}"
if [[ ! "${CLAUDE_CODE_MAX_CONTEXT_TOKENS}" =~ ^[1-9][0-9]*$ ]] ||
   [[ ! "${CLAUDE_CODE_AUTO_COMPACT_WINDOW}" =~ ^[1-9][0-9]*$ ]] ||
   [[ ! "${CLAUDE_AUTOCOMPACT_PCT_OVERRIDE}" =~ ^[1-9][0-9]*$ ]] ||
   (( CLAUDE_AUTOCOMPACT_PCT_OVERRIDE < 1 || CLAUDE_AUTOCOMPACT_PCT_OVERRIDE > 100 )); then
  echo "Claude context settings must be integer token counts and a percentage from 1 to 100" >&2
  exit 1
fi

if [[ $# -gt 0 ]]; then
  shift
fi

if [[ ! -d "${RUNS_ROOT}" ]]; then
  echo "Runs root does not exist: ${RUNS_ROOT}" >&2
  exit 1
fi
RUNS_ROOT="$(realpath -e "${RUNS_ROOT}")"

if [[ ! -d "${RUN_DIR_INPUT}" ]]; then
  echo "Prepared run directory does not exist: ${RUN_DIR_INPUT}" >&2
  exit 1
fi
RUN_DIR="$(realpath -e "${RUN_DIR_INPUT}")"
if [[ "$(dirname "${RUN_DIR}")" != "${RUNS_ROOT}" ]]; then
  echo "Run directory must be directly below ${RUNS_ROOT}: ${RUN_DIR}" >&2
  exit 1
fi

if [[ ! -s "${RUN_DIR}/manifest.jsonl" ]]; then
  echo "Missing or empty frozen manifest: ${RUN_DIR}/manifest.jsonl" >&2
  exit 1
fi
if [[ ! -s "${RUN_DIR}/run_metadata.json" ]]; then
  echo "Dataset preparation is incomplete; missing run metadata: ${RUN_DIR}/run_metadata.json" >&2
  exit 1
fi
if [[ ! -f "${STRESS_RUNNER}" ]]; then
  echo "Stress runner does not exist: ${STRESS_RUNNER}" >&2
  exit 1
fi
if ! command -v flock >/dev/null 2>&1; then
  echo "flock is required to prevent concurrent cleanup of the same run" >&2
  exit 1
fi

LOCK_FILE="${RUN_DIR}/.run_cc.lock"
exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
  echo "Another stress run is already using ${RUN_DIR}" >&2
  exit 1
fi

SESSION_DIR="${RUN_DIR}/stress_sessions"
RESULT_FILE="${RUN_DIR}/stress_results.jsonl"
if [[ -e "${SESSION_DIR}" && ! -d "${SESSION_DIR}" ]]; then
  echo "Refusing to remove non-directory session path: ${SESSION_DIR}" >&2
  exit 1
fi

if [[ -d "${SESSION_DIR}" ]]; then
  rm -rf -- "${SESSION_DIR}"
  echo "Removed previous sessions: ${SESSION_DIR}"
fi
if [[ -f "${RESULT_FILE}" ]]; then
  rm -f -- "${RESULT_FILE}"
  echo "Removed previous results: ${RESULT_FILE}"
fi

echo "Starting stress run: run_dir=${RUN_DIR} sessions=${SESSIONS} context=${CLAUDE_CODE_MAX_CONTEXT_TOKENS} autocompact=${CLAUDE_CODE_AUTO_COMPACT_WINDOW} pct=${CLAUDE_AUTOCOMPACT_PCT_OVERRIDE} payload_chars=${CONTEXT_PAYLOAD_CHARS}"
exec "${PYTHON_BIN}" "${STRESS_RUNNER}" \
  --run-dir "${RUN_DIR}" \
  --sessions "${SESSIONS}" \
  --turn-timeout-sec "${TURN_TIMEOUT_SEC}" \
  --autocompact "${AUTOCOMPACT}" \
  --context-payload-chars "${CONTEXT_PAYLOAD_CHARS}" \
  --repeat-forever \
  --allow-unsafe-host-execution \
  "$@"
