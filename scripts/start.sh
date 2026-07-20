#!/usr/bin/env bash
# =============================================================================
# Container entrypoint — prepare Hermes home and start the SQL Agent API
# =============================================================================
set -euo pipefail

log() {
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] [start] $*"
}

APP_HOME="${APP_HOME:-/app}"
HERMES_HOME="${HERMES_HOME:-/home/hermes/.hermes}"
HR_APP_ROOT="${HR_APP_ROOT:-$APP_HOME}"

export APP_HOME HERMES_HOME HR_APP_ROOT
export HERMES_ENABLE_PROJECT_PLUGINS="${HERMES_ENABLE_PROJECT_PLUGINS:-true}"
export HERMES_TUI="${HERMES_TUI:-0}"
export HERMES_SKIP_DESKTOP="${HERMES_SKIP_DESKTOP:-1}"
export SYSTEM_PROMPT_PATH="${SYSTEM_PROMPT_PATH:-$APP_HOME/prompts/system_prompt.md}"
export LOG_DIR="${LOG_DIR:-$APP_HOME/logs}"
export PYTHONUNBUFFERED=1
export HR_ENABLED_TOOLSETS="${HR_ENABLED_TOOLSETS:-sql}"

log "SQL Agent container starting"
log "APP_HOME=$APP_HOME HERMES_HOME=$HERMES_HOME"
log "SYSTEM_PROMPT_PATH=$SYSTEM_PROMPT_PATH"
log "HR_MODEL=${HR_MODEL:-}"
log "HR_ENABLED_TOOLSETS=$HR_ENABLED_TOOLSETS"
log "DATABASE_URL set: $([ -n "${DATABASE_URL:-}${HR_DATABASE_URL:-}${POSTGRES_URL:-}" ] && echo yes || echo no)"
log "TZ=${TZ:-UTC}"

# --- Directories -------------------------------------------------------------
mkdir -p \
  "$HERMES_HOME/plugins" \
  "$HERMES_HOME/logs" \
  "$LOG_DIR"

# --- Hermes config (do not overwrite user-customized volume copy blindly) ----
if [[ ! -f "$HERMES_HOME/config.yaml" ]]; then
  if [[ -f "$APP_HOME/config/hermes_config.yaml" ]]; then
    cp "$APP_HOME/config/hermes_config.yaml" "$HERMES_HOME/config.yaml"
    log "Installed default hermes config.yaml"
  else
    log "WARNING: no hermes config found"
  fi
else
  log "Using existing $HERMES_HOME/config.yaml"
  # Ensure sql toolset is enabled if config is stale from JSON-agent era
  if grep -q "enabled:" "$HERMES_HOME/config.yaml" 2>/dev/null; then
    if ! grep -qE '^\s*- sql\s*$' "$HERMES_HOME/config.yaml" 2>/dev/null; then
      log "NOTE: ensure tools.cli.enabled includes 'sql' in $HERMES_HOME/config.yaml"
    fi
  fi
fi

# --- SQL plugin (proper Hermes extension; refresh on every start) -------------
PLUGIN_SRC="$APP_HOME/plugins/hr-employee"
PLUGIN_DST="$HERMES_HOME/plugins/hr-employee"
if [[ -d "$PLUGIN_SRC" ]]; then
  rm -rf "$PLUGIN_DST"
  cp -a "$PLUGIN_SRC" "$PLUGIN_DST"
  log "Installed Hermes plugin hr-employee (SQL tools) → $PLUGIN_DST"
else
  log "WARNING: plugin source missing at $PLUGIN_SRC"
fi

if [[ -f "$HERMES_HOME/config.yaml" ]]; then
  if ! grep -q "hr-employee" "$HERMES_HOME/config.yaml" 2>/dev/null; then
    log "NOTE: ensure plugins.enabled includes hr-employee in config.yaml"
  fi
fi

# --- Validate prompt ---------------------------------------------------------
if [[ ! -f "$SYSTEM_PROMPT_PATH" ]]; then
  log "ERROR: system prompt not found at $SYSTEM_PROMPT_PATH"
  exit 1
fi
log "system prompt present ($(wc -c < "$SYSTEM_PROMPT_PATH" | tr -d ' ') bytes)"

if [[ -z "${DATABASE_URL:-}" && -z "${HR_DATABASE_URL:-}" && -z "${POSTGRES_URL:-}" ]]; then
  log "WARNING: DATABASE_URL not set — /ready will be 503 until configured"
fi

# --- API key sanity ----------------------------------------------------------
if [[ -z "${OPENROUTER_API_KEY:-}" && -z "${OPENAI_API_KEY:-}" && -z "${ANTHROPIC_API_KEY:-}" && -z "${HR_API_KEY:-}" ]]; then
  log "WARNING: no LLM API key set. Chat will fail until configured."
fi

if command -v pip >/dev/null 2>&1 && [[ -f "$APP_HOME/pyproject.toml" ]]; then
  log "Python packages preinstalled in image"
fi

cd "$APP_HOME"
log "Launching SQL Agent API (python -m app.main)"
exec python -m app.main
