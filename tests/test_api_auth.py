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


def test_none_mode_is_implicit_admin(tmp_path):
    app = build_app(_settings(tmp_path))
    c = TestClient(app)
    assert c.get("/health").status_code == 200
    me = c.get("/me").json()
    assert me["role"] == "admin" and me["auth_mode"] == "none"


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
        "email": "dev@x.com", "role": "developer", "auth_mode": "iap",
        "upload": {"team_repo": False, "managers_repo": False}}


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
    assert c.get("/me", headers={"Authorization": f"Bearer {t_dev}"}).json()["role"] == "developer"
    assert c.get("/me", headers={"Authorization": f"Bearer {t_boss}"}).json()["role"] == "admin"
    assert c.get("/me", headers={"Authorization": "Bearer hk_bogus"}).status_code == 401


def test_role_filtering_through_api(tmp_path):
    s = _settings(tmp_path, auth_mode="iap", iap_audience=AUD, admin_emails="boss@x.com")
    app = build_app(s, iap_verifier=IapVerifier(AUD, key_fetcher=lambda: {}))
    store = app.state.store
    mgr = store.register_source("folder", "/r/mgr", access="managers")
    store.upsert_document(source_type="folder", path="mgr/comp.md", title="comp",
                          content="secret", content_hash="h", source_id=mgr,
                          chunks=[Chunk(position=0, heading_path="comp", text="secret")],
                          embed_inputs=["secret"])
    c = TestClient(app)
    dev = {"Authorization": f"Bearer {store.create_token('dev@x.com')}"}
    boss = {"Authorization": f"Bearer {store.create_token('boss@x.com')}"}
    assert all(d["path"] != "mgr/comp.md" for d in c.get("/documents", headers=dev).json())
    assert any(d["path"] == "mgr/comp.md" for d in c.get("/documents", headers=boss).json())
    doc_id = next(d["id"] for d in c.get("/documents", headers=boss).json() if d["path"] == "mgr/comp.md")
    assert c.get(f"/documents/{doc_id}", headers=dev).status_code == 404
    assert c.get(f"/documents/{doc_id}", headers=boss).status_code == 200


def _fake_exchange():
    claims = {"iss": "https://accounts.google.com", "aud": "cid",
              "exp": int(time.time()) + 600, "email": "u@x.com", "email_verified": True}

    def exchange(code, settings):
        assert code == "authcode"
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


def test_sources_admin_only_and_allowlisted(tmp_path):
    docs = tmp_path / "roots" / "team"
    docs.mkdir(parents=True)
    (docs / "a.md").write_text("# A\n\nalpha")
    app, store, dev, boss = _iap_app_with_tokens(tmp_path, source_roots=str(tmp_path / "roots"))
    c = TestClient(app)
    body = {"location": str(docs), "access": "everyone"}
    assert c.post("/sources", json=body, headers=dev).status_code == 403   # not admin
    outside = {"location": str(tmp_path), "access": "everyone"}            # parent of root
    assert c.post("/sources", json=outside, headers=boss).status_code == 403
    r = c.post("/sources", json=body, headers=boss)
    assert r.status_code == 200 and r.json()["report"]["added"] == 1
    listed = c.get("/sources", headers=dev).json()
    assert listed[0]["access"] == "everyone"


def test_sources_registration_refused_without_roots_when_auth_on(tmp_path):
    app, _, _, boss = _iap_app_with_tokens(tmp_path)  # no source_roots configured
    c = TestClient(app)
    r = c.post("/sources", json={"location": str(tmp_path)}, headers=boss)
    assert r.status_code == 403


def test_delete_source_admin_only(tmp_path):
    docs = tmp_path / "roots" / "m"
    docs.mkdir(parents=True)
    (docs / "s.md").write_text("# S\n\nsecret")
    app, store, dev, boss = _iap_app_with_tokens(tmp_path, source_roots=str(tmp_path / "roots"))
    c = TestClient(app)
    c.post("/sources", json={"location": str(docs), "access": "managers"}, headers=boss)
    sid = c.get("/sources", headers=boss).json()[0]["id"]
    assert c.delete(f"/sources/{sid}", headers=dev).status_code == 403
    assert c.delete(f"/sources/{sid}", headers=boss).status_code == 200
    assert c.get("/sources", headers=boss).json() == []
    assert c.delete(f"/sources/{sid}", headers=boss).status_code == 404


class _FakeGH:
    def __init__(self):
        self.calls = []

    def put_file(self, path, content, message):
        self.calls.append((path, content, message))
        return "sha1"


def test_ingest_commits_to_repo_when_configured(tmp_path):
    fakes = {}

    def factory(repo):
        fakes[repo] = _FakeGH()
        return fakes[repo]

    s = _settings(tmp_path, auth_mode="iap", iap_audience=AUD, admin_emails="boss@x.com",
                  github_token="t", github_docs_repo="org/docs", github_managers_repo="org/mgr")
    app = build_app(s, iap_verifier=IapVerifier(AUD, key_fetcher=lambda: {}),
                    github_factory=factory)
    store = app.state.store
    dev = {"Authorization": f"Bearer {store.create_token('dev@x.com')}"}
    boss = {"Authorization": f"Bearer {store.create_token('boss@x.com')}"}
    c = TestClient(app)
    r = c.post("/ingest", files={"file": ("n.md", b"# N\n\nbody")}, headers=dev)
    assert r.status_code == 200 and r.json()["status"] == "committed"
    assert fakes["org/docs"].calls[0][0] == "uploads/8d360d6a-n.md"
    assert "dev@x.com" in fakes["org/docs"].calls[0][2]
    # managers repo: developers refused, managers/admins allowed
    r = c.post("/ingest", files={"file": ("m.md", b"# M")}, data={"repo": "managers"}, headers=dev)
    assert r.status_code == 403
    r = c.post("/ingest", files={"file": ("m.md", b"# M")}, data={"repo": "managers"}, headers=boss)
    assert r.status_code == 200 and fakes["org/mgr"].calls[0][0] == "uploads/29220162-m.md"


def test_ingest_falls_back_to_unversioned_without_github(tmp_path):
    app = build_app(_settings(tmp_path))  # none mode, no github config
    c = TestClient(app)
    r = c.post("/ingest", files={"file": ("n.md", b"# N\n\nbody")})
    assert r.status_code == 200
    assert r.json()["status"] == "added" and r.json()["versioned"] is False


def test_ingest_managers_repo_unconfigured_400(tmp_path):
    app, store, dev, boss = _iap_app_with_tokens(tmp_path)  # no github settings at all
    c = TestClient(app)
    r = c.post("/ingest", files={"file": ("m.md", b"# M")}, data={"repo": "managers"}, headers=boss)
    assert r.status_code == 400


def test_iap_mode_requires_audience(tmp_path):
    with pytest.raises(ValueError):
        build_app(_settings(tmp_path, auth_mode="iap"))


def test_ingest_commit_path_is_content_hash_qualified(tmp_path):
    import hashlib
    fakes = {}

    def factory(repo):
        fakes.setdefault(repo, _FakeGH())
        return fakes[repo]

    s = _settings(tmp_path, auth_mode="iap", iap_audience=AUD,
                  github_token="t", github_docs_repo="org/docs")
    app = build_app(s, iap_verifier=IapVerifier(AUD, key_fetcher=lambda: {}),
                    github_factory=factory)
    store = app.state.store
    dev = {"Authorization": f"Bearer {store.create_token('dev@x.com')}"}
    c = TestClient(app)
    r1 = c.post("/ingest", files={"file": ("notes.md", b"first doc")}, headers=dev)
    r2 = c.post("/ingest", files={"file": ("notes.md", b"different doc")}, headers=dev)
    p1, p2 = r1.json()["path"], r2.json()["path"]
    assert p1 != p2  # distinct contents must not overwrite each other
    h1 = hashlib.sha256(b"first doc").hexdigest()[:8]
    assert p1 == f"uploads/{h1}-notes.md"
    # identical re-upload converges on the same path (same content, same commit target)
    r3 = c.post("/ingest", files={"file": ("notes.md", b"first doc")}, headers=dev)
    assert r3.json()["path"] == p1


def test_role_grant_applies_across_email_casing(tmp_path):
    app, store, dev, boss = _iap_app_with_tokens(tmp_path)
    store.set_role("Mole@x.com", "manager")           # admin grants with one casing
    tok = store.create_token("mole@x.com")            # user logs in with another
    c = TestClient(app)
    assert c.get("/me", headers={"Authorization": f"Bearer {tok}"}).json()["role"] == "manager"


def test_sources_listing_hides_manager_sources_from_developers(tmp_path):
    app, store, dev, boss = _iap_app_with_tokens(tmp_path)
    store.register_source("folder", "/r/team")
    store.register_source("folder", "/r/mgr", access="managers")
    c = TestClient(app)
    dev_locs = {s["location"] for s in c.get("/sources", headers=dev).json()}
    assert dev_locs == {"/r/team"}
    boss_locs = {s["location"] for s in c.get("/sources", headers=boss).json()}
    assert boss_locs == {"/r/team", "/r/mgr"}


def test_ingest_filename_sanitized_for_github_path(tmp_path):
    fakes = {}

    def factory(repo):
        fakes.setdefault(repo, _FakeGH())
        return fakes[repo]

    s = _settings(tmp_path, auth_mode="iap", iap_audience=AUD,
                  github_token="t", github_docs_repo="org/docs")
    app = build_app(s, iap_verifier=IapVerifier(AUD, key_fetcher=lambda: {}),
                    github_factory=factory)
    store = app.state.store
    dev = {"Authorization": f"Bearer {store.create_token('dev@x.com')}"}
    c = TestClient(app)
    r = c.post("/ingest", files={"file": ("rep ort?ref=evil#x.md", b"data")}, headers=dev)
    assert r.status_code == 200
    committed_path = fakes["org/docs"].calls[0][0]
    assert committed_path.startswith("uploads/")
    assert all(ch not in committed_path for ch in "?# ")  # no URL-significant chars


def test_safe_filename_helper():
    from hippo.api import _safe_filename
    assert _safe_filename("../../etc/passwd") == "passwd"
    assert _safe_filename("a b?c#d.md") == "a_b_c_d.md"
    assert _safe_filename("???") == "upload"
    assert _safe_filename("notes.md") == "notes.md"


def test_ingest_rejects_oversized_upload(tmp_path):
    app = build_app(_settings(tmp_path, max_upload_bytes=20))
    c = TestClient(app)
    r = c.post("/ingest", files={"file": ("big.md", b"x" * 500)})
    assert r.status_code == 413
