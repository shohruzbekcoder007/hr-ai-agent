#!/usr/bin/env bash
# Container entrypoint — Hermes host + SQL bridge plugin
set -euo pipefail

log() {
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] [start] $*"
}

APP_HOME="${APP_HOME:-/app}"
HERMES_HOME="${HERMES_HOME:-/home/appuser/.hermes}"
export APP_HOME HERMES_HOME
export HR_APP_ROOT="${HR_APP_ROOT:-$APP_HOME}"
export LOG_DIR="${LOG_DIR:-$APP_HOME/logs}"
export PYTHONUNBUFFERED=1
export HERMES_ENABLE_PROJECT_PLUGINS="${HERMES_ENABLE_PROJECT_PLUGINS:-true}"
export SYSTEM_PROMPT_PATH="${SYSTEM_PROMPT_PATH:-$APP_HOME/prompts/sql_agent_system.md}"
export HERMES_SYSTEM_PROMPT_PATH="${HERMES_SYSTEM_PROMPT_PATH:-$APP_HOME/prompts/hermes_coordinator.md}"

mkdir -p "$LOG_DIR" "$HERMES_HOME/plugins" "$HERMES_HOME/logs"

# Hermes config
if [[ ! -f "$HERMES_HOME/config.yaml" ]]; then
  if [[ -f "$APP_HOME/config/hermes_config.yaml" ]]; then
    cp "$APP_HOME/config/hermes_config.yaml" "$HERMES_HOME/config.yaml"
    log "Installed hermes config.yaml"
  fi
fi

# sql-bridge plugin
PLUGIN_SRC="$APP_HOME/plugins/sql-bridge"
PLUGIN_DST="$HERMES_HOME/plugins/sql-bridge"
if [[ -d "$PLUGIN_SRC" ]]; then
  rm -rf "$PLUGIN_DST"
  cp -a "$PLUGIN_SRC" "$PLUGIN_DST"
  log "Installed Hermes plugin sql-bridge"
fi

log "Starting Hermes-host SQL service"
log "HERMES_HOME=$HERMES_HOME LLM_MODEL=${LLM_MODEL:-} DATABASE_URL set=$([ -n "${DATABASE_URL:-}" ] && echo yes || echo no)"

cd "$APP_HOME"
exec python -m app.main
