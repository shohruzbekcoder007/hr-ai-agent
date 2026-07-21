# Variant 2 — Hermes host + SQL tool (LangGraph)

```text
Open WebUI → Gateway → POST /v1/chat
     → Hermes host agent  (conversation + memory)
          → tool: sql_ask
               → LangGraph / LangChain SQL agent
                    → PostgreSQL (DATABASE_URL)
```

## Why this design

- **Hermes host** owns multi-turn context / session history.
- **SQL stack** stays LangGraph (schema + readonly SQL tools).
- Gateway keeps `HERMES_GIS_BASE_URL=http://host.docker.internal:8080`.

If the `hermes-agent` package is missing, **hermes_lite** runs the same pattern
(outer tool-calling host + `sql_ask`).

## Configure

```env
OPENAI_API_KEY=...
DATABASE_URL=postgresql://...
LLM_MODEL=gpt-4.1
HERMES_SKIP_MEMORY=false
HERMES_ENABLED_TOOLSETS=sql_bridge
```

## API

| Method | Path | Role |
|--------|------|------|
| POST | `/v1/chat` | Host chat (`session_id` for memory) |
| GET | `/ready` | Host + inner SQL ready |
| GET | `/v1/info` | Architecture metadata |

## Run

```bash
docker compose build
docker compose up -d
```

## Layout

```text
agents/
  hermes_host.py      # host agent (Hermes or hermes_lite)
  sql_bridge_tool.py  # sql_ask tool
  sql_agent.py        # LangGraph SQL implementation
plugins/sql-bridge/   # Hermes plugin registration
prompts/
  hermes_coordinator.md   # host system prompt
  sql_agent_system.md     # inner SQL agent prompt (Langflow text)
```
