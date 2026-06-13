import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from hippo.api import build_app
from hippo.auth import IapVerifier
from hippo.chunking import Chunk
from hippo.config import Settings

# Module-level RSA key for OIDC flow tests — generated once, shared across tests.
_OIDC_RSA_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_OIDC_KEY_FETCHER = lambda: {"oidc1": _OIDC_RSA_KEY.public_key()}  # noqa: E731

AUD = "/projects/1/global/backendServices/2"


def _settings(tmp_path, **over):
    base = dict(_env_file=None, db_path=tmp_path / "t.db", embedding_model="fake",
                embedding_dim=32, enrich_enabled=False)
    base.update(over)
    return Settings(**base)


def test_none_mode_is_implicit_owner(tmp_path):
    app = build_app(_settings(tmp_path))
    c = TestClient(app)
    assert c.get("/health").status_code == 200
    me = c.get("/me").json()
    assert me["role"] == "owner" and me["auth_mode"] == "none"


def test_iap_mode_rejects_without_assertion(tmp_path):
    s = _settings(tmp_path, auth_mode="iap", iap_audience=AUD)
    key = ec.generate_private_key(ec.SECP256R1())
    verifier = IapVerifier(AUD, key_fetcher=lambda: {"k1": key.public_key()})
    app = build_app(s, iap_verifier=verifier)
    c = TestClient(app)
    assert c.get("/documents").status_code == 401
    assertion = jwt.encode(
        {"aud": AUD, "iss": "https://cloud.google.com/iap",
         "exp": int(time.time()) + 600, "email": "dev@x.com"},
        key, algorithm="ES256", headers={"kid": "k1"})
    r = c.get("/me", headers={"x-goog-iap-jwt-assertion": assertion})
    assert r.status_code == 200 and r.json() == {
        "email": "dev@x.com", "role": "user", "auth_mode": "iap"}


def test_domain_gate_403(tmp_path):
    s = _settings(tmp_path, auth_mode="iap", iap_audience=AUD, allowed_domain="x.com")
    key = ec.generate_private_key(ec.SECP256R1())
    app = build_app(s, iap_verifier=IapVerifier(AUD, key_fetcher=lambda: {"k1": key.public_key()}))
    bad = jwt.encode({"aud": AUD, "iss": "https://cloud.google.com/iap",
                      "exp": int(time.time()) + 600, "email": "evil@gmail.com"},
                     key, algorithm="ES256", headers={"kid": "k1"})
    assert TestClient(app).get("/me", headers={"x-goog-iap-jwt-assertion": bad}).status_code == 403


def test_bearer_token_works_in_any_mode_and_env_admins_promoted(tmp_path):
    s = _settings(tmp_path, auth_mode="iap", iap_audience=AUD, admin_emails="boss@x.com")
    app = build_app(s, iap_verifier=IapVerifier(AUD, key_fetcher=lambda: {}))
    store = app.state.store
    t_dev = store.create_token("dev@x.com")
    t_boss = store.create_token("boss@x.com")
    c = TestClient(app)
    assert c.get("/me", headers={"Authorization": f"Bearer {t_dev}"}).json()["role"] == "user"
    assert c.get("/me", headers={"Authorization": f"Bearer {t_boss}"}).json()["role"] == "owner"
    assert c.get("/me", headers={"Authorization": "Bearer hk_bogus"}).status_code == 401


def test_role_filtering_through_api(tmp_path):
    """Admin-tier documents are hidden from user-tier callers."""
    s = _settings(tmp_path, auth_mode="iap", iap_audience=AUD, admin_emails="boss@x.com")
    app = build_app(s, iap_verifier=IapVerifier(AUD, key_fetcher=lambda: {}))
    store = app.state.store
    # find the Private (admin-tier) root
    private_id = store.con.execute(
        "SELECT id FROM folders WHERE min_role='admin' AND parent_id IS NULL").fetchone()[0]
    # ingest a doc into the admin-tier folder
    from hippo.ingest import Ingestor
    ing = Ingestor(store, max_chars=3000, overlap_chars=200)
    ing.ingest_bytes("comp.md", b"# Comp\n\nsecret", folder_id=private_id,
                     path_prefix="Private")
    c = TestClient(app)
    dev = {"Authorization": f"Bearer {store.create_token('dev@x.com')}"}
    boss = {"Authorization": f"Bearer {store.create_token('boss@x.com')}"}
    # user-tier caller cannot see admin-tier doc
    assert all(d["path"] != "Private/comp.md" for d in c.get("/documents", headers=dev).json())
    # owner/admin can see it
    admin_docs = c.get("/documents", headers=boss).json()
    assert any(d["path"] == "Private/comp.md" for d in admin_docs)
    doc_id = next(d["id"] for d in admin_docs if d["path"] == "Private/comp.md")
    assert c.get(f"/documents/{doc_id}", headers=dev).status_code == 404
    assert c.get(f"/documents/{doc_id}", headers=boss).status_code == 200


def _fake_exchange(seen=None):
    claims = {"iss": "https://accounts.google.com", "aud": "cid",
              "exp": int(time.time()) + 600, "email": "u@x.com", "email_verified": True}

    def exchange(code, settings, *, client_id, public_url):
        assert code == "authcode"
        if seen is not None:
            seen.update(client_id=client_id, public_url=public_url)
        return {"id_token": jwt.encode(claims, _OIDC_RSA_KEY, algorithm="RS256",
                                       headers={"kid": "oidc1"})}
    return exchange


def _oidc_app(tmp_path):
    s = _settings(tmp_path, auth_mode="oidc", secret_key="s3cret",
                  oidc_client_id="cid", oidc_client_secret="cs")
    return build_app(s, code_exchanger=_fake_exchange(), google_key_fetcher=_OIDC_KEY_FETCHER)


def test_oidc_full_flow_sets_session(tmp_path):
    c = TestClient(_oidc_app(tmp_path), follow_redirects=False)
    assert c.get("/documents").status_code == 401
    r = c.get("/auth/login")
    assert r.status_code == 307 and "accounts.google.com" in r.headers["location"]
    from urllib.parse import parse_qs, urlparse
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    r = c.get(f"/auth/callback?code=authcode&state={state}")
    assert r.status_code == 307 and r.headers["location"] == "/"
    assert c.get("/me").json()["email"] == "u@x.com"
    c.get("/auth/logout")
    assert c.get("/documents").status_code == 401


def test_oidc_exchange_uses_effective_client_id_and_public_url(tmp_path):
    # env oidc_client_id/public_url differ from the DB overlay; the code exchange
    # must use the EFFECTIVE (overlaid) values, matching login/callback.
    from hippo.db import connect
    from hippo.embeddings import FakeEmbedder
    from hippo.storage import Storage
    con = connect(tmp_path / "t.db", embedding_dim=32)
    st = Storage(con, FakeEmbedder(dim=32))
    st.set_config("oidc_client_id", "cid")          # token validation expects aud=cid
    # http:// so the (non-secure) session cookie round-trips over TestClient's http
    # transport — what matters here is that the OVERLAID value reaches the exchange.
    st.set_config("public_url", "http://overlaid.example")
    con.close()
    s = _settings(tmp_path, auth_mode="oidc", secret_key="s3cret",
                  oidc_client_id="env-cid", oidc_client_secret="cs",
                  public_url="http://env.local")
    seen = {}
    app = build_app(s, code_exchanger=_fake_exchange(seen),
                    google_key_fetcher=_OIDC_KEY_FETCHER)
    c = TestClient(app, follow_redirects=False)
    r = c.get("/auth/login")
    from urllib.parse import parse_qs, urlparse
    qs = parse_qs(urlparse(r.headers["location"]).query)
    # /auth/login itself must redirect with the overlaid client_id + redirect_uri
    assert qs["client_id"] == ["cid"]
    assert qs["redirect_uri"] == ["http://overlaid.example/auth/callback"]
    state = qs["state"][0]
    c.get(f"/auth/callback?code=authcode&state={state}")
    assert seen == {"client_id": "cid", "public_url": "http://overlaid.example"}


def test_oidc_state_mismatch_rejected(tmp_path):
    c = TestClient(_oidc_app(tmp_path), follow_redirects=False)
    c.get("/auth/login")
    assert c.get("/auth/callback?code=authcode&state=forged").status_code == 400


def test_oidc_requires_secret_key(tmp_path):
    with pytest.raises(ValueError):
        build_app(_settings(tmp_path, auth_mode="oidc", oidc_client_id="cid"))


def _iap_app_with_tokens(tmp_path, **settings_over):
    s = _settings(tmp_path, auth_mode="iap", iap_audience=AUD,
                  admin_emails="boss@x.com", **settings_over)
    app = build_app(s, iap_verifier=IapVerifier(AUD, key_fetcher=lambda: {}))
    store = app.state.store
    return (app, store,
            {"Authorization": f"Bearer {store.create_token('dev@x.com')}"},
            {"Authorization": f"Bearer {store.create_token('boss@x.com')}"})


def test_folders_admin_only_and_allowlisted(tmp_path):
    """Only admin+ can create folders; folder-mount checks HIPPO_SOURCE_ROOTS."""
    docs = tmp_path / "roots" / "team"
    docs.mkdir(parents=True)
    (docs / "a.md").write_text("# A\n\nalpha")
    app, store, dev, boss = _iap_app_with_tokens(tmp_path, source_roots=str(tmp_path / "roots"))
    c = TestClient(app)
    rows = c.get("/folders", headers=boss).json()
    default_id = next(r["id"] for r in rows if r["name"] == "Default")
    # user-tier is forbidden from creating folders
    assert c.post("/folders", json={"parent_id": default_id, "name": "X"}, headers=dev).status_code == 403
    # admin can create a folder
    r = c.post("/folders", json={"parent_id": default_id, "name": "Team"}, headers=boss)
    assert r.status_code == 200


def test_folders_registration_refused_without_roots_when_auth_on(tmp_path):
    """Mounting a folder-origin without HIPPO_SOURCE_ROOTS configured is refused."""
    app, _, _, boss = _iap_app_with_tokens(tmp_path)  # no source_roots configured
    c = TestClient(app)
    rows = c.get("/folders", headers=boss).json()
    default_id = next(r["id"] for r in rows if r["name"] == "Default")
    r = c.post("/folders",
               json={"parent_id": default_id, "name": "T", "origin": "folder",
                     "location": str(tmp_path)},
               headers=boss)
    assert r.status_code == 403


def test_delete_folder_admin_only(tmp_path):
    """Deleting a non-root folder is admin-only; non-root deletion works for admin."""
    app, store, dev, boss = _iap_app_with_tokens(tmp_path)
    c = TestClient(app)
    rows = c.get("/folders", headers=boss).json()
    default_id = next(r["id"] for r in rows if r["name"] == "Default")
    # admin creates a child folder
    fid = c.post("/folders", json={"parent_id": default_id, "name": "Sub"}, headers=boss).json()["id"]
    # user-tier cannot delete
    assert c.delete(f"/folders/{fid}", headers=dev).status_code == 403
    # admin can delete
    assert c.delete(f"/folders/{fid}", headers=boss).status_code == 200
    # already gone -> 404
    assert c.delete(f"/folders/{fid}", headers=boss).status_code == 404


def test_iap_mode_requires_audience(tmp_path):
    with pytest.raises(ValueError):
        build_app(_settings(tmp_path, auth_mode="iap"))


def test_role_grant_applies_across_email_casing(tmp_path):
    app, store, dev, boss = _iap_app_with_tokens(tmp_path)
    store.set_role("Mole@x.com", "admin")           # admin grants with one casing
    tok = store.create_token("mole@x.com")            # user logs in with another
    c = TestClient(app)
    assert c.get("/me", headers={"Authorization": f"Bearer {tok}"}).json()["role"] == "admin"


def test_folder_access_hides_higher_tier_from_user(tmp_path):
    """User-tier callers should only see user-tier folders in /folders listing."""
    app, store, dev, boss = _iap_app_with_tokens(tmp_path)
    c = TestClient(app)
    # boss sees all three seeded roots (user, admin, owner tiers)
    boss_folders = {f["name"] for f in c.get("/folders", headers=boss).json()}
    assert {"Default", "Private", "Owner"} <= boss_folders
    # user-tier sees only the Default (user-tier) root
    dev_folders = {f["name"] for f in c.get("/folders", headers=dev).json()}
    assert "Default" in dev_folders
    assert "Private" not in dev_folders
    assert "Owner" not in dev_folders


def test_ingest_falls_back_to_direct_without_github(tmp_path):
    app = build_app(_settings(tmp_path))  # none mode, no github config
    c = TestClient(app)
    fid = next(r["id"] for r in c.get("/folders").json() if r["name"] == "Default")
    r = c.post("/ingest", files={"file": ("n.md", b"# N\n\nbody")},
               data={"folder_ids": [str(fid)]})
    assert r.status_code == 200
    assert r.json()["status"] == "added" and r.json()["versioned"] is False


def test_ingest_filename_sanitized(tmp_path):
    app = build_app(_settings(tmp_path))
    c = TestClient(app)
    fid = next(r["id"] for r in c.get("/folders").json() if r["name"] == "Default")
    r = c.post("/ingest",
               files={"file": ("rep ort?ref=evil#x.md", b"data")},
               data={"folder_ids": [str(fid)]})
    assert r.status_code == 200
    path = r.json()["paths"][0]
    assert all(ch not in path for ch in "?# ")  # no URL-significant chars


def test_ingest_docx_via_api_fallback(tmp_path):
    from tests.test_parsers import _minimal_docx
    app = build_app(_settings(tmp_path))
    c = TestClient(app)
    fid = next(r["id"] for r in c.get("/folders").json() if r["name"] == "Default")
    r = c.post("/ingest",
               files={"file": ("Report.docx", _minimal_docx("alpha bravo charlie"))},
               data={"folder_ids": [str(fid)]})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "added" and body["versioned"] is False


def test_resync_missing_folder_does_not_wipe(tmp_path):
    app, store = _iap_app_with_tokens(tmp_path)[:2]
    admin = {"Authorization": f"Bearer {store.create_token('boss@x.com')}"}
    c = TestClient(app)
    rows = c.get("/folders", headers=admin).json()
    default_id = next(r["id"] for r in rows if r["name"] == "Default")
    # create a folder-origin child pointing at a non-existent path
    missing = str(tmp_path / "gone")
    fid = store.create_folder(parent_id=default_id, name="Gone",
                              origin="folder", location=missing)
    # path isn't a directory -> 400
    assert c.post(f"/folders/{fid}/resync", headers=admin).status_code == 400


def test_resync_known_and_unknown(tmp_path):
    app, store = _iap_app_with_tokens(tmp_path)[:2]
    admin = {"Authorization": f"Bearer {store.create_token('boss@x.com')}"}
    dev = {"Authorization": f"Bearer {store.create_token('dev@x.com')}"}
    c = TestClient(app)
    rows = c.get("/folders", headers=admin).json()
    default_id = next(r["id"] for r in rows if r["name"] == "Default")
    # create a folder-origin child pointing at tmp_path (it is a valid dir)
    fid = store.create_folder(parent_id=default_id, name="Docs",
                              origin="folder", location=str(tmp_path))
    r = c.post(f"/folders/{fid}/resync", headers=admin)
    assert r.status_code == 200 and "report" in r.json()
    assert c.post("/folders/99999/resync", headers=admin).status_code == 404
    assert c.post(f"/folders/{fid}/resync", headers=dev).status_code == 403
