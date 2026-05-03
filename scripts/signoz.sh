#!/usr/bin/env bash
#
# Manage a sibling SigNoz stack (Datadog-style local APM) for this repo.
#
# SigNoz is a self-hosted, OTel-native observability platform: APM
# service map, trace explorer, log explorer with trace correlation,
# metrics dashboards, exception tracking, alerts. We run their
# upstream docker-compose unmodified so we get free upgrades — this
# script just clones it once and wraps `docker compose` invocations.
#
# Layout:
#   ${OPENCLAW_SIGNOZ_HOME:-${HOME}/.signoz/signoz} is the upstream
#   SigNoz repo checkout. The compose runs from
#   ${OPENCLAW_SIGNOZ_HOME}/deploy/docker.
#
# Why a separate stack instead of vendoring into our compose?
#   - SigNoz needs ~6 services (ClickHouse, Zookeeper, query-service,
#     UI, collector, migrator) plus config files we don't want to
#     maintain.
#   - Their canonical compose ships those configs in sibling dirs.
#   - Cloning their repo gives us free upgrades via `git pull`.
#
# Usage:
#   scripts/signoz.sh up      # bootstrap (clone if needed) + docker compose up -d
#   scripts/signoz.sh down    # docker compose down (preserves data volumes)
#   scripts/signoz.sh status  # docker compose ps
#   scripts/signoz.sh logs    # docker compose logs -f
#   scripts/signoz.sh open    # open the UI in the default browser
#   scripts/signoz.sh nuke    # docker compose down -v (DELETES data)

set -euo pipefail

SIGNOZ_HOME="${OPENCLAW_SIGNOZ_HOME:-${HOME}/.signoz/signoz}"
SIGNOZ_REPO="${OPENCLAW_SIGNOZ_REPO:-https://github.com/SigNoz/signoz.git}"
SIGNOZ_REF="${OPENCLAW_SIGNOZ_REF:-main}"
COMPOSE_DIR="${SIGNOZ_HOME}/deploy/docker"
COMPOSE_ARGS=(compose -f "${COMPOSE_DIR}/docker-compose.yaml")

log()  { printf "\033[1;34m[signoz]\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m[warn]\033[0m  %s\n" "$*"; }
die()  { printf "\033[1;31m[fatal]\033[0m %s\n" "$*" >&2; exit 1; }

ensure_repo() {
  if [[ -d "${SIGNOZ_HOME}/.git" ]]; then
    return 0
  fi
  command -v git >/dev/null 2>&1 || die "git is required to bootstrap SigNoz"
  log "cloning SigNoz to ${SIGNOZ_HOME}"
  mkdir -p "$(dirname "${SIGNOZ_HOME}")"
  git clone --depth=1 --branch "${SIGNOZ_REF}" "${SIGNOZ_REPO}" "${SIGNOZ_HOME}"
}

ensure_docker_running() {
  command -v docker >/dev/null 2>&1 || die "docker not installed"
  docker info >/dev/null 2>&1 || die "docker daemon not reachable — is Docker Desktop running?"
}

ensure_no_port_conflicts() {
  # The signoz-otel-collector binds to 4317/4318. Anything else on the
  # same ports (e.g. a leftover Jaeger from before the OTel refactor)
  # will silently break ingestion.
  for port in 4317 4318 8080; do
    if lsof -nP -iTCP:${port} -sTCP:LISTEN >/dev/null 2>&1; then
      local pid
      pid=$(lsof -nP -iTCP:${port} -sTCP:LISTEN 2>/dev/null | awk 'NR==2 {print $2}')
      warn "port ${port} already in use by pid ${pid:-unknown} — SigNoz may fail to bind"
    fi
  done
}

cmd_up() {
  ensure_docker_running
  ensure_repo
  ensure_no_port_conflicts
  log "starting SigNoz (${COMPOSE_DIR})"
  cd "${COMPOSE_DIR}"
  docker "${COMPOSE_ARGS[@]}" up -d --remove-orphans
  log "waiting for query-service to become healthy (up to 180s; first run pulls ~3GB of images)"
  for _ in $(seq 1 180); do
    if curl -fsS -m 2 http://localhost:8080/api/v1/health >/dev/null 2>&1; then
      log "✅ SigNoz is up — open http://localhost:8080"
      printf "\n"
      printf "  \033[1;33mFIRST-RUN ONLY:\033[0m on the very first boot, SigNoz needs an admin user +\n"
      printf "  organization before it accepts traces. Until you complete that one-time\n"
      printf "  signup, the otel-collector resets OTLP connections (you'll see\n"
      printf "  'cannot create agent without orgId' in 'signoz' container logs).\n"
      printf "\n"
      printf "    1. Open http://localhost:8080 in your browser.\n"
      printf "    2. Create the admin user + org (local-only, never leaves your laptop).\n"
      printf "    3. Then 'make deploy' your agents (OPENCLAW_OTEL_ENABLED=true,\n"
      printf "       OPENCLAW_OTLP_ENDPOINT=http://localhost:4318/v1/traces) and\n"
      printf "       traces will start flowing.\n"
      printf "\n"
      return 0
    fi
    sleep 1
  done
  warn "query-service did not respond on :8080 within 180s; check 'scripts/signoz.sh logs'"
}

cmd_down() {
  ensure_docker_running
  [[ -d "${COMPOSE_DIR}" ]] || die "SigNoz not bootstrapped — run 'scripts/signoz.sh up' first"
  cd "${COMPOSE_DIR}"
  log "stopping SigNoz (data volumes preserved)"
  docker "${COMPOSE_ARGS[@]}" down
}

cmd_status() {
  [[ -d "${COMPOSE_DIR}" ]] || die "SigNoz not bootstrapped — run 'scripts/signoz.sh up' first"
  cd "${COMPOSE_DIR}"
  docker "${COMPOSE_ARGS[@]}" ps
}

cmd_logs() {
  [[ -d "${COMPOSE_DIR}" ]] || die "SigNoz not bootstrapped — run 'scripts/signoz.sh up' first"
  cd "${COMPOSE_DIR}"
  docker "${COMPOSE_ARGS[@]}" logs -f --tail=100 "${@:-}"
}

cmd_open() {
  local url="http://localhost:8080"
  if command -v open >/dev/null 2>&1; then
    open "${url}"
  elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "${url}"
  else
    log "open ${url} in your browser"
  fi
}

cmd_nuke() {
  ensure_docker_running
  [[ -d "${COMPOSE_DIR}" ]] || die "SigNoz not bootstrapped"
  cd "${COMPOSE_DIR}"
  warn "DELETING all SigNoz data (ClickHouse, sqlite, zookeeper)"
  docker "${COMPOSE_ARGS[@]}" down -v
}

cmd_update() {
  ensure_repo
  log "pulling latest SigNoz from ${SIGNOZ_REF}"
  git -C "${SIGNOZ_HOME}" fetch --tags origin "${SIGNOZ_REF}" || git -C "${SIGNOZ_HOME}" fetch --tags origin
  if git -C "${SIGNOZ_HOME}" rev-parse --verify --quiet "origin/${SIGNOZ_REF}" >/dev/null; then
    git -C "${SIGNOZ_HOME}" reset --hard "origin/${SIGNOZ_REF}"
  else
    git -C "${SIGNOZ_HOME}" reset --hard "${SIGNOZ_REF}"
  fi
  log "run 'scripts/signoz.sh up' to apply"
}

case "${1:-up}" in
  up)     cmd_up ;;
  down)   cmd_down ;;
  status) cmd_status ;;
  logs)   shift; cmd_logs "$@" ;;
  open)   cmd_open ;;
  nuke)   cmd_nuke ;;
  update) cmd_update ;;
  *)
    cat <<EOF
usage: scripts/signoz.sh [up|down|status|logs|open|nuke|update]

Commands:
  up      bootstrap if needed, then docker compose up -d
  down    stop containers (volumes preserved)
  status  docker compose ps
  logs    follow logs (optionally pass a service name)
  open    open the UI in the default browser
  nuke    docker compose down -v (DELETES ALL DATA)
  update  git pull the upstream SigNoz repo (run 'up' afterward)

Environment:
  OPENCLAW_SIGNOZ_HOME  defaults to ~/.signoz/signoz (the local checkout)
  OPENCLAW_SIGNOZ_REPO  defaults to https://github.com/SigNoz/signoz.git
  OPENCLAW_SIGNOZ_REF   defaults to main (use a release tag in production)
EOF
    exit 1 ;;
esac
