#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

PORT=8080
DO_BUILD=0
NO_OPEN=0
AUTO_UPDATE="${SPECTRA_AUTO_UPDATE:-1}"

usage() {
  cat <<'EOF'
Usage:
  ./spectra start [--port 8080] [--build] [--no-open] [--no-update]
  ./spectra stop
  ./spectra logs
  ./spectra status

Commands:
  start   Auto-update if safe, then start Spectra with Docker Compose in background
  stop    Stop Spectra containers
  logs    Follow Spectra logs
  status  Show container status
EOF
}

require_docker() {
  if ! command -v docker >/dev/null 2>&1; then
    echo "Error: docker command not found. Install Docker Desktop / Docker Engine first." >&2
    exit 1
  fi
  if docker info >/dev/null 2>&1; then
    return
  fi

  # macOS: try to auto-start Docker Desktop
  if [[ "$(uname -s)" == "Darwin" ]] && command -v open >/dev/null 2>&1; then
    echo "Docker daemon not running. Trying to start Docker Desktop..."
    open -a Docker >/dev/null 2>&1 || true

    # Wait up to 60s for daemon readiness
    for _ in {1..60}; do
      if docker info >/dev/null 2>&1; then
        echo "Docker Desktop is ready."
        return
      fi
      sleep 1
    done
  fi

  echo "Error: Docker daemon is not running. Start Docker Desktop and retry." >&2
  exit 1
}

open_browser() {
  local url="$1"
  if command -v open >/dev/null 2>&1; then
    open "$url" >/dev/null 2>&1 || true
  elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$url" >/dev/null 2>&1 || true
  elif command -v start >/dev/null 2>&1; then
    start "$url" >/dev/null 2>&1 || true
  fi
}

maybe_update_repo() {
  if [[ "${AUTO_UPDATE}" -eq 0 ]]; then
    return
  fi

  if ! command -v git >/dev/null 2>&1; then
    return
  fi

  if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    return
  fi

  if ! git diff --quiet --ignore-submodules -- || ! git diff --cached --quiet --ignore-submodules --; then
    echo "Local git changes detected. Skipping auto-update."
    return
  fi

  local branch upstream remote counts ahead behind
  branch="$(git branch --show-current 2>/dev/null || true)"
  if [[ -z "${branch}" ]]; then
    return
  fi

  upstream="$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null || true)"
  if [[ -z "${upstream}" ]]; then
    return
  fi

  remote="${upstream%%/*}"
  if ! git fetch --quiet "${remote}"; then
    echo "Could not reach ${remote}. Skipping auto-update."
    return
  fi

  counts="$(git rev-list --left-right --count HEAD...${upstream} 2>/dev/null || true)"
  ahead="${counts%% *}"
  behind="${counts##* }"
  ahead="${ahead:-0}"
  behind="${behind:-0}"

  if [[ "${ahead}" -gt 0 && "${behind}" -gt 0 ]]; then
    echo "Local branch has diverged from ${upstream}. Skipping auto-update."
    return
  fi

  if [[ "${behind}" -gt 0 ]]; then
    echo "Updating Spectra from ${upstream}..."
    if git pull --ff-only; then
      DO_BUILD=1
    else
      echo "Auto-update failed. Continuing with local checkout."
    fi
  fi
}

cmd="${1:-}"
if [[ -z "$cmd" ]]; then
  usage
  exit 1
fi
shift || true

case "$cmd" in
  start)
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --port)
          PORT="${2:-}"
          shift 2
          ;;
        --no-build)
          DO_BUILD=0
          shift
          ;;
        --build)
          DO_BUILD=1
          shift
          ;;
        --no-open)
          NO_OPEN=1
          shift
          ;;
        --no-update)
          AUTO_UPDATE=0
          shift
          ;;
        -h|--help)
          usage
          exit 0
          ;;
        *)
          echo "Unknown option: $1" >&2
          usage
          exit 1
          ;;
      esac
    done

    require_docker
    maybe_update_repo

    args=(compose up -d)
    if [[ "$DO_BUILD" -eq 1 ]]; then
      args+=(--build)
    fi
    SPECTRA_PORT="$PORT" docker "${args[@]}"

    url="http://localhost:${PORT}"
    echo "Spectra is starting on ${url}"
    if [[ "$NO_OPEN" -eq 0 ]]; then
      sleep 1
      open_browser "$url"
      echo "Browser opened."
    fi
    ;;

  stop)
    require_docker
    docker compose down
    ;;

  logs)
    require_docker
    docker compose logs -f
    ;;

  status)
    require_docker
    docker compose ps
    ;;

  -h|--help|help)
    usage
    ;;

  *)
    echo "Unknown command: $cmd" >&2
    usage
    exit 1
    ;;
esac
