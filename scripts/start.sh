#!/usr/bin/env bash
# Container entrypoint — clean shell
set -euo pipefail

log() {
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] [start] $*"
}

APP_HOME="${APP_HOME:-/app}"
export APP_HOME
export LOG_DIR="${LOG_DIR:-$APP_HOME/logs}"
export PYTHONUNBUFFERED=1

mkdir -p "$LOG_DIR"

log "AI Agents clean shell starting"
log "APP_HOME=$APP_HOME"

cd "$APP_HOME"
exec python -m app.main
