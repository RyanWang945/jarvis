from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Jarvis"
    environment: str = "development"
    host: str = "127.0.0.1"
    port: int = 8000
    log_level: str = "INFO"
    workspace_root: Path = Field(default_factory=lambda: Path(__file__).resolve().parents[1])
    log_dir: Path = Field(default=Path("logs"))
    data_dir: Path = Field(default=Path("data"))
    sec_pdf_dir: Path | None = None
    sec_raw_parse_dir: Path | None = None
    knowledge_db_path: Path | None = None
    knowledge_default_language: str = "zh"
    knowledge_default_chunk_profile: str = "medium_overlap_v1"
    dashscope_api_key: str | None = None
    dashscope_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    dashscope_embedding_model: str = "text-embedding-v4"
    dashscope_embedding_batch_size: int = 8
    dashscope_embedding_max_workers: int = 2
    aliyun_opensearch_api_key: str | None = None
    aliyun_opensearch_endpoint: str | None = None
    aliyun_opensearch_workspace: str = "default"
    aliyun_opensearch_document_analyze_service_id: str = "ops-document-analyze-002"
    aliyun_opensearch_document_analyze_image_storage: str = "base64"
    aliyun_opensearch_document_analyze_enable_semantic: bool = True
    opensearch_base_url: str = "http://127.0.0.1:9200"
    opensearch_username: str | None = None
    opensearch_password: str | None = None
    opensearch_index_prefix: str = "kb_wikipedia"
    opensearch_bulk_batch_size: int = 100
    opensearch_bulk_max_retries: int = 4
    knowledge_reranker_base_url: str | None = "http://127.0.0.1:8000"
    knowledge_reranker_timeout_seconds: float = 3.0
    knowledge_reranker_input_top_k: int = 50
    knowledge_reranker_max_length: int = 1024
    llm_provider: str = "deepseek"
    default_model_profile: str | None = None
    default_classifier_profile: str | None = "deepseek-v4-flash"
    llm_timeout_seconds: float = 60.0
    llm_max_context_tokens: int = 12000
    llm_max_output_tokens: int = 2000
    llm_context_safety_buffer: int = 1000
    coder_runtime_provider: str = "codex"
    coder_timeout_seconds: int = 1800
    coder_node_finalizer_llm_enabled: bool = False
    react_runtime_backend: str = "builtin"  # builtin | claude_agent_sdk
    result_aggregator_backend: str = "llm"  # llm | claude_agent_sdk
    repositories_config_path: Path | None = Field(default=Path("data/repositories.json"))
    deepseek_api_key: str | None = None
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-v4-flash"
    deepseek_available_models: str = "deepseek-v4-flash,deepseek-v4-pro"
    deepseek_timeout_seconds: float | None = None
    kimi_api_key: str | None = None
    kimi_base_url: str = "https://api.moonshot.cn/v1"
    kimi_model: str = "moonshot-v1-8k"
    kimi_available_models: str = ""
    gemini_api_key: str | None = None
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai"
    gemini_model: str = "gemini-2.5-flash"
    gemini_available_models: str = ""
    tavily_api_key: str | None = None
    tavily_base_url: str = "https://api.tavily.com"
    xai_api_key: str | None = None
    xai_base_url: str = "https://api.x.ai/v1"
    feishu_app_id: str | None = None
    feishu_app_secret: str | None = None
    feishu_bot_name: str = "Jarvis"
    feishu_progress_updates_enabled: bool = False
    feishu_progress_mode: str = "patch"
    feishu_progress_min_interval_seconds: float = 2.0
    feishu_progress_max_recent_events: int = 5
    feishu_capture_payload_on_error: bool = True
    feishu_capture_payload_always: bool = False
    feishu_payload_log_dir: Path = Field(default=Path("logs/feishu-payloads"))
    feishu_payload_log_max_bytes: int = 256 * 1024
    obsidian_workspace_path: Path | None = None
    obsidian_vault_path: Path | None = None
    default_timezone: str = "Asia/Shanghai"
    mcp_enabled: bool = False
    mcp_config_path: Path | None = None
    mcp_servers_json: str | None = None
    mcp_tools_cache_ttl_seconds: float = 300.0
    otel_enabled: bool = False
    otel_service_name: str = "jarvis-api"
    otel_exporter_otlp_endpoint: str = "http://127.0.0.1:4318"
    otel_exporter_otlp_protocol: str = "http/protobuf"
    otel_traces_sampler: str = "parentbased_traceidratio"
    otel_traces_sampler_arg: float = 1.0
    otel_capture_content: bool = True

    # MySQL configuration
    mysql_host: str = "127.0.0.1"
    mysql_port: int = 3306
    mysql_user: str = "jarvis"
    mysql_password: str = "jarvis"
    mysql_database: str = "jarvis"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="JARVIS_",
        extra="ignore",
    )

    @field_validator("obsidian_workspace_path", "obsidian_vault_path", mode="before")
    @classmethod
    def _empty_path_as_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
