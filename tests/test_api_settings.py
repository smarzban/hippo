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
