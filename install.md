# HR AI Agent — Production Installation Guide

This guide walks through a complete production deployment on:

**Physical server → Proxmox VE → Ubuntu 24.04 VM → Docker Engine → Docker Compose → Hermes + HR Agent**

No desktop environment. No GUI. Commands are for a standard Ubuntu Server 24.04 LTS guest.

---

## Table of contents

1. [Prerequisites](#1-prerequisites)
2. [Create the Ubuntu 24.04 VM on Proxmox](#2-create-the-ubuntu-2404-vm-on-proxmox)
3. [First login and system baseline](#3-first-login-and-system-baseline)
4. [Install Docker Engine](#4-install-docker-engine)
5. [Install Docker Compose plugin](#5-install-docker-compose-plugin)
6. [Clone / copy the project](#6-clone--copy-the-project)
7. [Configure environment variables](#7-configure-environment-variables)
8. [Build the image](#8-build-the-image)
9. [Run the container](#9-run-the-container)
10. [Verify health](#10-verify-health)
11. [Use the HR Agent API](#11-use-the-hr-agent-api)
12. [View logs](#12-view-logs)
13. [Restart / stop / start](#13-restart--stop--start)
14. [Update Hermes](#14-update-hermes)
15. [Update the HR Agent application](#15-update-the-hr-agent-application)
16. [Update employees.json](#16-update-employeesjson)
17. [Backup](#17-backup)
18. [Restore](#18-restore)
19. [Firewall and reverse proxy (optional)](#19-firewall-and-reverse-proxy-optional)
20. [Troubleshooting](#20-troubleshooting)
21. [Common errors](#21-common-errors)
22. [FAQ](#22-faq)

---

## 1. Prerequisites

| Item | Recommendation |
|------|----------------|
| Proxmox VE | 8.x |
| VM vCPU | 2+ cores |
| VM RAM | 4 GB minimum (8 GB recommended) |
| VM disk | 40 GB+ (thin LVM or ZFS) |
| Guest OS | Ubuntu Server 24.04 LTS |
| Network | Static IP or DHCP reservation |
| LLM access | OpenRouter **or** OpenAI **or** Anthropic API key |
| Outbound HTTPS | Required to pull Docker images and call the LLM API |

You need:

- SSH access to the VM as a sudo-capable user
- This project files (git clone or `scp`/`rsync`)

---

## 2. Create the Ubuntu 24.04 VM on Proxmox

### 2.1 Download the ISO (on Proxmox host or via GUI)

Proxmox UI: **local → ISO Images → Download from URL**

```text
https://releases.ubuntu.com/24.04/ubuntu-24.04.2-live-server-amd64.iso
```

(Use the current 24.04.x server ISO if the patch version differs.)

### 2.2 Create VM (example settings)

| Setting | Value |
|---------|-------|
| Name | `hr-ai-agent` |
| BIOS | OVMF (UEFI) or SeaBIOS |
| Machine | q35 |
| CPU | host, 2–4 cores |
| Memory | 4096–8192 MB |
| Disk | 40G, VirtIO SCSI, discard=on |
| Network | VirtIO, bridge `vmbr0` |
| Guest agent | Enable QEMU Guest Agent |

### 2.3 Install Ubuntu Server

1. Attach the ISO, start the VM, open Console.
2. Install **Ubuntu Server** (not Desktop).
3. Enable OpenSSH server when asked.
4. Create an admin user (example: `ubuntu`).
5. Finish install, reboot, remove ISO.

### 2.4 Note the VM IP

From Proxmox console or:

```bash
ip -br a
```

Example: `192.168.1.50`

From your workstation:

```bash
ssh ubuntu@192.168.1.50
```

---

## 3. First login and system baseline

```bash
sudo apt update
sudo apt -y upgrade
sudo apt -y install ca-certificates curl gnupg lsb-release git jq htop unzip
```

### Optional: set timezone

```bash
sudo timedatectl set-timezone UTC
timedatectl
```

### Optional: install QEMU guest agent

```bash
sudo apt -y install qemu-guest-agent
sudo systemctl enable --now qemu-guest-agent
```

### Create an application directory

```bash
sudo mkdir -p /opt/hr-ai-agent
sudo chown "$USER":"$USER" /opt/hr-ai-agent
cd /opt/hr-ai-agent
```

---

## 4. Install Docker Engine

Follow Docker’s official Ubuntu repository method.

```bash
# Remove conflicting packages (safe if none installed)
sudo apt -y remove docker.io docker-doc docker-compose docker-compose-v2 podman-docker containerd runc || true

# Add Docker’s official GPG key and apt repo
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc

echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "${VERSION_CODENAME}") stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

sudo apt update
sudo apt -y install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
```

### Enable and start Docker

```bash
sudo systemctl enable --now docker
sudo systemctl status docker --no-pager
```

### Allow your user to run Docker without sudo (optional)

```bash
sudo usermod -aG docker "$USER"
# Log out and back in (or newgrp docker)
newgrp docker
docker run --rm hello-world
```

---

## 5. Install Docker Compose plugin

The Compose **plugin** is installed with Docker Engine via `docker-compose-plugin` (previous section).

Verify:

```bash
docker compose version
```

Expected output example:

```text
Docker Compose version v2.x.x
```

> Note: Prefer `docker compose` (plugin) over legacy `docker-compose` binary.

---

## 6. Clone / copy the project

### Option A — Git clone

```bash
cd /opt
git clone <YOUR_GIT_URL_FOR_THIS_PROJECT> hr-ai-agent
cd /opt/hr-ai-agent
```

### Option B — Copy from workstation

From your workstation (PowerShell / Linux / macOS):

```bash
rsync -avz --exclude '.git' --exclude 'logs/*' --exclude '.env' \
  ./hr-ai-agent/ ubuntu@192.168.1.50:/opt/hr-ai-agent/
```

Or with `scp`:

```bash
scp -r ./hr-ai-agent ubuntu@192.168.1.50:/opt/
```

### Confirm layout

```bash
cd /opt/hr-ai-agent
ls -la
# Dockerfile  docker-compose.yml  .env.example  data/  agents/  hr_tools/  ...
```

---

## 7. Configure environment variables

```bash
cd /opt/hr-ai-agent
cp .env.example .env
chmod 600 .env
nano .env   # or: vim .env
```

### Minimum required settings

```bash
# Choose ONE provider path (example: OpenRouter)
OPENROUTER_API_KEY=sk-or-v1-xxxxxxxx

# Model (OpenRouter-style id works with Hermes)
HR_MODEL=anthropic/claude-sonnet-4.6

# Optional but recommended in production
API_BEARER_TOKEN=$(openssl rand -hex 32)
TZ=UTC
LOG_LEVEL=INFO
```

### Alternative providers

**OpenAI**

```bash
OPENAI_API_KEY=sk-...
HR_MODEL=gpt-4.1
```

**Anthropic**

```bash
ANTHROPIC_API_KEY=sk-ant-...
HR_MODEL=claude-sonnet-4-6
```

**OpenAI-compatible local / proxy**

```bash
OPENAI_API_KEY=not-needed-or-local-key
OPENAI_BASE_URL=http://10.0.0.20:11434/v1
HR_MODEL=llama3.1
```

Save and exit.

---

## 8. Build the image

The Dockerfile:

1. Clones [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) from GitHub
2. Installs Hermes into a virtualenv
3. Installs this HR application
4. Configures non-root user + Hermes plugin

```bash
cd /opt/hr-ai-agent
docker compose build
```

Build with a pinned Hermes ref (recommended for production):

```bash
docker compose build --build-arg HERMES_REF=v0.18.2
```

Or edit `docker-compose.yml` → `build.args.HERMES_REF`.

First build may take several minutes (network + compile of dependencies).

### Verify image

```bash
docker images | grep hr-ai-agent
```

---

## 9. Run the container

```bash
cd /opt/hr-ai-agent
docker compose up -d
```

Check status:

```bash
docker compose ps
docker ps --filter name=hr-ai-agent
```

Expected: `STATUS` contains `Up` and eventually `(healthy)`.

---

## 10. Verify health

### Liveness

```bash
curl -sS http://127.0.0.1:8080/health | jq .
```

```json
{ "status": "ok", "service": "hr-ai-agent" }
```

### Readiness (employees loaded + agent init)

```bash
curl -sS http://127.0.0.1:8080/ready | jq .
```

### Info

```bash
curl -sS http://127.0.0.1:8080/v1/info | jq .
```

### Container healthcheck script (inside container)

```bash
docker exec hr-ai-agent /app/scripts/healthcheck.sh
echo $?
# 0 = healthy
```

---

## 11. Use the HR Agent API

### Simple chat

```bash
curl -sS http://127.0.0.1:8080/v1/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"How many employees work here?"}' | jq .
```

### With bearer token (if configured)

```bash
export TOKEN='your-api-bearer-token'

curl -sS http://127.0.0.1:8080/v1/chat \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer ${TOKEN}" \
  -d '{"message":"Find all employees with Python skill. Return a markdown table."}' | jq .
```

### Multi-turn session

```bash
curl -sS http://127.0.0.1:8080/v1/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"Who is EMP-005?","session_id":"demo-1"}' | jq .

curl -sS http://127.0.0.1:8080/v1/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"What is their salary?","session_id":"demo-1"}' | jq .
```

### Direct tool call (no LLM)

```bash
curl -sS http://127.0.0.1:8080/v1/tools/count_employees \
  -H 'Content-Type: application/json' \
  -d '{"arguments":{"status":"active"}}' | jq .

curl -sS http://127.0.0.1:8080/v1/tools/salary_statistics \
  -H 'Content-Type: application/json' \
  -d '{"arguments":{"department":"Engineering"}}' | jq .
```

### OpenAPI docs

Browse: `http://<vm-ip>:8080/docs`

---

## 12. View logs

### Compose / container logs

```bash
docker compose logs -f hr-agent
docker compose logs --tail=200 hr-agent
docker logs -f hr-ai-agent
```

### Application log files (bind mount)

```bash
ls -la /opt/hr-ai-agent/logs/
tail -f /opt/hr-ai-agent/logs/hr-agent.log
tail -f /opt/hr-ai-agent/logs/hr-agent-errors.log
```

Log rotation is configured via `LOG_MAX_BYTES` and `LOG_BACKUP_COUNT` (see `.env.example`).
Docker’s own json-file driver also rotates (`max-size` / `max-file` in `docker-compose.yml`).

---

## 13. Restart / stop / start

```bash
cd /opt/hr-ai-agent

# Restart
docker compose restart hr-agent

# Stop (keeps container)
docker compose stop

# Start
docker compose start

# Stop and remove container (keeps volumes/images)
docker compose down

# Full teardown including named volume (DESTRUCTIVE for Hermes home)
docker compose down -v
```

---

## 14. Update Hermes

Hermes is installed **inside the image** at build time from GitHub.

### 14.1 Track latest `main`

```bash
cd /opt/hr-ai-agent
docker compose build --no-cache --build-arg HERMES_REF=main
docker compose up -d
```

### 14.2 Pin a release tag (recommended)

```bash
docker compose build --no-cache --build-arg HERMES_REF=v0.18.2
docker compose up -d
```

### 14.3 Pin a commit SHA (maximum reproducibility)

```bash
# Dockerfile uses git clone --branch; for SHA, temporarily change Dockerfile
# to: git clone ... && git checkout <sha>
# Or set HERMES_REF to a tag that points at the commit.
```

After update, verify:

```bash
docker compose logs --tail=100 hr-agent
curl -sS http://127.0.0.1:8080/ready | jq .
```

---

## 15. Update the HR Agent application

When you change Python code, prompts, plugins, Dockerfile, or Compose:

```bash
cd /opt/hr-ai-agent
# git pull   # if using git
docker compose build
docker compose up -d
docker compose logs -f hr-agent
```

Code and plugins are baked into the image; bind mounts cover `data/`, `logs/`, `prompts/`, `config/`.

If you only changed **prompts** or **config** (mounted volumes):

```bash
docker compose restart hr-agent
```

---

## 16. Update employees.json

### 16.1 Edit on the host

```bash
cd /opt/hr-ai-agent
cp data/employees.json data/employees.json.bak.$(date +%Y%m%d%H%M%S)
nano data/employees.json
# validate JSON
python3 -m json.tool data/employees.json > /dev/null && echo OK
```

### 16.2 Hot-reload without full restart

```bash
curl -sS http://127.0.0.1:8080/v1/tools/reload_employees \
  -H 'Content-Type: application/json' \
  -d '{"arguments":{}}' | jq .
```

Or restart:

```bash
docker compose restart hr-agent
```

### 16.3 Schema rules

- Root object with `"employees": [ {...}, ... ]` **or** a bare array of employee objects
- Each employee **must** have unique `employee_id`
- `manager` is an `employee_id` string or `null`
- `skills` / `languages` are arrays of strings

---

## 17. Backup

### 17.1 What to back up

| Path / volume | Contents |
|---------------|----------|
| `./data/employees.json` | Knowledge base |
| `./.env` | Secrets (encrypt at rest!) |
| `./prompts/` | System prompt |
| `./config/` | Hermes / agent config |
| `./logs/` | Optional audit |
| Docker volume `hr-ai-agent-hermes-home` | Hermes profile state |

### 17.2 Example backup script

```bash
#!/usr/bin/env bash
set -euo pipefail
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
DEST=/var/backups/hr-ai-agent
mkdir -p "$DEST"
cd /opt/hr-ai-agent

tar -czf "$DEST/hr-ai-agent-files-${STAMP}.tar.gz" \
  data prompts config .env.example \
  --exclude='logs/*'

# Include secrets if present (store securely!)
if [[ -f .env ]]; then
  tar -czf "$DEST/hr-ai-agent-env-${STAMP}.tar.gz" .env
  chmod 600 "$DEST/hr-ai-agent-env-${STAMP}.tar.gz"
fi

# Named volume
docker run --rm \
  -v hr-ai-agent-hermes-home:/source:ro \
  -v "$DEST":/backup \
  alpine tar -czf "/backup/hermes-home-${STAMP}.tar.gz" -C /source .

echo "Backups written to $DEST"
ls -lh "$DEST" | tail
```

Save as `/opt/hr-ai-agent/scripts/backup.sh`, `chmod +x`, run via cron:

```bash
sudo crontab -e
# Daily 02:15 UTC
15 2 * * * /opt/hr-ai-agent/scripts/backup.sh >> /var/log/hr-ai-agent-backup.log 2>&1
```

---

## 18. Restore

### 18.1 Restore files

```bash
cd /opt/hr-ai-agent
docker compose down

# Example
tar -xzf /var/backups/hr-ai-agent/hr-ai-agent-files-YYYYMMDDTHHMMSSZ.tar.gz
# restore .env carefully
tar -xzf /var/backups/hr-ai-agent/hr-ai-agent-env-YYYYMMDDTHHMMSSZ.tar.gz
chmod 600 .env

docker compose up -d
```

### 18.2 Restore Hermes volume

```bash
docker compose down
docker volume rm hr-ai-agent-hermes-home || true
docker volume create hr-ai-agent-hermes-home

docker run --rm \
  -v hr-ai-agent-hermes-home:/target \
  -v /var/backups/hr-ai-agent:/backup:ro \
  alpine sh -c 'tar -xzf /backup/hermes-home-YYYYMMDDTHHMMSSZ.tar.gz -C /target'

docker compose up -d
```

### 18.3 Verify

```bash
curl -sS http://127.0.0.1:8080/ready | jq .
curl -sS http://127.0.0.1:8080/v1/tools/count_employees \
  -H 'Content-Type: application/json' -d '{"arguments":{}}' | jq .
```

---

## 19. Firewall and reverse proxy (optional)

### UFW (VM)

```bash
sudo ufw allow OpenSSH
sudo ufw allow 8080/tcp   # or only from reverse-proxy IP
sudo ufw enable
sudo ufw status
```

Prefer exposing **80/443** on a reverse proxy and keeping 8080 on localhost.

### Nginx reverse proxy (TLS)

```nginx
server {
    listen 443 ssl http2;
    server_name hr.example.com;

    ssl_certificate     /etc/ssl/certs/hr.fullchain.pem;
    ssl_certificate_key /etc/ssl/private/hr.key;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;
    }
}
```

---

## 20. Troubleshooting

### Container exits immediately

```bash
docker compose ps -a
docker compose logs hr-agent
```

Common causes: missing `employees.json`, bad permissions on mounts, invalid `.env` quoting.

### Healthcheck unhealthy

```bash
docker inspect --format='{{json .State.Health}}' hr-ai-agent | jq .
docker exec hr-ai-agent /app/scripts/healthcheck.sh
docker exec hr-ai-agent curl -sS http://127.0.0.1:8080/ready
```

### Agent ready but chat fails

Usually LLM credentials or model routing:

```bash
docker compose logs hr-agent | tail -100
# Confirm keys are present inside container (values redacted in your head)
docker exec hr-ai-agent sh -c 'env | grep -E "API_KEY|HR_MODEL|OPENAI_BASE" | sed "s/=.*/=***/"'
```

### Tools return empty / wrong data

```bash
docker exec hr-ai-agent python - <<'PY'
from hr_tools.employee_service import get_employee_service
print(get_employee_service().readiness())
print(get_employee_service().count_employees())
PY
```

### Rebuild from scratch

```bash
docker compose down
docker compose build --no-cache
docker compose up -d
```

### Disk full

```bash
df -h
docker system df
docker image prune -f
```

---

## 21. Common errors

| Symptom | Cause | Fix |
|---------|-------|-----|
| `employees.json not found` | Missing mount or wrong path | Ensure `./data/employees.json` exists; check `EMPLOYEES_JSON_PATH` |
| `401 Unauthorized` | Bearer token required | Pass `Authorization: Bearer …` or clear `API_BEARER_TOKEN` |
| `503` on `/ready` | Agent failed init | Check logs; validate JSON; ensure Hermes import works |
| `No data found` | Valid empty search | Expected when no matches; verify query filters |
| LLM auth error | Bad/missing API key | Fix `.env`, `docker compose up -d --force-recreate` |
| Port already allocated | Host 8080 in use | Change `APP_PORT` in `.env` (maps host:container) |
| Permission denied on logs | Host dir owned by root | `sudo chown -R $USER:$USER logs data` |
| `import run_agent` fails | Hermes not in image | Rebuild image; check Dockerfile build logs |
| Plugin tools not called | Wrong toolsets | Ensure `HR_ENABLED_TOOLSETS=hr` and plugin installed under `HERMES_HOME` |
| JSON decode error on employees | Invalid JSON | `python3 -m json.tool data/employees.json` |
| Slow first response | Cold model / network | Normal; check outbound latency to provider |

---

## 22. FAQ

### Is a database required?

No. Only `employees.json`.

### Is RAG / Chroma / vector DB used?

No. Queries are deterministic filters over the in-memory JSON directory.

### Can I run multiple HR agents?

This design is **one container → one specialized HR agent**. Scale by placing a reverse proxy in front or running more Compose stacks with separate data files.

### Does this use Hermes Desktop / TUI?

No. Desktop and TUI are disabled (`HERMES_TUI=0`, `HERMES_SKIP_DESKTOP=1`). Interface is HTTP API only.

### How do I add more employees?

Edit `data/employees.json`, validate JSON, call `reload_employees` or restart.

### How do I change the system prompt?

Edit `prompts/system_prompt.md` (bind-mounted) and restart the container.

### Where is Hermes itself?

Inside the image at `/opt/hermes-agent` and installed into `/opt/venv`. Not rewritten. Extended via plugin.

### Can Hermes update itself with `hermes update`?

Not recommended inside this immutable production image. Rebuild the image with a new `HERMES_REF` instead.

### What Python version?

Python **3.12** in the container (Hermes supports 3.11–3.13).

### How do I secure salary data?

Run only on private networks, set `API_BEARER_TOKEN`, terminate TLS at a reverse proxy, and restrict SSH. The agent is an authorized internal HR tool by design.

---

## Quick reference card

```bash
cd /opt/hr-ai-agent
cp .env.example .env && nano .env
docker compose build
docker compose up -d
docker compose logs -f hr-agent
curl -sS http://127.0.0.1:8080/ready | jq .
curl -sS http://127.0.0.1:8080/v1/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"List all departments with headcount."}'
docker compose restart
docker compose down
```

For architecture and API details, see [README.md](README.md).
