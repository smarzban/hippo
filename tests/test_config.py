from pathlib import Path

from knowledgehub.config import Settings


def test_defaults():
    s = Settings(_env_file=None)
    assert s.db_path == Path("hub.db")
    assert s.chat_model == "openai:gpt-5.2"
    assert s.embedding_model == "text-embedding-3-small"
    assert s.embedding_dim == 1536
    assert s.enrich_enabled is True
    assert s.enrich_model == "openai:gpt-5-mini"
    assert s.chunk_max_chars == 3000
    assert s.max_tool_calls == 15


def test_env_override(monkeypatch):
    monkeypatch.setenv("HUB_CHAT_MODEL", "anthropic:claude-opus-4-8")
    s = Settings(_env_file=None)
    assert s.chat_model == "anthropic:claude-opus-4-8"
