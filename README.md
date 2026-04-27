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
JARVIS_KNOWLEDGE_DEFAULT_LANGUAGE=zh
JARVIS_KNOWLEDGE_DEFAULT_CHUNK_PROFILE=medium_overlap_v1
JARVIS_PLANNER_TYPE=llm
JARVIS_LLM_PROVIDER=deepseek
JARVIS_LLM_TIMEOUT_SECONDS=60
JARVIS_WORKER_MODE=inline
JARVIS_WORKER_MAX_WORKERS=4
JARVIS_DASHSCOPE_API_KEY=sk-...
JARVIS_DASHSCOPE_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
JARVIS_DASHSCOPE_EMBEDDING_MODEL=text-embedding-v4
JARVIS_DASHSCOPE_EMBEDDING_BATCH_SIZE=8
JARVIS_DASHSCOPE_EMBEDDING_MAX_WORKERS=2
JARVIS_ALIYUN_OPENSEARCH_API_KEY=OS-...
JARVIS_ALIYUN_OPENSEARCH_ENDPOINT=https://***.opensearch.aliyuncs.com
JARVIS_ALIYUN_OPENSEARCH_WORKSPACE=default
JARVIS_ALIYUN_OPENSEARCH_DOCUMENT_ANALYZE_SERVICE_ID=ops-document-analyze-002
JARVIS_ALIYUN_OPENSEARCH_DOCUMENT_ANALYZE_IMAGE_STORAGE=base64
JARVIS_ALIYUN_OPENSEARCH_DOCUMENT_ANALYZE_ENABLE_SEMANTIC=true
JARVIS_OPENSEARCH_BASE_URL=http://127.0.0.1:9200
JARVIS_OPENSEARCH_USERNAME=
JARVIS_OPENSEARCH_PASSWORD=
JARVIS_OPENSEARCH_INDEX_PREFIX=kb_wikipedia
JARVIS_OPENSEARCH_BULK_BATCH_SIZE=100
JARVIS_OPENSEARCH_BULK_MAX_RETRIES=4
JARVIS_DEEPSEEK_API_KEY=sk-...
JARVIS_DEEPSEEK_BASE_URL=https://api.deepseek.com
JARVIS_DEEPSEEK_MODEL=deepseek-v4-pro
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

`JARVIS_TAVILY_API_KEY` enables the external `tavily_search` skill when installed under `data/skills/`.

`JARVIS_DASHSCOPE_API_KEY` and the related DashScope settings are reserved for the knowledge base embedding pipeline. The default model is `text-embedding-v4` and the default base URL is the Beijing region endpoint.

`JARVIS_ALIYUN_OPENSEARCH_*` settings are reserved for SEC PDF document parsing through Alibaba Cloud AI Search Open Platform. The current implementation targets the async document analyze API with `ops-document-analyze-002`.

`JARVIS_OPENSEARCH_*` settings are reserved for the knowledge base index and search pipeline. The default OpenSearch endpoint is `http://127.0.0.1:9200`.
