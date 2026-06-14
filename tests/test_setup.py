# tests/test_setup.py
from fastapi.testclient import TestClient

from hippo.api import build_app
from hippo.auth import IapVerifier
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
    # wrong/absent token rejected — deterministically 403 (forbidden), not 401 (LOW-41):
    # the endpoint returns 403 'invalid setup token', and a regression to 401 (which some
    # clients treat as "retry login") would be a different security semantic.
    r = c.post("/setup", json={"token": "nope", "auth_mode": "password",
                               "owner_email": "o@x.com", "owner_password": "ownerpass1",
                               "models": {}})
    assert r.status_code == 403 and "token" in r.json()["detail"].lower()


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


def test_config_get_put_owner_only_and_secrets_protected(tmp_path):
    app = build_app(_settings(tmp_path))  # none-mode caller is owner
    c = TestClient(app)
    got = c.get("/config")
    assert got.status_code == 200
    assert "chat_model" in got.json() and "secret_key" not in got.json() \
        and "github_token" not in got.json()
    # set an operational key
    assert c.put("/config", json={"chat_model": "ollama:llama3"}).status_code == 200
    assert app.state.store.get_config("chat_model") == "ollama:llama3"
    # writing a secret/env-only key is rejected
    r = c.put("/config", json={"secret_key": "leak"})
    assert r.status_code == 400 and "secret_key" in r.json()["detail"]
    # unknown key rejected
    assert c.put("/config", json={"nonsense": "x"}).status_code == 400


def test_embedding_keys_are_env_only_not_db_overridable(tmp_path):
    """MED-07: embedding_model/dim are env-only — the env-built embedder is the single
    source of truth, so a DB overlay could neither take effect (chunk_vec dim is fixed)
    nor stay accurate after a reindex. PUT /config rejects them as non-overridable, and
    they never appear in GET /config; /settings/status reports the env value."""
    app = build_app(_settings(tmp_path))   # fake embedder, dim=32
    c = TestClient(app)
    for key, val in (("embedding_dim", 128), ("embedding_model", "other")):
        r = c.put("/config", json={key: val})
        assert r.status_code == 400 and "not a settable operational key" in r.json()["detail"]
    # GET /config exposes only the DB-overridable keys (no embedding_*)
    got = c.get("/config").json()
    assert "embedding_model" not in got and "embedding_dim" not in got
    assert "chat_model" in got
    # status still reports the true (env) embedding model
    assert c.get("/settings/status").json()["embedding_model"] == "fake"


def test_auth_switch_blocked_when_owner_lacks_target_credential(tmp_path):
    # none-mode owner has no password; switching to password would lock everyone out
    app = build_app(_settings(tmp_path, secret_key="k"))
    c = TestClient(app)
    r = c.put("/config", json={"auth_mode": "password"})
    assert r.status_code == 400 and "password" in r.json()["detail"].lower()


def test_auth_switch_to_none_is_rejected(tmp_path):
    """LOW-42: re-opening a secured instance by switching auth_mode to the open 'none'
    mode is the most dangerous downgrade — it must be explicitly refused (only
    password/oidc/iap are valid switch targets)."""
    c = TestClient(build_app(_settings(tmp_path, secret_key="k")))
    r = c.put("/config", json={"auth_mode": "none"})
    assert r.status_code == 400
    detail = r.json()["detail"].lower()
    assert "password" in detail and "oidc" in detail and "iap" in detail


def test_auth_switch_to_password_allowed_once_owner_has_password(tmp_path):
    app = build_app(_settings(tmp_path, secret_key="k"))
    # none-mode caller is the "local" owner; give a real owner a password first
    app.state.store.set_password("owner@x.com", __import__("hippo.auth", fromlist=["hash_password"]).hash_password("ownerpass1"), role="owner")
    c = TestClient(app)
    # switching is allowed because an owner holds a valid password credential
    assert c.put("/config", json={"auth_mode": "password"}).status_code == 200


def test_auth_switch_to_mode_missing_secret_env_rejected(tmp_path):
    app = build_app(_settings(tmp_path, secret_key=""))   # no secret key
    c = TestClient(app)
    r = c.put("/config", json={"auth_mode": "oidc"})
    assert r.status_code == 400 and "secret" in r.json()["detail"].lower()


def test_allowed_domain_db_override_gates_role_resolution_live(tmp_path):
    # iap mode, env has NO domain restriction. Set allowed_domain via the DB
    # overlay; a bearer token for an out-of-domain email must be rejected (403)
    # while an in-domain one works — proving the override gates live.
    s = _settings(tmp_path, auth_mode="iap", iap_audience="aud")
    app = build_app(s, iap_verifier=IapVerifier("aud", key_fetcher=lambda: {}))
    store = app.state.store
    store.set_config("allowed_domain", "x.com")
    in_dom = store.create_token("a@x.com")
    out_dom = store.create_token("a@y.com")
    c = TestClient(app)
    assert c.get("/me", headers={"Authorization": f"Bearer {in_dom}"}).status_code == 200
    assert c.get("/me", headers={"Authorization": f"Bearer {out_dom}"}).status_code == 403


def test_auth_switch_to_iap_requires_audience(tmp_path):
    # switching to iap with no iap_audience configured must be rejected (would brick)
    app = build_app(_settings(tmp_path))
    c = TestClient(app)
    r = c.put("/config", json={"auth_mode": "iap"})
    assert r.status_code == 400 and "iap_audience" in r.json()["detail"].lower()
    # once an audience is set (same request body), the switch is allowed
    assert c.put("/config", json={"auth_mode": "iap", "iap_audience": "aud"}).status_code == 200


def test_auth_switch_to_oidc_requires_client_id(tmp_path):
    # secret_key + client_secret present, but no oidc_client_id configured -> 400
    app = build_app(_settings(tmp_path, secret_key="k", oidc_client_secret="cs"))
    c = TestClient(app)
    r = c.put("/config", json={"auth_mode": "oidc"})
    assert r.status_code == 400 and "client_id" in r.json()["detail"].lower()
    # providing the client_id in the same request makes it usable -> ok
    assert c.put("/config", json={"auth_mode": "oidc",
                                  "oidc_client_id": "cid"}).status_code == 200


def test_setup_oidc_requires_client_id_in_body(tmp_path):
    s = _settings(tmp_path, setup_token="t", secret_key="k", oidc_client_secret="cs")
    c = TestClient(build_app(s))
    r = c.post("/setup", json={"token": "t", "auth_mode": "oidc", "owner_email": "o@x.com",
                               "oidc": {"public_url": "https://h"}, "models": {}})
    assert r.status_code == 400 and "client_id" in r.json()["detail"].lower()


def test_setup_iap_requires_audience_in_body(tmp_path):
    s = _settings(tmp_path, setup_token="t")
    c = TestClient(build_app(s))
    r = c.post("/setup", json={"token": "t", "auth_mode": "iap", "owner_email": "o@x.com",
                               "models": {}})
    assert r.status_code == 400 and "iap_audience" in r.json()["detail"].lower()
    # providing it in the body succeeds
    s2 = _settings(tmp_path, setup_token="t", db_path=tmp_path / "t2.db")
    c2 = TestClient(build_app(s2))
    assert c2.post("/setup", json={"token": "t", "auth_mode": "iap", "owner_email": "o@x.com",
                                   "iap_audience": "aud", "models": {}}).status_code == 200


def test_put_config_embedding_dim_rejected_as_non_overridable(tmp_path):
    """embedding_dim is env-only (MED-07): PUT /config rejects it as non-overridable."""
    app = build_app(_settings(tmp_path))
    c = TestClient(app)
    r = c.put("/config", json={"embedding_dim": "abc"})
    assert r.status_code == 400 and "not a settable operational key" in r.json()["detail"]


def test_setup_ignores_embedding_keys(tmp_path):
    """MED-07: the wizard may still send embedding_model/dim, but POST /setup never
    persists them to the overlay (env is authoritative) — so setup succeeds and no
    embedding config row is written, even for a junk value."""
    s = _settings(tmp_path, setup_token="t", secret_key="k")
    app = build_app(s)
    c = TestClient(app)
    r = c.post("/setup", json={"token": "t", "auth_mode": "password", "owner_email": "o@x.com",
                               "owner_password": "ownerpass1",
                               "models": {"chat_model": "m", "embedding_dim": "abc",
                                          "embedding_model": "other"}})
    assert r.status_code == 200
    assert app.state.store.get_config("embedding_model") is None
    assert app.state.store.get_config("embedding_dim") is None
    assert app.state.store.get_config("chat_model") == "m"   # DB-overridable key still persists
