import pytest

from hippo.chunking import Chunk
from hippo.db import connect
from hippo.embeddings import FakeEmbedder
from hippo.storage import Storage


@pytest.fixture
def store(tmp_path):
    con = connect(tmp_path / "t.db", embedding_dim=32)
    return Storage(con, FakeEmbedder(dim=32))


def _doc(store, path="polly/integrations.md", text="Telegram webhook setup for polly."):
    chunks = [Chunk(position=0, heading_path="Integrations > Telegram", text=text)]
    return store.upsert_document(
        source_type="folder",
        path=path,
        title="Polly Integrations",
        content=f"# Polly Integrations\n\n{text}",
        content_hash="hash1",
        chunks=chunks,
        embed_inputs=[c.text for c in chunks],
    )


def test_upsert_and_get(store):
    doc_id = _doc(store)
    doc = store.get_document(doc_id)
    assert doc.title == "Polly Integrations"
    assert "Telegram webhook" in doc.content


def test_unchanged_detection(store):
    _doc(store)
    assert store.is_unchanged("polly/integrations.md", "hash1") is True
    assert store.is_unchanged("polly/integrations.md", "other") is False
    assert store.is_unchanged("missing.md", "hash1") is False


def test_update_replaces_chunks(store):
    doc_id = _doc(store)
    chunks = [Chunk(position=0, heading_path="", text="Completely new content about slack.")]
    new_id = store.upsert_document(
        source_type="folder",
        path="polly/integrations.md",
        title="Polly Integrations",
        content="new",
        content_hash="hash2",
        chunks=chunks,
        embed_inputs=[c.text for c in chunks],
    )
    assert new_id == doc_id  # same document row, replaced contents
    rows = store.con.execute("SELECT count(*) FROM chunks WHERE document_id=?", (doc_id,)).fetchone()
    assert rows[0] == 1
    assert store.con.execute("SELECT count(*) FROM chunk_vec").fetchone()[0] == 1


def test_delete_document(store):
    doc_id = _doc(store)
    store.delete_document_by_path("polly/integrations.md")
    assert store.get_document(doc_id) is None
    assert store.con.execute("SELECT count(*) FROM chunk_vec").fetchone()[0] == 0
    assert store.con.execute("SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH '\"telegram\"'").fetchone()[0] == 0


def test_list_documents(store):
    _doc(store)
    _doc(store, path="other/budget.md", text="Quarterly budget numbers.")
    docs = store.list_documents()
    assert len(docs) == 2
    filtered = store.list_documents(query="budget")
    assert len(filtered) == 1 and filtered[0].path == "other/budget.md"
