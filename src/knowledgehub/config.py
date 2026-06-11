from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All knobs. Override via env vars prefixed HUB_ (e.g. HUB_CHAT_MODEL)."""

    model_config = SettingsConfigDict(env_prefix="HUB_", env_file=".env")

    db_path: Path = Path("hub.db")
    chat_model: str = "openai:gpt-5.2"
    embedding_model: str = "text-embedding-3-small"
    embedding_dim: int = 1536
    enrich_enabled: bool = True
    enrich_model: str = "openai:gpt-5-mini"
    chunk_max_chars: int = 3000  # ~750 tokens
    chunk_overlap_chars: int = 200
    max_tool_calls: int = 15
    search_top_k: int = 8


def get_settings() -> Settings:
    return Settings()
