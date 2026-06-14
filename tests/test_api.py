import pydantic_ai.models
import pytest
from fastapi.testclient import TestClient
from pydantic_ai.models.test import TestModel

from hippo.api import build_app
from hippo.config import Settings

pydantic_ai.models.ALLOW_MODEL_REQUESTS = False


def _settings(tmp_path, **over):
    base = dict(_env_file=None, db_path=tmp_path / "t.db", embedding_model="fake",
                embedding_dim=32, enrich_enabled=False)
    base.update(over)
    return Settings(**base)


@pytest.fixture
def client(tmp_path):
    app = build_app(_settings(tmp_path), model_override=TestModel(custom_output_text="hi from hub"))
    return TestClient(app)


def _default_folder_id(client):
    rows = client.get("/folders").json()
    return next(r["id"] for r in rows if r["name"] == "Default")


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_ingest_upload_and_list_documents(client):
    fid = _default_folder_id(client)
    r = client.post("/ingest",
                    files={"file": ("notes.md", b"# Notes\n\npolly telegram webhook", "text/markdown")},
                    data={"folder_ids": [str(fid)]})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "added" and len(body["paths"]) == 1

    docs = client.get("/documents").json()
    assert len(docs) == 1 and docs[0]["title"] == "Notes"

    doc = client.get(f"/documents/{docs[0]['id']}").json()
    assert "polly telegram webhook" in doc["content"]


def test_document_404(client):
    assert client.get("/documents/999").status_code == 404


def test_chat_streams_vercel_protocol(client):
    payload = {
        "trigger": "submit-message",
        "id": "chat1",
        "messages": [
            {"id": "m1", "role": "user", "parts": [{"type": "text", "text": "what is polly?"}]}
        ],
    }
    r = client.post("/chat", json=payload)
    assert r.status_code == 200
    assert "text/event-stream" in r.headers["content-type"]
    # TestModel streams each word as a separate delta; check all words present
    assert "hi" in r.text and "from" in r.text and "hub" in r.text


def test_ingest_with_enrichment_enabled(tmp_path, monkeypatch):
    """Enricher.run_sync must work from API routes (event loop already running)."""
    from hippo.enrich import Enricher

    monkeypatch.setattr(
        "hippo.api.Enricher", lambda model: Enricher(TestModel(custom_output_text="ctx line"))
    )
    settings = _settings(tmp_path, enrich_enabled=True)
    app = build_app(settings, model_override=TestModel(custom_output_text="hi"))
    client = TestClient(app)

    fid = next(r["id"] for r in client.get("/folders").json() if r["name"] == "Default")
    r = client.post("/ingest",
                    files={"file": ("notes.md", b"# Notes\n\npolly webhook", "text/markdown")},
                    data={"folder_ids": [str(fid)]})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "added"


def test_usage_limits_caps_tool_calls_not_just_requests():
    """M4: the cap must bound tool calls (the documented ~15 knob), with a
    generous request_limit backstop — not the other way around."""
    from hippo.agent import usage_limits

    s = Settings(_env_file=None, max_tool_calls=7)
    ul = usage_limits(s)
    assert ul.tool_calls_limit == 7
    assert ul.request_limit is not None and ul.request_limit > 7


def test_ingest_rejects_unsupported_type(client):
    fid = _default_folder_id(client)
    r = client.post("/ingest",
                    files={"file": ("data.bin", b"\x00\x01", "application/octet-stream")},
                    data={"folder_ids": [str(fid)]})
    assert r.status_code == 422
    assert "unsupported" in r.json()["detail"]


def test_ingest_into_two_folders_creates_two_docs(tmp_path):
    c = TestClient(build_app(_settings(tmp_path)))
    rows = c.get("/folders").json()
    default_id = next(r["id"] for r in rows if r["name"] == "Default")
    sub = c.post("/folders", json={"parent_id": default_id, "name": "Retail"}).json()["id"]
    r = c.post("/ingest", files={"file": ("note.md", b"# Note\n\nhi", "text/markdown")},
               data={"folder_ids": [str(default_id), str(sub)]})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "added" and len(body["paths"]) == 2
    paths = {d["path"] for d in c.get("/documents").json()}
    assert "Default/note.md" in paths and "Default/Retail/note.md" in paths


def test_ingest_into_higher_tier_folder_is_forbidden(tmp_path):
    import time
    import jwt
    from cryptography.hazmat.primitives.asymmetric import ec
    from hippo.auth import IapVerifier

    AUD = "/projects/1/global/backendServices/2"
    s = _settings(tmp_path, auth_mode="iap", iap_audience=AUD)
    key = ec.generate_private_key(ec.SECP256R1())
    app = build_app(s, iap_verifier=IapVerifier(AUD, key_fetcher=lambda: {"k1": key.public_key()}))
    c = TestClient(app)
    tok = jwt.encode({"aud": AUD, "iss": "https://cloud.google.com/iap",
                      "exp": int(time.time()) + 600, "email": "dev@x.com"},
                     key, algorithm="ES256", headers={"kid": "k1"})
    h = {"x-goog-iap-jwt-assertion": tok}
    # Get the Owner folder id directly from the store (user-tier callers can't see it in the listing)
    store = app.state.store
    owner_id = store.con.execute(
        "SELECT id FROM folders WHERE min_role='owner' AND parent_id IS NULL"
    ).fetchone()[0]
    # a user cannot upload into Owner tier folder (403) even if they know the id
    r = c.post("/ingest", files={"file": ("x.md", b"# X\n\nhi", "text/markdown")},
               data={"folder_ids": [str(owner_id)]}, headers=h)
    assert r.status_code == 403


def test_ingest_rejects_oversized_upload(tmp_path):
    app = build_app(_settings(tmp_path, max_upload_bytes=20))
    c = TestClient(app)
    fid = next(r["id"] for r in c.get("/folders").json() if r["name"] == "Default")
    r = c.post("/ingest", files={"file": ("big.md", b"x" * 500)},
               data={"folder_ids": [str(fid)]})
    assert r.status_code == 413


def test_serves_built_ui_when_configured(tmp_path):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>Hippo</title>")
    (dist / "assets" / "app.js").write_text("console.log('hi')")
    app = build_app(_settings(tmp_path, ui_dist=str(dist)))
    c = TestClient(app)
    # SPA root serves index.html
    r = c.get("/")
    assert r.status_code == 200 and "Hippo" in r.text
    # an unknown client-side route also gets index.html (SPA fallback)
    r = c.get("/some/client/route")
    assert r.status_code == 200 and "<title>Hippo</title>" in r.text
    # static asset served
    r = c.get("/assets/app.js")
    assert r.status_code == 200 and "console.log" in r.text
    # API routes still win and stay JSON
    assert c.get("/health").json() == {"status": "ok"}


def test_ingest_content_length_precheck(tmp_path):
    app = build_app(_settings(tmp_path, max_upload_bytes=20))
    c = TestClient(app)
    fid = next(r["id"] for r in c.get("/folders").json() if r["name"] == "Default")
    r = c.post("/ingest", files={"file": ("a.md", b"x" * 500)},
               data={"folder_ids": [str(fid)]})
    assert r.status_code == 413


def test_spa_does_not_mask_unknown_api_paths(tmp_path):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>Hippo</title>")
    app = build_app(_settings(tmp_path, ui_dist=str(dist)))
    c = TestClient(app)
    assert c.get("/documents/nope/extra").status_code == 404  # reserved -> 404, not HTML
    assert c.get("/some/client/route").status_code == 200      # real SPA route -> shell
    assert "Hippo" in c.get("/").text


def test_no_static_ui_when_unset(tmp_path):
    app = build_app(_settings(tmp_path))  # ui_dist default ""
    c = TestClient(app)
    # with no UI configured, an unknown path is a normal 404 (no catch-all)
    assert c.get("/some/client/route").status_code == 404
    assert c.get("/health").json() == {"status": "ok"}


def test_ingest_docx_via_api_fallback(tmp_path):
    from tests.test_parsers import _minimal_docx
    app = build_app(_settings(tmp_path))  # none mode
    c = TestClient(app)
    fid = next(r["id"] for r in c.get("/folders").json() if r["name"] == "Default")
    r = c.post("/ingest",
               files={"file": ("Report.docx", _minimal_docx("alpha bravo charlie"))},
               data={"folder_ids": [str(fid)]})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "added"


def test_mcp_requires_token(tmp_path):
    app = build_app(_settings(tmp_path))  # mcp_enabled defaults True; none-mode
    with TestClient(app) as c:
        # no Authorization header -> 401 before MCP processing
        assert c.post("/mcp", json={"jsonrpc": "2.0", "method": "ping", "id": 1}).status_code == 401
        assert c.post("/mcp", headers={"Authorization": "Bearer hk_bogus"},
                      json={"jsonrpc": "2.0", "method": "ping", "id": 1}).status_code == 401


def test_mcp_valid_token_full_handshake(tmp_path):
    """A valid token must pass the gate AND drive a working MCP handshake."""
    app = build_app(_settings(tmp_path))
    store = app.state.store
    token = store.create_token("dev@x.com")
    hdr = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    with TestClient(app) as c:
        init = c.post("/mcp/", headers=hdr, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "t", "version": "1"}}})
        assert init.status_code == 200
        sid = init.headers.get("mcp-session-id")
        h2 = dict(hdr)
        if sid:
            h2["mcp-session-id"] = sid
        listed = c.post("/mcp/", headers=h2,
                        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert listed.status_code == 200
        names = {t["name"] for t in listed.json()["result"]["tools"]}
        assert names == {"search", "read_document", "list_documents", "grep"}


def test_mcp_disabled_not_mounted(tmp_path):
    app = build_app(_settings(tmp_path, mcp_enabled=False))
    with TestClient(app) as c:
        assert c.post("/mcp", json={}).status_code == 404


def test_mcp_rejects_out_of_domain_token(tmp_path):
    s = _settings(tmp_path, allowed_domain="x.com")
    app = build_app(s)
    store = app.state.store
    good = store.create_token("dev@x.com")
    bad = store.create_token("contractor@gmail.com")  # token exists but wrong domain
    with TestClient(app) as c:
        # out-of-domain token is rejected
        assert c.post("/mcp/", headers={"Authorization": f"Bearer {bad}",
            "Accept": "application/json, text/event-stream", "Content-Type": "application/json"},
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                             "clientInfo": {"name": "t", "version": "1"}}}).status_code == 401
        # in-domain token still works
        init = c.post("/mcp/", headers={"Authorization": f"Bearer {good}",
            "Accept": "application/json, text/event-stream", "Content-Type": "application/json"},
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                             "clientInfo": {"name": "t", "version": "1"}}})
        assert init.status_code == 200


def test_mcp_revoked_token_rejected(tmp_path):
    app = build_app(_settings(tmp_path))
    store = app.state.store
    tok = store.create_token("dev@x.com")
    tid = store.list_tokens("dev@x.com")[0][0]
    assert store.revoke_token(tid, "dev@x.com") is True
    with TestClient(app) as c:
        assert c.post("/mcp/", headers={"Authorization": f"Bearer {tok}",
            "Accept": "application/json, text/event-stream", "Content-Type": "application/json"},
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                             "clientInfo": {"name": "t", "version": "1"}}}).status_code == 401


def test_safe_filename_helper():
    from hippo.api import _safe_filename
    assert _safe_filename("../../etc/passwd") == "passwd"
    assert _safe_filename("a b?c#d.md") == "a_b_c_d.md"
    assert _safe_filename("???") == "upload"
    assert _safe_filename("notes.md") == "notes.md"
