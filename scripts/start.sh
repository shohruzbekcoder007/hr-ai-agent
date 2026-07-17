#!/usr/bin/env bash
# =============================================================================
# Container entrypoint — prepare Hermes home and start the HR Agent API
# =============================================================================
set -euo pipefail

log() {
  # Structured-ish startup line (also captured by container logging)
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] [start] $*"
}

APP_HOME="${APP_HOME:-/app}"
HERMES_HOME="${HERMES_HOME:-/home/hermes/.hermes}"
HR_APP_ROOT="${HR_APP_ROOT:-$APP_HOME}"

export APP_HOME HERMES_HOME HR_APP_ROOT
export HERMES_ENABLE_PROJECT_PLUGINS="${HERMES_ENABLE_PROJECT_PLUGINS:-true}"
export HERMES_TUI="${HERMES_TUI:-0}"
export HERMES_SKIP_DESKTOP="${HERMES_SKIP_DESKTOP:-1}"
export EMPLOYEES_JSON_PATH="${EMPLOYEES_JSON_PATH:-$APP_HOME/data/employees.json}"
export SYSTEM_PROMPT_PATH="${SYSTEM_PROMPT_PATH:-$APP_HOME/prompts/system_prompt.md}"
export LOG_DIR="${LOG_DIR:-$APP_HOME/logs}"
export PYTHONUNBUFFERED=1

log "HR AI Agent container starting"
log "APP_HOME=$APP_HOME HERMES_HOME=$HERMES_HOME"
log "EMPLOYEES_JSON_PATH=$EMPLOYEES_JSON_PATH"
log "HR_MODEL=${HR_MODEL:-}"
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
fi

# --- HR plugin (proper Hermes extension; refresh on every start) -------------
PLUGIN_SRC="$APP_HOME/plugins/hr-employee"
PLUGIN_DST="$HERMES_HOME/plugins/hr-employee"
if [[ -d "$PLUGIN_SRC" ]]; then
  rm -rf "$PLUGIN_DST"
  cp -a "$PLUGIN_SRC" "$PLUGIN_DST"
  log "Installed Hermes plugin hr-employee → $PLUGIN_DST"
else
  log "WARNING: plugin source missing at $PLUGIN_SRC"
fi

# Ensure plugin is enabled in config.yaml
if [[ -f "$HERMES_HOME/config.yaml" ]]; then
  if ! grep -q "hr-employee" "$HERMES_HOME/config.yaml" 2>/dev/null; then
    log "NOTE: ensure plugins.enabled includes hr-employee in config.yaml"
  fi
fi

# --- Validate knowledge base -------------------------------------------------
if [[ ! -f "$EMPLOYEES_JSON_PATH" ]]; then
  log "ERROR: employees.json not found at $EMPLOYEES_JSON_PATH"
  exit 1
fi
log "employees.json present ($(wc -c < "$EMPLOYEES_JSON_PATH" | tr -d ' ') bytes)"

if [[ ! -f "$SYSTEM_PROMPT_PATH" ]]; then
  log "ERROR: system prompt not found at $SYSTEM_PROMPT_PATH"
  exit 1
fi

# --- API key sanity (warn only; /ready may still work for tool-only tests) ---
if [[ -z "${OPENROUTER_API_KEY:-}" && -z "${OPENAI_API_KEY:-}" && -z "${ANTHROPIC_API_KEY:-}" && -z "${HR_API_KEY:-}" ]]; then
  log "WARNING: no LLM API key set (OPENROUTER_API_KEY / OPENAI_API_KEY / ANTHROPIC_API_KEY). Chat will fail until configured."
fi

# --- Optional: reinstall project in editable mode if venv present ------------
if command -v pip >/dev/null 2>&1 && [[ -f "$APP_HOME/pyproject.toml" ]]; then
  # Already installed in image; skip reinstall to keep startup fast
  log "Python packages preinstalled in image"
fi

cd "$APP_HOME"
log "Launching HR Agent API (python -m app.main)"
exec python -m app.main
