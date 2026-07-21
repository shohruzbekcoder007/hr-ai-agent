# =============================================================================
# LangChain SQLAgent service — multi-stage, non-root
# Flow: Chat Input → Prompt Template → SQLAgent → Chat Output
# =============================================================================

FROM python:3.12-slim-bookworm AS builder

ARG DEBIAN_FRONTEND=noninteractive

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /build

COPY requirements.txt pyproject.toml README.md ./
COPY agents ./agents
COPY app ./app

# Install deps from requirements first (explicit), then package
RUN pip install --upgrade pip setuptools wheel \
    && pip install -r requirements.txt \
    && pip install .

# -----------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime

ARG DEBIAN_FRONTEND=noninteractive
ARG APP_UID=10001
ARG APP_GID=10001

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    APP_HOME=/app \
    LOG_DIR=/app/logs \
    APP_HOST=0.0.0.0 \
    APP_PORT=8080 \
    TZ=UTC

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        tini \
        tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid "${APP_GID}" appuser \
    && useradd --uid "${APP_UID}" --gid appuser --create-home --shell /usr/sbin/nologin appuser

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app

COPY --chown=appuser:appuser agents ./agents
COPY --chown=appuser:appuser app ./app
COPY --chown=appuser:appuser prompts ./prompts
COPY --chown=appuser:appuser config ./config
COPY --chown=appuser:appuser scripts ./scripts
COPY --chown=appuser:appuser requirements.txt pyproject.toml README.md ./

RUN mkdir -p /app/logs \
    && chown -R appuser:appuser /app/logs \
    && chmod +x /app/scripts/*.sh

USER appuser

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["/app/scripts/healthcheck.sh"]

ENTRYPOINT ["/usr/bin/tini", "--", "/app/scripts/start.sh"]
CMD []
