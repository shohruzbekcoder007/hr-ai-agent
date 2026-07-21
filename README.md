# AI Agents — LangChain SQLAgent

Langflow-compatible flow:

```text
Chat Input → Prompt Template → SQLAgent → Chat Output
```

Built with **LangChain** `create_sql_agent` + `SQLDatabaseToolkit` (same as Langflow 1.9.2 SQLAgent node).

## Configure

```bash
cp .env.example .env
# set OPENAI_API_KEY, DATABASE_URL, LLM_MODEL=gpt-4.1
```

Prompt (from Langflow Prompt Template): `prompts/sql_agent_system.md`

## Run

```bash
pip install -r requirements.txt
python -m app.main
```

Docker:

```bash
docker compose build
docker compose up -d
```

## API (Hermes-compatible → Open WebUI gateway)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Liveness |
| GET | `/ready` | Orchestrator + agents ready |
| GET | `/v1/info` | Metadata + agent list |
| POST | `/v1/chat` | `{"message":"..."}` — **single entry for gateway** |

### Multi-agent

```text
Open WebUI → Gateway (HERMES_GIS_BASE_URL) → this app /v1/chat
                                         → Orchestrator
                                         → sql_agent [+ extra_agent…]
```

| Env | Meaning |
|-----|---------|
| `AGENT_ORCHESTRATION_MODE=sql_only` | Only SQL (default) |
| `sequential` | Agent1 then Agent2 (context passed) |
| `parallel` | All agents, merge answers |
| `route` | Keyword pick one agent |
| `AGENT_EXTRA_ENABLED=true` | Load `agents/extra_agent.py` |

Gateway URL **does not change** when you add agents — still one base URL.

```bash
curl -s http://127.0.0.1:8080/v1/chat \
  -H "Content-Type: application/json" \
  -d "{\"message\":\"Statistika qo'mitasi markaziy apparatida nechta odam ishlaydi?\"}"
```
