# Install — LangChain SQLAgent

Flow: **Chat Input → Prompt Template → SQLAgent → Chat Output**

1. `cp .env.example .env` and set `OPENAI_API_KEY`, `DATABASE_URL`, `LLM_MODEL=gpt-4.1`
2. `pip install -r requirements.txt` **or** `docker compose build && docker compose up -d`
3. `curl http://127.0.0.1:8080/ready`
4. `POST /v1/chat` with `{"message":"..."}`

Prompt file: `prompts/sql_agent_system.md` (from Langflow export).
