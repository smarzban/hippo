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


def test_orphan_vec_rowid_is_skipped_not_crash(store):
    """L3: a chunk_vec rowid with no matching chunk (orphan) must be skipped by
    search, not crash with SearchHit(*None)."""
    _doc(store)
    # forge an orphan vector row (rowid that no chunk references)
    import sqlite_vec
    store.con.execute(
        "INSERT INTO chunk_vec(rowid, embedding) VALUES (?,?)",
        (999999, sqlite_vec.serialize_float32([0.1] * 32)),
    )
    store.con.commit()
    hits = store.search_hybrid("telegram webhook", top_k=8)
    assert all(h is not None for h in hits)
    assert 999999 not in [h.chunk_id for h in hits]


def test_embedding_model_mismatch_refused(store):
    """M3: the DB records its embedding model; ingesting with a different model
    (same dim) must be refused rather than silently mixing embedding spaces."""
    _doc(store)  # stamps model "fake"
    other = FakeEmbedder(dim=32)
    other.model = "some-other-model"
    mixed = Storage(store.con, other)
    with pytest.raises(ValueError, match="embedding model"):
        _doc(mixed, path="new.md")


def test_embedding_model_stamp_persists_and_matches(store):
    _doc(store)
    row = store.con.execute("SELECT value FROM meta WHERE key='embedding_model'").fetchone()
    assert row[0] == "fake"
    # same model is fine
    _doc(store, path="second.md", text="another doc about slack")
    assert len(store.list_documents()) == 2


def test_reindex_rebuilds_vectors(store):
    _doc(store)
    n = store.reindex(embedding_dim=32)
    assert n == 1
    assert store.con.execute("SELECT count(*) FROM chunk_vec").fetchone()[0] == 1
    # still searchable after rebuild
    assert store.search_hybrid("telegram webhook", top_k=3)


def test_reindex_failure_preserves_existing_vectors(store):
    """M2: a mid-run embedding failure must NOT leave the index wiped."""
    _doc(store)
    _doc(store, path="b.md", text="another doc about slack channels")
    before = store.con.execute("SELECT count(*) FROM chunk_vec").fetchone()[0]
    assert before == 2

    class FailingEmbedder:
        model = "fake"
        dim = 32

        def embed(self, texts):
            raise RuntimeError("simulated API failure")

    broken = Storage(store.con, FailingEmbedder())
    with pytest.raises(RuntimeError):
        broken.reindex(embedding_dim=32)
    after = store.con.execute("SELECT count(*) FROM chunk_vec").fetchone()[0]
    assert after == before  # vectors intact, nothing destroyed


def test_reindex_dim_mismatch_refused_before_destroying(store):
    _doc(store)

    class WrongDim:
        model = "fake"
        dim = 32

        def embed(self, texts):
            return [[0.0] * 8 for _ in texts]  # wrong dimension

    broken = Storage(store.con, WrongDim())
    before = store.con.execute("SELECT count(*) FROM chunk_vec").fetchone()[0]
    with pytest.raises(ValueError, match="dimension"):
        broken.reindex(embedding_dim=32)
    assert store.con.execute("SELECT count(*) FROM chunk_vec").fetchone()[0] == before


def test_ensure_user_defaults_developer(store):
    assert store.ensure_user("a@x.com") == "developer"
    assert store.ensure_user("a@x.com") == "developer"  # idempotent
    assert store.list_users() == [("a@x.com", "developer")]


def test_set_role_and_validation(store):
    store.set_role("a@x.com", "manager")
    assert store.ensure_user("a@x.com") == "manager"
    store.set_role("new@x.com", "admin")  # creates the row too
    assert ("new@x.com", "admin") in store.list_users()
    with pytest.raises(ValueError):
        store.set_role("a@x.com", "superuser")


def test_token_roundtrip_and_hashing(store):
    t = store.create_token("a@x.com", name="laptop")
    assert t.startswith("hk_") and len(t) > 30
    assert store.resolve_token(t) == "a@x.com"
    assert store.resolve_token("hk_wrong") is None
    # only the hash is stored — the raw token must not appear in the db
    raw = store.con.execute("SELECT token_hash FROM tokens").fetchone()[0]
    assert t not in raw and t[3:] not in raw
