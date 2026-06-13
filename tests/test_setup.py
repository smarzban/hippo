# tests/test_setup.py
from fastapi.testclient import TestClient

from hippo.api import build_app
from hippo.config import Settings


def _settings(tmp_path, **over):
    base = dict(_env_file=None, db_path=tmp_path / "t.db", embedding_model="fake",
                embedding_dim=32, enrich_enabled=False)
    base.update(over)
    return Settings(**base)


def test_db_config_overrides_chat_model_live(tmp_path):
    s = _settings(tmp_path, chat_model="env:model")
    app = build_app(s)
    # set a DB override AFTER construction; chat_model must be read live
    app.state.store.set_config("chat_model", "db:model")
    # build_app exposes the live resolver for the chat route; assert via a helper
    from hippo.config import Config
    assert Config(s, app.state.store).get("chat_model") == "db:model"


def test_auth_mode_resolved_from_db_overlay_at_construction(tmp_path):
    # pre-seed a DB with auth_mode=password BEFORE build_app, env says none
    from hippo.db import connect
    from hippo.embeddings import FakeEmbedder
    from hippo.storage import Storage
    con = connect(tmp_path / "t.db", embedding_dim=32)
    Storage(con, FakeEmbedder(dim=32)).set_config("auth_mode", "password")
    con.close()
    s = _settings(tmp_path, auth_mode="none", secret_key="k")
    app = build_app(s)
    c = TestClient(app)
    # password mode is active (from the DB overlay): /me is 401, /auth/config says password
    assert c.get("/auth/config").json()["auth_mode"] == "password"
    assert c.get("/me").status_code == 401
