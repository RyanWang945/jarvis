from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Jarvis"
    environment: str = "development"
    host: str = "127.0.0.1"
    port: int = 8000
    log_level: str = "INFO"
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
    planner_type: str = "llm"
    llm_provider: str = "deepseek"
    llm_timeout_seconds: float = 60.0
    worker_mode: str = "inline"
    worker_max_workers: int = 4
    auto_recover_on_startup: bool = True
    coder_timeout_seconds: int = 1800
    deepseek_api_key: str | None = None
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-v4-pro"
    deepseek_timeout_seconds: float | None = None
    kimi_api_key: str | None = None
    kimi_base_url: str = "https://api.moonshot.cn/v1"
    kimi_model: str = "moonshot-v1-8k"
    gemini_api_key: str | None = None
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai"
    gemini_model: str = "gemini-2.5-flash"
    tavily_api_key: str | None = None
    tavily_base_url: str = "https://api.tavily.com"
    feishu_app_id: str | None = None
    feishu_app_secret: str | None = None
    feishu_bot_name: str = "Jarvis"
    obsidian_vault_path: Path | None = None

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="JARVIS_",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
