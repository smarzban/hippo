import pydantic_ai.models
import pytest
from fastapi.testclient import TestClient
from pydantic_ai.models.test import TestModel

from hippo.api import build_app
from hippo.config import Settings

pydantic_ai.models.ALLOW_MODEL_REQUESTS = False


@pytest.fixture
def client(tmp_path):
    settings = Settings(
        _env_file=None,
        db_path=tmp_path / "t.db",
        embedding_model="fake",
        embedding_dim=32,
        enrich_enabled=False,
    )
    app = build_app(settings, model_override=TestModel(custom_output_text="hi from hub"))
    return TestClient(app)


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_ingest_upload_and_list_documents(client):
    r = client.post("/ingest", files={"file": ("notes.md", b"# Notes\n\npolly telegram webhook", "text/markdown")})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "added" and body["chunks"] >= 1

    docs = client.get("/documents").json()
    assert len(docs) == 1 and docs[0]["title"] == "Notes"

    doc = client.get(f"/documents/{docs[0]['id']}").json()
    assert "polly telegram webhook" in doc["content"]


def test_document_404(client):
    assert client.get("/documents/999").status_code == 404


def test_sources_register_and_list(client, tmp_path):
    folder = tmp_path / "docs"
    folder.mkdir()
    (folder / "a.md").write_text("# A\n\nalpha")
    r = client.post("/sources", json={"kind": "folder", "location": str(folder)})
    assert r.status_code == 200
    assert r.json()["report"]["added"] == 1
    assert len(client.get("/sources").json()) == 1


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
    settings = Settings(
        _env_file=None,
        db_path=tmp_path / "t.db",
        embedding_model="fake",
        embedding_dim=32,
        enrich_enabled=True,
    )
    app = build_app(settings, model_override=TestModel(custom_output_text="hi"))
    client = TestClient(app)

    r = client.post("/ingest", files={"file": ("notes.md", b"# Notes\n\npolly webhook", "text/markdown")})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "added"

    folder = tmp_path / "docs"
    folder.mkdir()
    (folder / "a.md").write_text("# A\n\nalpha")
    r = client.post("/sources", json={"location": str(folder)})
    assert r.json()["report"] == {"added": 1, "updated": 0, "skipped": 0, "removed": 0, "failed": 0}


def test_usage_limits_caps_tool_calls_not_just_requests():
    """M4: the cap must bound tool calls (the documented ~15 knob), with a
    generous request_limit backstop — not the other way around."""
    from hippo.api import _usage_limits

    s = Settings(_env_file=None, max_tool_calls=7)
    ul = _usage_limits(s)
    assert ul.tool_calls_limit == 7
    assert ul.request_limit is not None and ul.request_limit > 7


def test_ingest_rejects_unsupported_type(client):
    r = client.post("/ingest", files={"file": ("data.bin", b"\x00\x01", "application/octet-stream")})
    assert r.status_code == 422
    assert "unsupported" in r.json()["detail"]
