#!/usr/bin/env bash
# =============================================================================
# Docker HEALTHCHECK + manual readiness probe
# Exit 0 = healthy, non-zero = unhealthy
# =============================================================================
set -euo pipefail

URL="${HEALTHCHECK_URL:-http://127.0.0.1:8080/health}"
READY_URL="${READY_URL:-http://127.0.0.1:8080/ready}"
TIMEOUT="${HEALTHCHECK_TIMEOUT_SECONDS:-5}"
CHECK_READY="${HEALTHCHECK_CHECK_READY:-1}"

# Prefer curl; fall back to python if curl missing
http_get() {
  local target="$1"
  if command -v curl >/dev/null 2>&1; then
    curl -fsS --max-time "$TIMEOUT" "$target"
  else
    python - "$target" "$TIMEOUT" <<'PY'
import sys, urllib.request
url, timeout = sys.argv[1], float(sys.argv[2])
with urllib.request.urlopen(url, timeout=timeout) as resp:
    body = resp.read()
    if resp.status >= 400:
        raise SystemExit(1)
    sys.stdout.buffer.write(body)
PY
  fi
}

# 1) Liveness
if ! http_get "$URL" >/dev/null; then
  echo "healthcheck: liveness failed for $URL" >&2
  exit 1
fi

# 2) Readiness (agent + PostgreSQL)
if [[ "$CHECK_READY" == "1" ]]; then
  if ! http_get "$READY_URL" >/dev/null; then
    echo "healthcheck: readiness failed for $READY_URL" >&2
    exit 1
  fi
fi

exit 0
