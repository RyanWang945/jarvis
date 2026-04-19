# Jarvis

Long-running local AI assistant driven by Feishu, LangGraph, and local skills.

## Local Development

Install dependencies:

```powershell
uv sync
```

Run the API server:

```powershell
uv run jarvis
```

Health check:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
```

Configuration can be provided through environment variables prefixed with `JARVIS_`, or through a local `.env` file. Use `.env.example` as the starting template.

Common settings:

```text
JARVIS_HOST=127.0.0.1
JARVIS_PORT=8000
JARVIS_LOG_LEVEL=INFO
JARVIS_LOG_DIR=logs
JARVIS_DATA_DIR=data
JARVIS_PLANNER_TYPE=llm
JARVIS_LLM_PROVIDER=deepseek
JARVIS_LLM_TIMEOUT_SECONDS=60
JARVIS_WORKER_MODE=inline
JARVIS_WORKER_MAX_WORKERS=4
JARVIS_DEEPSEEK_API_KEY=sk-...
JARVIS_DEEPSEEK_BASE_URL=https://api.deepseek.com
JARVIS_DEEPSEEK_MODEL=deepseek-chat
JARVIS_KIMI_API_KEY=sk-...
JARVIS_KIMI_BASE_URL=https://api.moonshot.cn/v1
JARVIS_KIMI_MODEL=moonshot-v1-8k
JARVIS_GEMINI_API_KEY=...
JARVIS_GEMINI_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai
JARVIS_GEMINI_MODEL=gemini-2.5-flash
JARVIS_TAVILY_API_KEY=tvly-...
JARVIS_OBSIDIAN_VAULT_PATH=E:\path\to\vault
```

Run a local agent task:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/agent/run `
  -Method Post `
  -ContentType "application/json" `
  -Body '{"instruction":"运行测试并总结结果","workdir":"E:\\pythonProject\\jarvis"}'
```

`JARVIS_PLANNER_TYPE=llm` uses the configured OpenAI-compatible chat provider for planning and completion assessment. Set `JARVIS_LLM_PROVIDER` to `deepseek`, `kimi`, or `gemini`; `rule_based` is only a local fallback for tests or offline debugging.

`JARVIS_WORKER_MODE=thread` enables the experimental threaded worker client and starts the in-process dispatcher that resumes agent threads when workers finish.

`JARVIS_TAVILY_API_KEY` enables the `web_search` tool backed by Tavily.
