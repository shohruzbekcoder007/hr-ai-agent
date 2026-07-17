# =============================================================================
# HR AI Agent — Production Dockerfile
# Architecture: Ubuntu 24.04 VM → Docker → this image → Hermes + HR Agent
# Multi-stage, non-root, clones Hermes from GitHub at build time.
# =============================================================================

# -----------------------------------------------------------------------------
# Stage 1: Builder — clone Hermes, install Python deps into a venv
# -----------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS builder

ARG HERMES_REPO=https://github.com/NousResearch/hermes-agent.git
ARG HERMES_REF=main
ARG DEBIAN_FRONTEND=noninteractive

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git \
        build-essential \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Virtualenv keeps the runtime stage clean
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /build

# Install Hermes Agent Framework from GitHub (do NOT rewrite Hermes)
RUN git clone --depth 1 --branch "${HERMES_REF}" "${HERMES_REPO}" /opt/hermes-agent \
    && pip install --upgrade pip setuptools wheel \
    && pip install /opt/hermes-agent

# Install this project's runtime dependencies + package (hr_tools mapping)
COPY requirements.txt pyproject.toml README.md ./
COPY agents ./agents
COPY app ./app
COPY tools ./tools
COPY plugins ./plugins
COPY prompts ./prompts
COPY data ./data
COPY config ./config
COPY scripts ./scripts

RUN pip install .

# -----------------------------------------------------------------------------
# Stage 2: Runtime — slim image, non-root user
# -----------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime

ARG DEBIAN_FRONTEND=noninteractive
ARG APP_UID=10001
ARG APP_GID=10001

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    APP_HOME=/app \
    HR_APP_ROOT=/app \
    HERMES_HOME=/home/hermes/.hermes \
    HERMES_ENABLE_PROJECT_PLUGINS=true \
    HERMES_TUI=0 \
    HERMES_SKIP_DESKTOP=1 \
    EMPLOYEES_JSON_PATH=/app/data/employees.json \
    SYSTEM_PROMPT_PATH=/app/prompts/system_prompt.md \
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
    && groupadd --gid "${APP_GID}" hermes \
    && useradd --uid "${APP_UID}" --gid hermes --create-home --shell /usr/sbin/nologin hermes

# Copy virtualenv + hermes source reference (for plugin/docs paths)
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /opt/hermes-agent /opt/hermes-agent

WORKDIR /app

# Application payload
COPY --chown=hermes:hermes agents ./agents
COPY --chown=hermes:hermes app ./app
COPY --chown=hermes:hermes tools ./tools
COPY --chown=hermes:hermes plugins ./plugins
COPY --chown=hermes:hermes prompts ./prompts
COPY --chown=hermes:hermes data ./data
COPY --chown=hermes:hermes config ./config
COPY --chown=hermes:hermes scripts ./scripts
COPY --chown=hermes:hermes requirements.txt pyproject.toml README.md ./

# Hermes home: config + HR plugin (proper extension, no core rewrite)
RUN mkdir -p \
        /home/hermes/.hermes/plugins \
        /home/hermes/.hermes/logs \
        /app/logs \
        /app/data \
    && cp -a /app/plugins/hr-employee /home/hermes/.hermes/plugins/hr-employee \
    && cp /app/config/hermes_config.yaml /home/hermes/.hermes/config.yaml \
    && chown -R hermes:hermes /home/hermes /app/logs

# Executable scripts
RUN chmod +x /app/scripts/*.sh

USER hermes

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=3 \
    CMD ["/app/scripts/healthcheck.sh"]

# tini reaps zombies; entrypoint prepares HERMES_HOME then starts the API
ENTRYPOINT ["/usr/bin/tini", "--", "/app/scripts/start.sh"]
CMD []
