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


def test_auth_defaults_off():
    s = Settings(_env_file=None)
    assert s.auth_mode == "none"
    assert s.allowed_domain == ""
    assert s.admin_email_list == set()
    assert s.source_root_list == []


def test_admin_emails_parsed_and_lowercased():
    s = Settings(_env_file=None, admin_emails="A@x.com, b@x.com ,")
    assert s.admin_email_list == {"a@x.com", "b@x.com"}


def test_source_roots_colon_separated(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    s = Settings(_env_file=None, source_roots=f"{a}:{b}")
    assert s.source_root_list == [a.resolve(), b.resolve()]


def test_auth_mode_typo_rejected():
    import pytest
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        Settings(_env_file=None, auth_mode="oidcc")


def test_password_is_a_valid_auth_mode():
    from hippo.config import Settings
    s = Settings(_env_file=None, auth_mode="password")
    assert s.auth_mode == "password"


def test_slack_settings_defaults_off():
    s = Settings(_env_file=None)
    assert s.slack_enabled is False
    assert s.slack_bot_token == ""
    assert s.slack_app_token == ""


def test_slack_settings_from_env(monkeypatch):
    monkeypatch.setenv("HIPPO_SLACK_ENABLED", "true")
    monkeypatch.setenv("HIPPO_SLACK_BOT_TOKEN", "xoxb-abc")
    monkeypatch.setenv("HIPPO_SLACK_APP_TOKEN", "xapp-xyz")
    s = Settings(_env_file=None)
    assert s.slack_enabled is True
    assert s.slack_bot_token == "xoxb-abc"
    assert s.slack_app_token == "xapp-xyz"


def test_config_overlay_db_overrides_env_for_operational_keys(tmp_path):
    from hippo.config import Config, Settings, DB_OVERRIDABLE
    from hippo.db import connect
    from hippo.embeddings import FakeEmbedder
    from hippo.storage import Storage

    s = Settings(_env_file=None, chat_model="env-model", github_token="SECRET")
    store = Storage(connect(tmp_path / "t.db", embedding_dim=32), FakeEmbedder(dim=32))
    cfg = Config(s, store)
    assert cfg.get("chat_model") == "env-model"          # env default
    store.set_config("chat_model", "db-model")
    assert cfg.get("chat_model") == "db-model"           # DB overrides
    assert "chat_model" in DB_OVERRIDABLE
    # a secret/env-only key is NEVER sourced from the DB
    store.set_config("github_token", "DB-LEAK")
    assert cfg.get("github_token") == "SECRET"           # still env
    # embedding_dim is coerced to int
    store.set_config("embedding_dim", "768")
    assert cfg.get("embedding_dim") == 768


def test_setup_token_is_an_env_only_setting(tmp_path):
    from hippo.config import Settings, DB_OVERRIDABLE
    s = Settings(_env_file=None, setup_token="abc")
    assert s.setup_token == "abc"
    assert "setup_token" not in DB_OVERRIDABLE
