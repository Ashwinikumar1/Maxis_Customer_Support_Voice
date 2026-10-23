#!/usr/bin/env bash
# One command for the usual local tasks. Run from anywhere:
#
#   voice_agent/scripts/dev.sh             # EVERYTHING, once: setup -> offline tests -> start the server
#                                          #   (stops at the first failure; add --rebuild to rewrite .env
#                                          #   with every comment, --scenarios to also run the model tests)
#
# Or one step at a time:
#   voice_agent/scripts/dev.sh setup       # check tools + Google sign-in, update .env, install deps
#                                          #   (setup --rebuild: .env in the template layout, all comments)
#   voice_agent/scripts/dev.sh test        # all tests that need no cloud access (~1 min)
#                                          #   (test --terraform: also validate security/terraform; it
#                                          #   downloads Terraform providers, e.g. Genesys, from the
#                                          #   internet, so it is off by default)
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
  # Terraform validation (security/terraform) checks the infrastructure code only, not the app, and
  # `terraform init` downloads the Google and Genesys Cloud providers from registry.terraform.io,
  # which corporate networks often block. So it runs only when asked: `dev.sh test --terraform`
  # (CI runs it on every pull request anyway).
  local skip_tf=1
  [[ "${1:-}" == "--terraform" ]] && skip_tf=""
  step "Security checks${skip_tf:+ (Terraform skipped; dev.sh test --terraform to include it)}"
  (cd "$REPO" && SKIP_TERRAFORM="$skip_tf" bash security/ci/run_checks.sh)
  step "All tests passed"
}

start() {
  local rebuild="" scenarios=""
  for arg in "$@"; do
    case "$arg" in
      --rebuild) rebuild="--rebuild" ;;
      --scenarios) scenarios=1 ;;
      *) die "Unknown option '$arg'. Use --rebuild and/or --scenarios" ;;
    esac
  done
  setup $rebuild
  run_tests
  if [[ -n "$scenarios" ]]; then
    step "Scenarios on the real model"
    uv run python scripts/run_scenarios.py --judge || echo "Some scenarios failed: see above (the server starts anyway)."
  fi
  step "Starting the server: open http://127.0.0.1:8080 (Ctrl+C to stop)"
  uv run python -m observability
}

case "${1:---start}" in
  --start|--rebuild|--scenarios) [[ "${1:-}" == "--start" ]] && shift; start "$@" ;;
  setup)     shift; setup "$@" ;;
  test)      shift; run_tests "$@" ;;
  scenarios) shift; uv run python scripts/run_scenarios.py "$@" ;;
  run)       python3 scripts/setup_env.py --check; uv run python -m observability ;;
  mock)      shift; uv run python -m mock_gateway "$@" ;;
  deploy)    ./deploy.sh ;;
  validate)  shift; uv run python scripts/validate_observability.py "$@" ;;
  all)       shift; setup; run_tests; uv run python scripts/run_scenarios.py "$@" ;;
  help|-h|--help) sed -n '2,23p' "$0" | sed 's/^# \{0,1\}//' ;;
  *) die "Unknown command '$1'. Try: setup, test, scenarios, run, mock, deploy, validate, all" ;;
esac
