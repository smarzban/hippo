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


def test_setup_status_public_and_setup_token_gate(tmp_path):
    s = _settings(tmp_path, setup_token="let-me-in")
    c = TestClient(build_app(s))
    st = c.get("/setup/status")
    assert st.status_code == 200
    assert st.json()["setup_complete"] is False
    assert set(st.json()["auth_modes_available"]) == {"password", "oidc", "iap"}  # no 'none'
    # wrong/absent token rejected
    assert c.post("/setup", json={"token": "nope", "auth_mode": "password",
                                  "owner_email": "o@x.com", "owner_password": "ownerpass1",
                                  "models": {}}).status_code in (401, 403)


def test_password_setup_happy_path(tmp_path):
    s = _settings(tmp_path, setup_token="let-me-in", secret_key="k")
    app = build_app(s)
    c = TestClient(app)
    r = c.post("/setup", json={
        "token": "let-me-in", "auth_mode": "password",
        "owner_email": "owner@x.com", "owner_password": "ownerpass1",
        "roots": {"user": "Team", "admin": "Managers", "owner": "Execs"},
        "models": {"chat_model": "ollama:llama3", "embedding_model": "fake", "embedding_dim": 32},
    })
    assert r.status_code == 200
    assert app.state.store.is_setup_complete() is True
    # owner can log in immediately
    assert c.post("/auth/login", json={"email": "owner@x.com", "password": "ownerpass1"}).json()["role"] == "owner"
    # roots were renamed + models persisted
    names = {f.min_role: f.name for f in app.state.store.list_folders(role="owner") if f.parent_id is None}
    assert names == {"user": "Team", "admin": "Managers", "owner": "Execs"}
    assert app.state.store.get_config("chat_model") == "ollama:llama3"
    # re-running setup after completion is refused
    assert c.post("/setup", json={"token": "let-me-in", "auth_mode": "password",
                                  "owner_email": "x@x.com", "owner_password": "xxxxxxxx",
                                  "models": {}}).status_code == 409


def test_oidc_setup_refuses_without_secret_env(tmp_path):
    s = _settings(tmp_path, setup_token="t", secret_key="", oidc_client_secret="")
    c = TestClient(build_app(s))
    r = c.post("/setup", json={"token": "t", "auth_mode": "oidc", "owner_email": "o@x.com",
                               "oidc": {"client_id": "cid", "public_url": "https://h"}, "models": {}})
    assert r.status_code == 400 and "secret" in r.json()["detail"].lower()
