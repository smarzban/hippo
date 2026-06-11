from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All knobs. Override via env vars prefixed HIPPO_ (e.g. HIPPO_CHAT_MODEL)."""

    # extra="ignore": .env also holds provider keys (OPENAI_API_KEY etc.) that
    # belong to the process environment, not to Settings.
    model_config = SettingsConfigDict(env_prefix="HIPPO_", env_file=".env", extra="ignore")

    db_path: Path = Path("hippo.db")
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
