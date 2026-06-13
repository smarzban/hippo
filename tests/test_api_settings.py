from fastapi.testclient import TestClient
from hippo.api import build_app
from hippo.config import Settings


def _settings(tmp_path, **kw):
    base = dict(db_path=tmp_path / "h.db", embedding_model="fake", embedding_dim=8,
                enrich_enabled=False, auth_mode="iap", iap_audience="aud",
                admin_emails="boss@x.com")
    base.update(kw)
    return Settings(_env_file=None, **base)


def _app(tmp_path, **kw):
    # iap mode with no IAP key_fetcher => only bearer tokens authenticate (zero-network).
    from hippo.auth import IapVerifier
    app = build_app(_settings(tmp_path, **kw), iap_verifier=IapVerifier("aud", key_fetcher=lambda: {}))
    return app, app.state.store


def _bearer(store, email):
    return {"Authorization": f"Bearer {store.create_token(email)}"}


def test_users_list_and_set_role_admin_only(tmp_path):
    app, store = _app(tmp_path)
    store.ensure_user("dev@x.com")
    admin, dev = _bearer(store, "boss@x.com"), _bearer(store, "dev@x.com")
    c = TestClient(app)
    # developer is forbidden
    assert c.get("/users", headers=dev).status_code == 403
    # admin lists + promotes
    assert c.get("/users", headers=admin).status_code == 200
    assert c.put("/users/dev@x.com/role", json={"role": "manager"}, headers=admin).status_code == 200
    assert any(u["email"] == "dev@x.com" and u["role"] == "manager"
               for u in c.get("/users", headers=admin).json())


def test_set_role_rejects_invalid_and_self_demotion(tmp_path):
    app, store = _app(tmp_path)
    admin = _bearer(store, "boss@x.com")
    c = TestClient(app)
    assert c.put("/users/dev@x.com/role", json={"role": "wizard"}, headers=admin).status_code == 400
    # anti-lockout: admin cannot demote their own account
    assert c.put("/users/boss@x.com/role", json={"role": "developer"}, headers=admin).status_code == 400


def test_tokens_self_service_and_secret_once(tmp_path):
    app, store = _app(tmp_path)
    dev = _bearer(store, "dev@x.com")
    c = TestClient(app)
    created = c.post("/tokens", json={"name": "laptop"}, headers=dev)
    assert created.status_code == 200
    body = created.json()
    assert body["token"].startswith("hk_")          # secret returned once
    # listing shows metadata only — never the secret
    listed = c.get("/tokens", headers=dev).json()
    assert any(t["id"] == body["id"] and t["name"] == "laptop" for t in listed)
    assert all("token" not in t and "hk_" not in str(t.values()) for t in listed)


def test_tokens_cross_user_revoke_blocked_for_dev_allowed_for_admin(tmp_path):
    app, store = _app(tmp_path)
    dev, admin = _bearer(store, "dev@x.com"), _bearer(store, "boss@x.com")
    c = TestClient(app)
    other_id = int(c.post("/tokens", json={"name": "x"}, headers=admin).json()["id"])  # admin's token
    # developer cannot delete someone else's token
    assert c.delete(f"/tokens/{other_id}", headers=dev).status_code == 404
    # admin can
    assert c.delete(f"/tokens/{other_id}", headers=admin).status_code == 200


def test_tokens_all_view_is_admin_only(tmp_path):
    app, store = _app(tmp_path)
    dev, admin = _bearer(store, "dev@x.com"), _bearer(store, "boss@x.com")
    c = TestClient(app)
    c.post("/tokens", json={"name": "d"}, headers=dev)
    assert c.get("/tokens?all=true", headers=dev).status_code == 403
    all_rows = c.get("/tokens?all=true", headers=admin).json()
    assert any(t.get("email") == "dev@x.com" for t in all_rows)


def test_developer_can_revoke_own_token(tmp_path):
    app, store = _app(tmp_path)
    dev = _bearer(store, "dev@x.com")
    c = TestClient(app)
    tid = c.post("/tokens", json={"name": "mine"}, headers=dev).json()["id"]
    assert c.delete(f"/tokens/{tid}", headers=dev).status_code == 200       # self-service success
    assert all(t["id"] != tid for t in c.get("/tokens", headers=dev).json())


def test_users_shows_effective_role_and_blocks_bootstrap_demotion(tmp_path):
    app, store = _app(tmp_path, admin_emails="boss@x.com,boss2@x.com")
    boss2 = _bearer(store, "boss2@x.com")
    store.ensure_user("boss@x.com")   # stored 'developer', but a bootstrap admin
    c = TestClient(app)
    users = {u["email"]: u["role"] for u in c.get("/users", headers=boss2).json()}
    assert users["boss@x.com"] == "admin"   # EFFECTIVE role shown, not stale 'developer'
    # another admin can't demote a bootstrap admin (resolve_role re-promotes => no-op/lie)
    assert c.put("/users/boss@x.com/role", json={"role": "developer"},
                 headers=boss2).status_code == 400


def test_resync_missing_folder_does_not_wipe(tmp_path):
    app, store = _app(tmp_path)
    admin = _bearer(store, "boss@x.com")
    missing = tmp_path / "gone"
    sid = store.register_source("folder", str(missing), access="everyone")
    c = TestClient(app)
    # path isn't a directory -> 400, NOT a sync that would delete the source's docs
    assert c.post(f"/sources/{sid}/resync", headers=admin).status_code == 400


def test_resync_known_and_unknown(tmp_path):
    app, store = _app(tmp_path)
    admin = _bearer(store, "boss@x.com")
    sid = store.register_source("folder", str(tmp_path), access="everyone")
    c = TestClient(app)
    r = c.post(f"/sources/{sid}/resync", headers=admin)
    assert r.status_code == 200 and "report" in r.json()
    assert c.post("/sources/99999/resync", headers=admin).status_code == 404
    assert c.post(f"/sources/{sid}/resync", headers=_bearer(store, "dev@x.com")).status_code == 403


def test_status_admin_only_and_no_secrets(tmp_path):
    app, store = _app(tmp_path, chat_model="openai:gpt-5.2")
    admin, dev = _bearer(store, "boss@x.com"), _bearer(store, "dev@x.com")
    c = TestClient(app)
    assert c.get("/settings/status", headers=dev).status_code == 403
    st = c.get("/settings/status", headers=admin).json()
    assert st["auth_mode"] == "iap" and st["chat_model"] == "openai:gpt-5.2"
    assert set(st["counts"]) == {"documents", "sources", "users"}
    assert "hk_" not in str(st) and "secret" not in str(st).lower()
