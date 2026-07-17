# HR AI Agent

**Production-ready specialized Human Resources agent** built on the
[Nous Research Hermes Agent Framework](https://github.com/NousResearch/hermes-agent).

Single-container deployment for Proxmox → Ubuntu 24.04 VM → Docker → Hermes → **one HR Agent**.

No desktop UI. No GUI. Server architecture only. Knowledge source: **`data/employees.json` only**
(no database, no vector store, no RAG).

---

## Architecture

```
Physical Server
      │
      ▼
   Proxmox VE
      │
      ▼
 Ubuntu 24.04 VM
      │
      ▼
  Docker Engine
      │
      ▼
 Docker Compose  ──►  container: hr-ai-agent
                           │
                           ├─ Hermes Agent Framework (cloned from GitHub at image build)
                           ├─ Hermes plugin: hr-employee  (custom tools, toolset=hr)
                           ├─ HR Agent (AIAgent + system prompt)
                           ├─ employees.json (sole knowledge base)
                           └─ FastAPI HTTP API :8080
```

| Layer | Component | Role |
|-------|-----------|------|
| Hypervisor | Proxmox | Hosts the Ubuntu VM |
| Guest OS | Ubuntu 24.04 | Docker host |
| Runtime | Docker + Compose | Single production container |
| Framework | [hermes-agent](https://github.com/NousResearch/hermes-agent) | Agent loop, tool registry, plugins |
| Extension | `plugins/hr-employee` | Registers HR tools **without rewriting Hermes** |
| Specialist | `agents/hr_agent.py` | One HR-domain agent (`enabled_toolsets=["hr"]`) |
| Knowledge | `data/employees.json` | Authoritative employee directory |
| Interface | FastAPI (`app/api.py`) | `/v1/chat`, `/health`, `/ready` |

---

## Features

- **Domain-locked HR agent** — refuses non-HR requests politely
- **JSON-only knowledge** — loads `employees.json` at startup; tools never invent data
- **Hermes-native tools** — `search_employee`, `find_department`, `salary_statistics`, …
- **Structured logging** with rotation (`logs/hr-agent.log`)
- **Docker healthcheck** (liveness + readiness)
- **Hot-reload** of employee data via `reload_employees` tool / volume mount
- **Optional API bearer token**
- **Multi-turn sessions** (in-memory, per process)
- **Non-root container user**, multi-stage image, `tini` init

### Example questions the agent can answer

| Question | Tool path |
|----------|-----------|
| How many employees work here? | `count_employees` |
| List all employees | `list_employees` |
| Find employee by ID / name | `get_employee` / `search_employee` |
| Find all programmers | `search_employee(position=…)` or `search_skill` |
| Who is in HR? | `find_department(department=HR)` |
| Who is the manager? | `get_manager_chain` |
| Highest / average salary | `salary_statistics` |
| Hired after 2024 | `search_employee(hired_after=2024-01-01)` |
| Older than 40 | `search_employee(older_than=40)` |
| Speaks English / has Python | `search_language` / `search_skill` |
| Department stats | `list_departments` / `employee_statistics` |
| Return JSON or Markdown table | Prompt instructions + tool JSON |

---

## Folder structure

```
project/
├── docker-compose.yml      # Production Compose definition
├── Dockerfile              # Multi-stage image (clones Hermes from GitHub)
├── .env.example            # All environment variables (copy → .env)
├── .gitignore
├── install.md              # Step-by-step production install guide
├── README.md               # This file
├── requirements.txt        # App Python deps (Hermes installed in Docker)
├── pyproject.toml          # Package metadata (import name hr_tools → tools/)
├── data/
│   └── employees.json      # Sole knowledge base (25 sample employees)
├── config/
│   ├── agent.yaml          # HR agent non-secret settings
│   ├── hermes_config.yaml  # Hermes profile (plugins, tool gating)
│   └── logging.yaml        # Logging reference config
├── prompts/
│   └── system_prompt.md    # Professional HR system prompt
├── agents/
│   └── hr_agent.py         # Hermes AIAgent façade (one specialist)
├── tools/                  # Filesystem path for HR tools
│   ├── employee_service.py # JSON load + query engine
│   └── employee_tool.py    # Hermes tool schemas + handlers
├── plugins/
│   └── hr-employee/        # Official Hermes plugin extension
│       ├── plugin.yaml
│       └── __init__.py
├── app/
│   ├── main.py             # Process entrypoint
│   ├── api.py              # FastAPI routes
│   └── logging_setup.py    # Rotating structured logs
├── logs/                   # Runtime logs (volume)
└── scripts/
    ├── start.sh            # Container ENTRYPOINT
    └── healthcheck.sh      # Docker HEALTHCHECK
```

> **Import note:** Hermes already owns the top-level Python package name `tools`.
> Our implementations live in the `tools/` **directory** but are installed as the
> package **`hr_tools`** (`pyproject.toml` package-dir mapping). This is intentional
> and required for correct coexistence with Hermes.

---

## How the agent works

1. **Container start** (`scripts/start.sh`)
   - Ensures `$HERMES_HOME` exists
   - Copies `plugins/hr-employee` into `$HERMES_HOME/plugins/`
   - Installs default `config.yaml` if missing
   - Validates `employees.json` and system prompt
   - Exec `python -m app.main`

2. **Startup init** (`agents/hr_agent.py`)
   - Loads entire `employees.json` into memory via `EmployeeService`
   - Registers HR tools with Hermes registry (best-effort)
   - Hermes plugin also registers the same tools under **toolset `hr`**

3. **Chat request** (`POST /v1/chat`)
   - Builds a fresh `AIAgent` with:
     - `ephemeral_system_prompt` = contents of `prompts/system_prompt.md`
     - `enabled_toolsets=["hr"]` (no terminal/browser/web by default)
     - `skip_memory=True`, `skip_context_files=True`, `quiet_mode=True`
   - Model calls HR tools → JSON facts → natural language (or JSON/table if asked)
   - If tools return empty → agent must say **No data found**

4. **Never**
   - Invent employees or salaries
   - Read a database
   - Use RAG / embeddings / vector DBs

---

## How Hermes is integrated

We **do not fork or rewrite** Hermes.

| Mechanism | Purpose |
|-----------|---------|
| `pip install` from GitHub in Dockerfile | Official framework install |
| `from run_agent import AIAgent` | Library embedding ([docs](https://hermes-agent.nousresearch.com/docs/guides/python-library)) |
| `plugins/hr-employee` + `ctx.register_tool` | Custom tools without core edits ([plugins](https://hermes-agent.nousresearch.com/docs/user-guide/features/plugins)) |
| `enabled_toolsets=["hr"]` | Lock agent to HR tools only |
| `ephemeral_system_prompt` | Domain-specific HR persona |
| `config/hermes_config.yaml` → `$HERMES_HOME/config.yaml` | Enable plugin `hr-employee` |

Hermes repository is cloned at **image build time** (`ARG HERMES_REF`, default `main`).

---

## How JSON is loaded

```text
employees.json
      │
      ▼
EmployeeService.load()     # startup (and reload_employees tool)
      │
      ├─ validates schema shape (list or {employees: [...]})
      ├─ indexes by employee_id
      └─ keeps full records in memory
            │
            ▼
   HR tool handlers (search_employee, …)
            │
            ▼
   JSON string results → Hermes AIAgent tool loop → user answer
```

Path is controlled by `EMPLOYEES_JSON_PATH` (default `/app/data/employees.json`).
In Compose, `./data` is bind-mounted so you can edit the file on the host and call
`reload_employees` (or restart the container).

---

## API

Base URL (default): `http://<vm-ip>:8080`

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/health` | No | Liveness |
| `GET` | `/ready` | No | Readiness (employees + agent) |
| `GET` | `/v1/info` | No | Service metadata |
| `POST` | `/v1/chat` | Optional bearer | Chat with HR agent |
| `POST` | `/v1/tools/{name}` | Optional bearer | Direct tool invoke (automation) |
| `GET` | `/docs` | No | OpenAPI UI |

### Chat example

```bash
curl -sS http://127.0.0.1:8080/v1/chat \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer ${API_BEARER_TOKEN}" \
  -d '{"message":"How many employees work in Engineering? List them as a markdown table."}'
```

```json
{
  "success": true,
  "response": "| employee_id | full_name | position | ...",
  "session_id": "…",
  "employee_count": 25
}
```

### Direct tool example

```bash
curl -sS http://127.0.0.1:8080/v1/tools/salary_statistics \
  -H 'Content-Type: application/json' \
  -d '{"arguments":{"department":"Engineering","status":"active"}}'
```

---

## Quick start (already on a Docker host)

```bash
cp .env.example .env
# Edit .env — set at least one of OPENROUTER_API_KEY / OPENAI_API_KEY / ANTHROPIC_API_KEY

docker compose build
docker compose up -d
docker compose logs -f hr-agent

curl -sS http://127.0.0.1:8080/ready | jq .
curl -sS http://127.0.0.1:8080/v1/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"How many employees work here?"}'
```

- **Local PC (Windows / macOS / Linux):** see **[install_local.md](install_local.md)**  
- **Production VM (Proxmox / Ubuntu):** see **[install.md](install.md)**

---

## Configuration

| Variable | Purpose |
|----------|---------|
| `OPENROUTER_API_KEY` / `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` | LLM credentials |
| `HR_MODEL` | Model id (default `anthropic/claude-sonnet-4.6`) |
| `EMPLOYEES_JSON_PATH` | Path to JSON directory |
| `SYSTEM_PROMPT_PATH` | Path to system prompt markdown |
| `API_BEARER_TOKEN` | Optional API protection |
| `HR_ENABLED_TOOLSETS` | Default `hr` |
| `LOG_LEVEL` / `LOG_FORMAT` | `INFO` / `json` |
| `TZ` | Container timezone |

See `.env.example` for the complete list.

---

## Employee JSON schema

Each employee is an object with fields including:

`employee_id`, `first_name`, `last_name`, `middle_name`, `birth_date`, `gender`,
`department`, `position`, `manager`, `salary`, `phone`, `email`, `hire_date`,
`experience_years`, `education`, `skills`, `languages`, `status`, `vacation_days`,
`address`, `city`, `country`.

Sample data ships with **25 employees** across Executive, Engineering, HR, Finance,
Marketing, Sales, Legal, and Operations.

---

## Security notes

- Run only on a trusted private network or behind a reverse proxy with TLS.
- Set `API_BEARER_TOKEN` in production.
- Salary and PII are available to the agent by design (internal HR tool).
- Terminal / browser / web toolsets are disabled for this specialist.
- Container runs as non-root user `hermes` (uid 10001).

---

## Future improvements

- Optional read-only RBAC (mask salary for non-HR roles)
- Webhook / Slack / Telegram delivery via Hermes gateway
- Audit log of every tool call with actor identity
- JSON Schema validation on employees.json with CI checks
- Blue/green deploy for zero-downtime employee data swaps
- Prometheus metrics (`/metrics`)
- Multi-file org units (departments.json) while staying JSON-only
- Pin Hermes to a release tag / commit SHA in Dockerfile for supply-chain control

---

## License

Application code in this repository: MIT (or as designated by your organization).

Hermes Agent Framework: see [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) license.
