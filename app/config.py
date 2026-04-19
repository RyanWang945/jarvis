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
    planner_type: str = "llm"
    worker_mode: str = "inline"
    worker_max_workers: int = 4
    deepseek_api_key: str | None = None
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"
    deepseek_timeout_seconds: float = 60.0
    tavily_api_key: str | None = None
    tavily_base_url: str = "https://api.tavily.com"
    feishu_app_id: str | None = None
    feishu_app_secret: str | None = None
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
