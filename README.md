# HR SQL Agent (Hermes)

**Production-ready PostgreSQL SQL agent** built on the
[Nous Research Hermes Agent Framework](https://github.com/NousResearch/hermes-agent).

Single-container deployment: Docker → Hermes → **one SQL Agent**.

No desktop UI. Knowledge source: **PostgreSQL only** (via read-only SQL tools).
No `employees.json`, no vector store, no RAG.

---

## Architecture

```
Docker Compose  ──►  container: hr-ai-agent
                           │
                           ├─ Hermes Agent Framework
                           ├─ Plugin: hr-employee  (toolset=sql)
                           ├─ AIAgent + SQL system prompt
                           ├─ PostgreSQL (DATABASE_URL)
                           └─ FastAPI HTTP API :8080
```

| Layer | Component | Role |
|-------|-----------|------|
| Framework | [hermes-agent](https://github.com/NousResearch/hermes-agent) | Agent loop, tool registry, plugins |
| Extension | `plugins/hr-employee` | Registers SQL tools without rewriting Hermes |
| Specialist | `agents/hr_agent.py` | SQL agent (`enabled_toolsets=["sql"]`) |
| Knowledge | PostgreSQL (`DATABASE_URL`) | Authoritative HR / org data |
| Tools | `list_tables`, `describe_table`, `run_sql`, `db_ping` | Read-only DB access |
| Interface | FastAPI (`app/api.py`) | `/v1/chat`, `/health`, `/ready` |

### Request flow

```text
User question
    → Hermes AIAgent
    → list_tables / describe_table (schema)
    → run_sql (SELECT only)
    → PostgreSQL
    → natural language answer
```

---

## Features

- **Hermes-native SQL agent** — model writes SQL; tools execute it
- **Read-only SQL guard** — blocks INSERT/UPDATE/DELETE/DDL
- **Business dictionary** in `prompts/system_prompt.md`
- **Structured logging** with rotation (`logs/hr-agent.log`)
- **Docker healthcheck** (liveness + DB readiness)
- **Optional API bearer token**
- **Multi-turn sessions** (in-memory, per process)

### Example questions

| Question | Typical tools |
|----------|----------------|
| How many employees? | `run_sql` → `SELECT count(*) FROM employees` |
| List departments | `run_sql` / `list_tables` + `describe_table` |
| Who works in a section? | JOIN `employees` ↔ `work_places` ↔ `sections` |
| Education history | JOIN `institutions` |

---

## Folder structure

```
project/
├── docker-compose.yml
├── Dockerfile
├── .env.example
├── requirements.txt
├── pyproject.toml
├── config/
│   ├── agent.yaml
│   ├── hermes_config.yaml
│   └── logging.yaml
├── prompts/
│   └── system_prompt.md      # SQL expert + business dictionary
├── agents/
│   └── hr_agent.py           # Hermes AIAgent façade
├── hr_tools/
│   ├── db_service.py         # PostgreSQL pool + readonly SQL guard
│   └── sql_tool.py           # Hermes SQL tools
├── plugins/
│   └── hr-employee/          # Registers toolset sql
├── app/
│   ├── main.py
│   ├── api.py
│   └── logging_setup.py
├── logs/
└── scripts/
    ├── start.sh
    ├── healthcheck.sh
    └── validate_sql_guard.py
```

> **Import note:** package is named `hr_tools/` (not `tools/`) so it never
> shadows Hermes's top-level `tools` package.

---

## Configuration

Copy `.env.example` → `.env`:

```env
DATABASE_URL=postgresql://readonly_user:password@host:5432/tuzilma
HR_ENABLED_TOOLSETS=sql
HR_MODEL=gpt-4o-mini
OPENAI_API_KEY=...
```

Prefer a **read-only** DB role.

---

## API

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Liveness |
| `GET` | `/ready` | Agent + PostgreSQL ready |
| `GET` | `/v1/info` | Service metadata |
| `POST` | `/v1/chat` | SQL agent chat |
| `POST` | `/v1/tools/{name}` | Direct tool invoke |
| `GET` | `/docs` | OpenAPI UI |

### Chat example

```bash
curl -sS http://127.0.0.1:8080/v1/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"How many employees are in the database?"}'
```

### Direct SQL tool

```bash
curl -sS http://127.0.0.1:8080/v1/tools/run_sql \
  -H 'Content-Type: application/json' \
  -d '{"arguments":{"sql":"SELECT count(*) AS n FROM employees"}}'
```

---

## Local checks (no DB required)

```bash
python scripts/validate_sql_guard.py
```

---

## Hermes integration

We **do not fork or rewrite** Hermes.

| Mechanism | Purpose |
|-----------|---------|
| `from run_agent import AIAgent` | Library embedding |
| `plugins/hr-employee` + `ctx.register_tool` | SQL tools |
| `enabled_toolsets=["sql"]` | Only SQL tools |
| `ephemeral_system_prompt` | SQL expert persona |
