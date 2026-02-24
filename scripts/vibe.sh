#!/usr/bin/env bash
set -euo pipefail

TASK="${1:-}"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

resolve_python() {
  if [[ -x "${ROOT}/.venv/bin/python" ]]; then
    echo "${ROOT}/.venv/bin/python"
    return
  fi
  if [[ -x "${ROOT}/venv/bin/python" ]]; then
    echo "${ROOT}/venv/bin/python"
    return
  fi
  command -v python3 >/dev/null 2>&1 && { echo "python3"; return; }
  echo "python"
}

run() {
  echo ">> $*"
  "$@"
}

check_health() {
  local url="$1"
  echo ">> GET ${url}"
  curl -fsS "${url}" >/dev/null
}

usage() {
  cat <<'EOF'
Usage: ./scripts/vibe.sh <task>

Tasks:
  bootstrap
  format
  lint
  typecheck
  test-quick
  test-full
  test-regression
  review-fast
  review-full
  smoke-api
  smoke-docker
EOF
}

if [[ -z "${TASK}" ]]; then
  usage
  exit 1
fi

PYTHON="$(resolve_python)"

echo "Running task '${TASK}' in ${ROOT}"
echo "Using Python: ${PYTHON}"

cd "${ROOT}"

case "${TASK}" in
  bootstrap)
    run "${PYTHON}" -m pip install -r requirements.txt
    run "${PYTHON}" -m pip install -e ".[dev]"
    ;;
  format)
    run "${PYTHON}" -m ruff check --fix libs services workers tests
    run "${PYTHON}" -m black libs services workers tests
    run "${PYTHON}" -m isort libs services workers tests
    ;;
  lint)
    run "${PYTHON}" -m ruff check libs services workers tests
    run "${PYTHON}" -m black --check libs services workers tests
    run "${PYTHON}" -m isort --check-only libs services workers tests
    ;;
  typecheck)
    run "${PYTHON}" -m mypy libs services workers
    ;;
  test-quick)
    run "${PYTHON}" -m pytest -q
    ;;
  test-full)
    run "${PYTHON}" -m pytest tests -v
    ;;
  test-regression)
    run "${PYTHON}" -m pytest -q \
      tests/test_route_regressions.py \
      tests/test_stage_extraction_regressions.py \
      tests/test_review_workflow_guards.py
    ;;
  review-fast)
    run "${PYTHON}" -m compileall libs services workers tests
    run "${PYTHON}" -m pytest -q
    ;;
  review-full)
    run "${PYTHON}" -m ruff check libs services workers tests
    run "${PYTHON}" -m black --check libs services workers tests
    run "${PYTHON}" -m isort --check-only libs services workers tests
    run "${PYTHON}" -m mypy libs services workers
    run "${PYTHON}" -m pytest tests -v
    ;;
  smoke-api)
    check_health "http://localhost:8001/health"
    check_health "http://localhost:8002/health"
    check_health "http://localhost:8003/health"
    ;;
  smoke-docker)
    run docker compose ps
    ;;
  *)
    usage
    exit 1
    ;;
esac

echo "Task '${TASK}' completed successfully."
