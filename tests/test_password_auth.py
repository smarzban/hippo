# tests/test_password_auth.py
import pytest
from fastapi.testclient import TestClient

from hippo.api import build_app
from hippo.auth import hash_password
from hippo.config import Settings


def _settings(tmp_path, **over):
    base = dict(_env_file=None, db_path=tmp_path / "t.db", embedding_model="fake",
                embedding_dim=32, enrich_enabled=False, auth_mode="password",
                secret_key="test-secret")
    base.update(over)
    return Settings(**base)


def _app_with_owner(tmp_path, email="owner@x.com", pw="s3cret-pass"):
    app = build_app(_settings(tmp_path))
    app.state.store.set_password(email, hash_password(pw), role="owner")
    return app


def test_password_mode_requires_secret_key(tmp_path):
    with pytest.raises(ValueError, match="HIPPO_SECRET_KEY"):
        build_app(_settings(tmp_path, secret_key=""))


def test_unauthenticated_is_401_and_auth_config_is_public(tmp_path):
    c = TestClient(_app_with_owner(tmp_path))
    assert c.get("/me").status_code == 401
    cfg = c.get("/auth/config")          # public, no secrets
    assert cfg.status_code == 200 and cfg.json() == {"auth_mode": "password"}


def test_login_success_sets_session_and_me_works(tmp_path):
    c = TestClient(_app_with_owner(tmp_path))
    r = c.post("/auth/login", json={"email": "owner@x.com", "password": "s3cret-pass"})
    assert r.status_code == 200 and r.json()["role"] == "owner"
    me = c.get("/me")                    # session cookie carried by the client
    assert me.status_code == 200 and me.json()["email"] == "owner@x.com"
    c.post("/auth/logout")
    assert c.get("/me").status_code == 401


def test_wrong_password_is_generic_401(tmp_path):
    c = TestClient(_app_with_owner(tmp_path))
    r = c.post("/auth/login", json={"email": "owner@x.com", "password": "nope"})
    assert r.status_code == 401
    assert "invalid" in r.json()["detail"].lower()
    # unknown user is the SAME generic error (no account enumeration)
    r2 = c.post("/auth/login", json={"email": "ghost@x.com", "password": "x"})
    assert r2.status_code == 401 and r2.json()["detail"] == r.json()["detail"]


def test_lockout_blocks_even_correct_password(tmp_path):
    c = TestClient(_app_with_owner(tmp_path))
    for _ in range(5):
        c.post("/auth/login", json={"email": "owner@x.com", "password": "wrong"})
    r = c.post("/auth/login", json={"email": "owner@x.com", "password": "s3cret-pass"})
    assert r.status_code == 401 and "locked" in r.json()["detail"].lower()


def test_bearer_token_still_works_in_password_mode(tmp_path):
    app = _app_with_owner(tmp_path)
    tok = app.state.store.create_token("owner@x.com")
    c = TestClient(app)
    assert c.get("/me", headers={"Authorization": f"Bearer {tok}"}).json()["role"] == "owner"
