#!/usr/bin/env bash
# One command for the usual local tasks. Run from anywhere:
#
#   voice_agent/scripts/dev.sh setup       # check tools + Google sign-in, update .env, install deps
#   voice_agent/scripts/dev.sh test        # all tests that need no cloud access (~1 min)
#   voice_agent/scripts/dev.sh scenarios   # test-plan + roaming scenarios on the real model [--judge -v ...]
#   voice_agent/scripts/dev.sh run         # start the server as Cloud Run does -> http://127.0.0.1:8080
#   voice_agent/scripts/dev.sh mock        # standalone mock gateway on :8090 (HTTP path testing)
#   voice_agent/scripts/dev.sh deploy      # deploy to Cloud Run (dev projects only, see deploy.sh)
#   voice_agent/scripts/dev.sh validate    # check traces/logs/metrics of the deployed service
#   voice_agent/scripts/dev.sh all         # setup + test + scenarios
#
# Extra arguments are passed on (e.g. `dev.sh scenarios TP-10 R-05 -v`).
set -euo pipefail

APP="$(cd "$(dirname "$0")/.." && pwd)"
REPO="$(cd "$APP/.." && pwd)"
cd "$APP"

step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
die() { printf '\033[31m%s\033[0m\n' "$*" >&2; exit 1; }

setup() {
  step "Tools"
  command -v uv >/dev/null || die "uv is not installed: https://docs.astral.sh/uv/getting-started/installation/"
  command -v gcloud >/dev/null || echo "gcloud not found: needed for Vertex AI, deploy and validate (tests still run)."
  echo "uv $(uv --version | awk '{print $2}')"

  step "Settings (.env)"
  python3 scripts/setup_env.py "$@"

  step "Dependencies (uv sync)"
  uv sync

  step "Google sign-in (Application Default Credentials)"
  if command -v gcloud >/dev/null; then
    if gcloud auth application-default print-access-token >/dev/null 2>&1; then
      echo "Signed in."
    else
      echo "Not signed in (or expired): opening the browser sign-in..."
      gcloud auth application-default login
    fi
  fi
  echo
  echo "Ready. Next: scripts/dev.sh test, then scripts/dev.sh run"
}

run_tests() {
  step "Roaming, tools and agent flow (pytest)"
  uv run pytest -q tests
  step "Observability"
  uv run python -m unittest discover -s observability/tests -t .
  step "Security checks"
  (cd "$REPO" && SKIP_TERRAFORM="${SKIP_TERRAFORM:-$(command -v terraform >/dev/null && echo "" || echo 1)}" \
     bash security/ci/run_checks.sh)
  step "All tests passed"
}

case "${1:-help}" in
  setup)     shift; setup "$@" ;;
  test)      run_tests ;;
  scenarios) shift; uv run python scripts/run_scenarios.py "$@" ;;
  run)       python3 scripts/setup_env.py --check; uv run python -m observability ;;
  mock)      shift; uv run python -m mock_gateway "$@" ;;
  deploy)    ./deploy.sh ;;
  validate)  shift; uv run python scripts/validate_observability.py "$@" ;;
  all)       shift; setup; run_tests; uv run python scripts/run_scenarios.py "$@" ;;
  help|-h|--help) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//' ;;
  *) die "Unknown command '$1'. Try: setup, test, scenarios, run, mock, deploy, validate, all" ;;
esac
