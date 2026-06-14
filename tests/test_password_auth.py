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


def test_failed_login_emits_warning_log(tmp_path, caplog):
    """MED-14: failed password logins / lockouts emit WARNINGs so an operator can
    detect brute-force/credential-stuffing — the password surface otherwise has no
    failed-attempt telemetry (contrast verify_request, which logs bearer/IAP denials)."""
    import logging
    c = TestClient(_app_with_owner(tmp_path))
    with caplog.at_level(logging.WARNING, logger="hippo.auth"):
        c.post("/auth/login", json={"email": "owner@x.com", "password": "nope"})
    assert any("bad password" in r.getMessage() and "owner@x.com" in r.getMessage()
               for r in caplog.records)

    # an unknown user is logged distinctly (the client still gets the generic 401)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="hippo.auth"):
        c.post("/auth/login", json={"email": "ghost@x.com", "password": "x"})
    assert any("no local credential" in r.getMessage() for r in caplog.records)

    # repeated failures trip a lockout WARNING
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="hippo.auth"):
        for _ in range(6):
            c.post("/auth/login", json={"email": "owner@x.com", "password": "nope"})
    assert any("locked" in r.getMessage() for r in caplog.records)


def test_lockout_blocks_even_correct_password(tmp_path):
    c = TestClient(_app_with_owner(tmp_path))
    for _ in range(5):
        c.post("/auth/login", json={"email": "owner@x.com", "password": "wrong"})
    r = c.post("/auth/login", json={"email": "owner@x.com", "password": "s3cret-pass"})
    assert r.status_code == 401 and "locked" in r.json()["detail"].lower()


def test_correct_password_works_after_lockout_window_expires(tmp_path):
    """MED-20 (integration) + LOW-15: once the lockout window elapses, the account
    unlocks and a correct password logs in again (the counter is decayed first, so it
    isn't instantly re-locked)."""
    app = _app_with_owner(tmp_path)
    c = TestClient(app)
    for _ in range(5):
        c.post("/auth/login", json={"email": "owner@x.com", "password": "wrong"})
    assert c.post("/auth/login",
                  json={"email": "owner@x.com", "password": "s3cret-pass"}).status_code == 401
    # force the window into the past (simulate the 15 minutes elapsing)
    with app.state.store.con:
        app.state.store.con.execute(
            "UPDATE users SET locked_until=datetime('now','-1 minute') WHERE email=?",
            ("owner@x.com",))
    r = c.post("/auth/login", json={"email": "owner@x.com", "password": "s3cret-pass"})
    assert r.status_code == 200 and r.json()["role"] == "owner"


def test_privileged_mutation_emits_audit_log(tmp_path, caplog):
    """MED-13: a privileged mutation (admin password reset) emits a hippo.audit line
    naming the actor and target, so post-incident 'who did what' is answerable."""
    import logging
    app = _app_with_owner(tmp_path)
    store = app.state.store
    store.set_password("dev@x.com", hash_password("devpass12"), role="user")
    c = TestClient(app)
    c.post("/auth/login", json={"email": "owner@x.com", "password": "s3cret-pass"})
    with caplog.at_level(logging.INFO, logger="hippo.audit"):
        assert c.post("/users/dev@x.com/password").status_code == 200
    msgs = [r.getMessage() for r in caplog.records if r.name == "hippo.audit"]
    assert any("password reset" in m and "owner@x.com" in m and "dev@x.com" in m for m in msgs)


def test_bearer_token_still_works_in_password_mode(tmp_path):
    app = _app_with_owner(tmp_path)
    tok = app.state.store.create_token("owner@x.com")
    c = TestClient(app)
    assert c.get("/me", headers={"Authorization": f"Bearer {tok}"}).json()["role"] == "owner"


MIN_PW_LEN = 8


def test_self_service_password_change(tmp_path):
    c = TestClient(_app_with_owner(tmp_path))
    c.post("/auth/login", json={"email": "owner@x.com", "password": "s3cret-pass"})
    # wrong current → 403
    assert c.post("/me/password", json={"current": "nope", "new": "brandnew-pass"}).status_code == 403
    # too short → 400
    assert c.post("/me/password", json={"current": "s3cret-pass", "new": "short"}).status_code == 400
    # ok
    assert c.post("/me/password", json={"current": "s3cret-pass", "new": "brandnew-pass"}).status_code == 200
    c.post("/auth/logout")
    assert c.post("/auth/login", json={"email": "owner@x.com", "password": "brandnew-pass"}).status_code == 200


def test_admin_reset_returns_secret_once_and_is_gated(tmp_path):
    app = _app_with_owner(tmp_path)
    app.state.store.set_password("dev@x.com", hash_password("old-pass-dev"), role="user")
    c = TestClient(app)
    c.post("/auth/login", json={"email": "owner@x.com", "password": "s3cret-pass"})
    r = c.post("/users/dev@x.com/password", json={})
    assert r.status_code == 200 and len(r.json()["password"]) >= MIN_PW_LEN
    new_pw = r.json()["password"]
    # the reset password actually works
    c.post("/auth/logout")
    assert c.post("/auth/login", json={"email": "dev@x.com", "password": new_pw}).status_code == 200


def test_admin_reset_requires_admin(tmp_path):
    app = _app_with_owner(tmp_path)
    app.state.store.set_password("dev@x.com", hash_password("p"), role="user")
    c = TestClient(app)
    c.post("/auth/login", json={"email": "dev@x.com", "password": "p"})  # rank user
    assert c.post("/users/owner@x.com/password", json={}).status_code == 403


def test_admin_cannot_reset_bootstrap_owner_by_stored_role(tmp_path):
    """Regression (PR #12 review): the tier check must use the EFFECTIVE role. A
    HIPPO_ADMIN_EMAILS user is owner at request time even if their stored role is
    lower, so a rank-1 admin must not be able to reset their local password."""
    app = build_app(_settings(tmp_path, admin_emails="boss@x.com"))
    store = app.state.store
    # boss is a bootstrap owner but only has a 'user' stored role + a local password
    store.set_password("boss@x.com", hash_password("boss-pass"), role="user")
    store.set_password("mgr@x.com", hash_password("mgr-pass"), role="admin")
    c = TestClient(app)
    c.post("/auth/login", json={"email": "mgr@x.com", "password": "mgr-pass"})  # rank admin
    # stored role is 'user' (< admin) but effective role is owner → must be 403
    assert c.post("/users/boss@x.com/password", json={}).status_code == 403
