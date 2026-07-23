# Variant 2 — Hermes host + SQL tool (LangGraph) + document RAG

```text
Open WebUI → Gateway → POST /v1/chat
     → Hermes host agent  (conversation + memory)
          → tool: sql_ask
               → LangGraph / LangChain SQL agent → PostgreSQL
          → tool: docs_ask
               → RAG agent → Chroma (PDF / Word / FAQ)

Direct RAG (no host): POST /v1/docs/chat
```

## Why this design

- **Hermes host** owns multi-turn context / session history.
- **SQL stack** stays LangGraph (schema + readonly SQL tools).
- **RAG** is a separate agent for policies/PDF/Word; exposed as `docs_ask` + `/v1/docs/*`.
- Gateway keeps `HERMES_GIS_BASE_URL=http://host.docker.internal:8080`.

If the `hermes-agent` package is missing, **hermes_lite** runs the same pattern
(outer tool-calling host + `sql_ask` / `docs_ask`).

## Configure

```env
OPENAI_API_KEY=...
DATABASE_URL=postgresql://...
LLM_MODEL=gpt-4.1
HERMES_SKIP_MEMORY=false
HERMES_ENABLED_TOOLSETS=sql_bridge,docs_bridge

# Document RAG + embeddings
RAG_ENABLED=true
RAG_DOCS_DIR=./data/docs
RAG_CHROMA_ROOT=./data/rag/chroma

# Preferred: remote embedding-service (sibling d:\GROK\embedding-service)
RAG_EMBED_PROVIDER=remote
RAG_EMBED_URL=http://host.docker.internal:8090
# RAG_EMBED_BEARER_TOKEN=

# In-process fallback (no separate service):
# RAG_EMBED_PROVIDER=openai
# RAG_EMBED_MODEL=text-embedding-3-small
# Local bge-m3: RAG_EMBED_PROVIDER=local + requirements-rag-local.txt
```

## API

| Method | Path | Role |
|--------|------|------|
| POST | `/v1/chat` | Host chat (`session_id` for memory) |
| GET | `/ready` | Host + inner SQL ready |
| GET | `/v1/info` | Architecture metadata (+ `rag` summary) |
| GET | `/v1/self-improve` | Learned SQL-pattern store stats |
| POST | `/v1/docs/chat` | Document RAG Q&A |
| POST | `/v1/docs/reindex` | Rebuild Chroma for current embed identity |
| GET | `/v1/docs/ready` | RAG ready |
| GET | `/v1/docs/info` | RAG config + stats |
| GET | `/v1/docs/files` | Files under `RAG_DOCS_DIR` |

## Document RAG

1. Put PDF / DOCX / MD / TXT into `data/docs/` (Docker volume `./data`).
2. `POST /v1/docs/reindex` (same bearer as chat if `API_BEARER_TOKEN` set).
3. Ask via `POST /v1/docs/chat` or host chat (`docs_ask`).

**Embedding switch:** change model on **embedding-service** env (or local `RAG_EMBED_*`), then **full reindex** here (`POST /v1/docs/reindex`).  
`index_key` comes from the embed service (`provider__model__d{dim}`); Chroma paths never mix dimensions.

```bash
# Terminal 1 — embedding service
cd ../embedding-service && python -m app.main

# Terminal 2 — this app (after RAG_EMBED_PROVIDER=remote)
curl -X POST http://127.0.0.1:8080/v1/docs/reindex
```

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
  embeddings.py       # get_embeddings() from env only
  rag_agent.py        # PDF/Word → Chroma → answer
  rag_bridge_tool.py  # docs_ask tool
  self_improve.py     # global SQL recipe store
plugins/sql-bridge/   # Hermes plugin registration
prompts/
  hermes_coordinator.md   # host system prompt
  sql_agent_system.md     # inner SQL agent prompt
  rag_agent_system.md     # document RAG prompt
data/
  docs/               # drop PDF/DOCX/MD/TXT here
  rag/chroma/         # per-model Chroma indexes
```
