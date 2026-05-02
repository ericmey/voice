#!/usr/bin/env bash
#
# Install + manage a host-side OpenTelemetry Collector for openclaw-livekit.
#
# What it does:
#   1. Downloads otelcol-contrib (the upstream "contrib" build) from GitHub
#      releases into ${OPENCLAW_OTELCOL_HOME:-~/.openclaw/otel-collector/bin}.
#      Pinned to OTELCOL_VERSION below; bump that to upgrade.
#   2. Renders config/otel-collector/config.yaml.template into
#      ${OPENCLAW_OTELCOL_HOME}/config.yaml with the operator's paths.
#   3. Renders config/launchd/ai.openclaw.otel-collector.plist.template into
#      ~/Library/LaunchAgents/ai.openclaw.otel-collector.plist.
#   4. Bootstraps the launchd job (or kicks it if already loaded).
#
# Why bypass Homebrew?
#   The OTel project does not maintain a Homebrew formula for the collector;
#   the official install path on macOS is the GitHub releases tarball. Using
#   that gets us:
#     - Reproducible version pinning (OTELCOL_VERSION in this file)
#     - All ~80 contrib receivers/exporters bundled (we use hostmetrics,
#       docker_stats, httpcheck, filelog, otlphttp out of the box)
#     - SBOM + signature artifacts for supply-chain checks if needed
#
# Usage:
#   scripts/install-host-otel-collector.sh                # install + start
#   scripts/install-host-otel-collector.sh --reinstall    # force-redownload binary
#   scripts/install-host-otel-collector.sh --restart      # render + kick launchd
#   scripts/install-host-otel-collector.sh --uninstall    # bootout + remove plist
#   scripts/install-host-otel-collector.sh --status       # print state
#   scripts/install-host-otel-collector.sh --dry-run      # show what would happen

set -euo pipefail

OTELCOL_VERSION="${OPENCLAW_OTELCOL_VERSION:-0.151.0}"
OTELCOL_HOME="${OPENCLAW_OTELCOL_HOME:-${HOME}/.openclaw/otel-collector}"
OTELCOL_BIN_DIR="${OTELCOL_HOME}/bin"
OTELCOL_BIN="${OTELCOL_BIN_DIR}/otelcol-contrib"
OTELCOL_CONFIG="${OTELCOL_HOME}/config.yaml"
OTELCOL_LOGS="${OPENCLAW_OTELCOL_LOGS:-${HOME}/.openclaw/logs}"
PLIST_LABEL="ai.openclaw.otel-collector"
PLIST_DEST="${HOME}/Library/LaunchAgents/${PLIST_LABEL}.plist"

# Resolve repo root from this script's location (../). Allows the script
# to be run from anywhere.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
CONFIG_TEMPLATE="${REPO_ROOT}/config/otel-collector/config.yaml.template"
PLIST_TEMPLATE="${REPO_ROOT}/config/launchd/${PLIST_LABEL}.plist.template"

# Sane defaults for the log directories we tail.
OPENCLAW_GATEWAY_LOGS_DIR="${OPENCLAW_GATEWAY_LOGS:-${HOME}/.openclaw/logs}"
LIVEKIT_VOICE_LOGS_DIR="${LIVEKIT_VOICE_LOGS:-${REPO_ROOT}/logs/voice}"

DRY_RUN="false"
ACTION="install"
for arg in "$@"; do
    case "${arg}" in
        --reinstall) ACTION="reinstall" ;;
        --restart)   ACTION="restart" ;;
        --uninstall) ACTION="uninstall" ;;
        --status)    ACTION="status" ;;
        --dry-run)   DRY_RUN="true" ;;
        -h|--help)
            sed -n '2,/^set -euo/p' "$0" | sed 's/^# \{0,1\}//;/^set -euo/d'
            exit 0
            ;;
        *)
            printf "[install] unknown arg: %s\n" "${arg}" >&2
            exit 2
            ;;
    esac
done

log()   { printf "\033[1;34m[otelcol]\033[0m %s\n" "$*"; }
warn()  { printf "\033[1;33m[warn]   \033[0m %s\n" "$*"; }
err()   { printf "\033[1;31m[err]    \033[0m %s\n" "$*" >&2; }
ok()    { printf "\033[1;32m[ ok ]   \033[0m %s\n" "$*"; }

run() {
    if [[ "${DRY_RUN}" == "true" ]]; then
        printf "  + %s\n" "$*"
    else
        "$@"
    fi
}

require_macos() {
    if [[ "$(uname)" != "Darwin" ]]; then
        err "this script targets macOS only (got: $(uname))"
        exit 1
    fi
}

detect_arch() {
    local m
    m="$(uname -m)"
    case "${m}" in
        arm64) echo "darwin_arm64" ;;
        x86_64) echo "darwin_amd64" ;;
        *)
            err "unsupported macOS architecture: ${m}"
            exit 1
            ;;
    esac
}

ensure_binary() {
    local force="${1:-false}"
    if [[ -x "${OTELCOL_BIN}" && "${force}" != "true" ]]; then
        local installed
        installed="$("${OTELCOL_BIN}" --version 2>/dev/null | awk '{print $3}' | sed 's/^v//' || true)"
        if [[ "${installed}" == "${OTELCOL_VERSION}" ]]; then
            ok "otelcol-contrib ${installed} already installed at ${OTELCOL_BIN}"
            return 0
        else
            warn "found otelcol-contrib ${installed:-unknown}, target ${OTELCOL_VERSION} — replacing"
        fi
    fi

    local arch tarball url tmpdir
    arch="$(detect_arch)"
    tarball="otelcol-contrib_${OTELCOL_VERSION}_${arch}.tar.gz"
    url="https://github.com/open-telemetry/opentelemetry-collector-releases/releases/download/v${OTELCOL_VERSION}/${tarball}"
    tmpdir="$(mktemp -d)"

    log "downloading ${tarball} (~88MB)"
    run mkdir -p "${OTELCOL_BIN_DIR}"
    if [[ "${DRY_RUN}" == "false" ]]; then
        curl -fL --progress-bar -o "${tmpdir}/${tarball}" "${url}"
        tar -xzf "${tmpdir}/${tarball}" -C "${tmpdir}"
        install -m 0755 "${tmpdir}/otelcol-contrib" "${OTELCOL_BIN}"
        rm -rf "${tmpdir}"
        ok "installed otelcol-contrib v${OTELCOL_VERSION} → ${OTELCOL_BIN}"
    fi
}

render_config() {
    if [[ ! -f "${CONFIG_TEMPLATE}" ]]; then
        err "config template not found: ${CONFIG_TEMPLATE}"
        exit 1
    fi
    log "rendering ${CONFIG_TEMPLATE}"
    log "         → ${OTELCOL_CONFIG}"
    run mkdir -p "${OTELCOL_HOME}" "${OTELCOL_LOGS}"

    if [[ "${DRY_RUN}" == "false" ]]; then
        # Use sed for variable substitution. Each {{KEY}} → resolved value.
        # Slashes in paths are fine — we use `|` as the sed separator.
        sed \
            -e "s|{{HOSTNAME}}|$(hostname)|g" \
            -e "s|{{HOME}}|${HOME}|g" \
            -e "s|{{OPENCLAW_GATEWAY_LOGS}}|${OPENCLAW_GATEWAY_LOGS_DIR}|g" \
            -e "s|{{LIVEKIT_VOICE_LOGS}}|${LIVEKIT_VOICE_LOGS_DIR}|g" \
            "${CONFIG_TEMPLATE}" > "${OTELCOL_CONFIG}"
        ok "rendered config → ${OTELCOL_CONFIG}"
    fi
}

render_plist() {
    if [[ ! -f "${PLIST_TEMPLATE}" ]]; then
        err "plist template not found: ${PLIST_TEMPLATE}"
        exit 1
    fi
    log "rendering ${PLIST_TEMPLATE}"
    log "         → ${PLIST_DEST}"

    if [[ "${DRY_RUN}" == "false" ]]; then
        mkdir -p "$(dirname "${PLIST_DEST}")"
        sed \
            -e "s|{{OTELCOL_BIN}}|${OTELCOL_BIN}|g" \
            -e "s|{{OTELCOL_CONFIG}}|${OTELCOL_CONFIG}|g" \
            -e "s|{{OTELCOL_LOGS}}|${OTELCOL_LOGS}|g" \
            -e "s|{{HOME}}|${HOME}|g" \
            -e "s|{{HOSTNAME}}|$(hostname)|g" \
            -e "s|{{REPO_ROOT}}|${REPO_ROOT}|g" \
            -e "s|{{LIVEKIT_VOICE_LOGS}}|${LIVEKIT_VOICE_LOGS_DIR}|g" \
            "${PLIST_TEMPLATE}" > "${PLIST_DEST}"
        ok "rendered plist → ${PLIST_DEST}"
    fi
}

bootstrap_launchd() {
    log "bootstrapping launchd job ${PLIST_LABEL}"
    if [[ "${DRY_RUN}" == "true" ]]; then
        printf "  + launchctl bootout gui/$(id -u) ${PLIST_DEST} || true\n"
        printf "  + launchctl bootstrap gui/$(id -u) ${PLIST_DEST}\n"
        return 0
    fi
    # bootout-then-bootstrap is the canonical idempotent pattern; bootout
    # is allowed to fail (job not loaded yet on first run).
    launchctl bootout "gui/$(id -u)" "${PLIST_DEST}" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "${PLIST_DEST}"
    ok "launchd job loaded"
}

kickstart_launchd() {
    log "kickstart -k gui/$(id -u)/${PLIST_LABEL}"
    if [[ "${DRY_RUN}" == "true" ]]; then return 0; fi
    launchctl kickstart -k "gui/$(id -u)/${PLIST_LABEL}"
    ok "kicked"
}

uninstall() {
    log "uninstalling ${PLIST_LABEL}"
    if [[ "${DRY_RUN}" == "true" ]]; then
        printf "  + launchctl bootout gui/$(id -u) ${PLIST_DEST}\n"
        printf "  + rm -f ${PLIST_DEST}\n"
        printf "  (binary + config + logs in ${OTELCOL_HOME} kept for safety)\n"
        return 0
    fi
    launchctl bootout "gui/$(id -u)" "${PLIST_DEST}" 2>/dev/null || true
    rm -f "${PLIST_DEST}"
    ok "removed plist; binary + config + logs preserved at ${OTELCOL_HOME}"
    ok "to fully purge: rm -rf ${OTELCOL_HOME}"
}

show_status() {
    printf "%-20s %s\n" "binary:"   "${OTELCOL_BIN}"
    if [[ -x "${OTELCOL_BIN}" ]]; then
        printf "%-20s %s\n" "version:"  "$("${OTELCOL_BIN}" --version 2>/dev/null || echo MISSING)"
    else
        printf "%-20s \033[1;33m%s\033[0m\n" "version:" "not installed"
    fi
    printf "%-20s %s\n" "config:"   "${OTELCOL_CONFIG}"
    printf "%-20s %s\n" "plist:"    "${PLIST_DEST}"
    printf "%-20s %s\n" "log dir:"  "${OTELCOL_LOGS}"
    printf "%-20s " "launchd state:"
    if launchctl list 2>/dev/null | grep -q "${PLIST_LABEL}"; then
        local pid
        pid="$(launchctl list 2>/dev/null | awk -v lbl="${PLIST_LABEL}" '$3==lbl {print $1}')"
        if [[ "${pid}" == "-" || -z "${pid}" ]]; then
            printf "\033[1;33mloaded but not running\033[0m\n"
        else
            printf "\033[1;32mrunning (pid=%s)\033[0m\n" "${pid}"
        fi
    else
        printf "\033[1;33mnot loaded\033[0m\n"
    fi
    if [[ -f "${OTELCOL_LOGS}/otel-collector.log" ]]; then
        printf "%-20s\n" "recent log lines:"
        tail -n 5 "${OTELCOL_LOGS}/otel-collector.log" 2>/dev/null | sed 's/^/  /'
    fi
}

require_macos

case "${ACTION}" in
    install)
        ensure_binary
        render_config
        render_plist
        bootstrap_launchd
        sleep 2
        show_status
        ;;
    reinstall)
        ensure_binary "true"
        render_config
        render_plist
        bootstrap_launchd
        sleep 2
        show_status
        ;;
    restart)
        render_config
        render_plist
        # bootstrap (not just kickstart) so any plist EnvironmentVariables
        # changes get picked up by launchd on the next process spawn.
        bootstrap_launchd
        sleep 3
        show_status
        ;;
    uninstall)
        uninstall
        ;;
    status)
        show_status
        ;;
esac
