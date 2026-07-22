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
| GET | `/v1/self-improve` | Learned SQL-pattern store stats |

## Self-improving (global recipe store)

The SQL agent learns across all users/sessions of this instance:

- Every **successful** question is stored with the **executed SQL** as a
  reusable *recipe* (`data/self_improve.json`, mounted volume).
- On a new question, the **top-k** most similar recipes are injected into the
  SQL prompt as few-shot examples — so working SQL / multi-script term
  mappings are reused instead of re-derived.
- **No prompt bloat**: only top-k are injected; the store is bounded
  (`SELF_IMPROVE_MAX_RECIPES`, least-used pruned) — curation, not accumulation.
- **Leak-safe & global**: stores the SQL *technique*, never result rows; one
  shared store benefits everyone (single shared database).

Backend-agnostic — works under `hermes`, `hermes_lite`, or plain LangGraph.

```env
SELF_IMPROVE_ENABLED=true
SELF_IMPROVE_STORE_PATH=./data/self_improve.json
SELF_IMPROVE_TOP_K=3
SELF_IMPROVE_MIN_SCORE=0.18
SELF_IMPROVE_MAX_RECIPES=500
```

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
