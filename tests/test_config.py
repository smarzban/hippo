from pathlib import Path

from hippo.config import Settings


def test_defaults():
    s = Settings(_env_file=None)
    assert s.db_path == Path("hippo.db")
    assert s.chat_model == "openai:gpt-5.2"
    assert s.embedding_model == "text-embedding-3-small"
    assert s.embedding_dim == 1536
    assert s.enrich_enabled is True
    assert s.enrich_model == "openai:gpt-5-mini"
    assert s.chunk_max_chars == 3000
    assert s.chunk_overlap_chars == 200
    assert s.search_top_k == 8
    assert s.max_tool_calls == 15


def test_env_file_provider_keys_ignored(tmp_path):
    env = tmp_path / ".env"
    env.write_text("OPENAI_API_KEY=sk-test\nOPENAI_BASE_URL=http://x\nHIPPO_CHAT_MODEL=openai:foo\n")
    s = Settings(_env_file=env)
    assert s.chat_model == "openai:foo"


def test_env_override(monkeypatch):
    monkeypatch.setenv("HIPPO_CHAT_MODEL", "anthropic:claude-opus-4-8")
    s = Settings(_env_file=None)
    assert s.chat_model == "anthropic:claude-opus-4-8"
