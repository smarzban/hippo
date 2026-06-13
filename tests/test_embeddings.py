import math

import pytest

from hippo.chunking import Chunk
from hippo.db import connect
from hippo.embeddings import EMBED_BATCH, FakeEmbedder, OpenAIEmbedder
from hippo.storage import Storage


def test_fake_embedder_deterministic_unit_vectors():
    e = FakeEmbedder(dim=32)
    a1 = e.embed(["hello world"])[0]
    a2 = e.embed(["hello world"])[0]
    b = e.embed(["goodbye"])[0]
    assert a1 == a2
    assert a1 != b
    assert len(a1) == 32
    assert math.isclose(sum(x * x for x in a1), 1.0, rel_tol=1e-6)


def test_fake_embedder_similar_texts_share_tokens():
    e = FakeEmbedder(dim=32)
    base = e.embed(["telegram webhook setup"])[0]
    near = e.embed(["telegram webhook configuration"])[0]
    far = e.embed(["quarterly budget report"])[0]

    def dot(u, v):
        return sum(a * b for a, b in zip(u, v))

    assert dot(base, near) > dot(base, far)


# ---------------------------------------------------------------------------
# PR-3 regressions: MED-08 (dim stamp/validate), MED-10 (timeout/retries),
# LOW-16 (batching), LOW-32 (dimensions param). All offline.
# ---------------------------------------------------------------------------

def _user_root(store) -> int:
    return store.con.execute(
        "SELECT id FROM folders WHERE min_role='user' AND parent_id IS NULL").fetchone()[0]


def _add(store, path, chash="h"):
    store.upsert_document(
        source_type="upload", path=path, title=path, content="hi",
        content_hash=chash, chunks=[Chunk(position=0, heading_path="", text="hi")],
        embed_inputs=["hi"], folder_id=_user_root(store))


def test_embedding_dim_mismatch_on_reopen_raises_clear_error(tmp_path):
    """MED-08: a DB indexed at one dim, then reopened with a different configured dim
    (chunk_vec kept via IF NOT EXISTS), raises a clear reindex error on the next
    write — not a raw sqlite-vec "Dimension mismatch" OperationalError."""
    db = tmp_path / "t.db"
    store = Storage(connect(db, embedding_dim=32), FakeEmbedder(dim=32))
    _add(store, "a.md")                      # stamps embedding_model=fake, embedding_dim=32

    store2 = Storage(connect(db, embedding_dim=64), FakeEmbedder(dim=64))
    with pytest.raises(ValueError, match="dimension 32"):
        _add(store2, "b.md", chash="h2")


def test_reindex_restamps_dim(tmp_path):
    """MED-08: reindex to a new dim re-stamps embedding_dim so later writes validate
    against the NEW dim, not the old one."""
    db = tmp_path / "t.db"
    store = Storage(connect(db, embedding_dim=32), FakeEmbedder(dim=32))
    _add(store, "a.md")
    store.embedder = FakeEmbedder(dim=16)
    store.reindex(16)
    stamped = store.con.execute(
        "SELECT value FROM meta WHERE key='embedding_dim'").fetchone()[0]
    assert stamped == "16"
    _add(store, "b.md", chash="h2")          # validates fine against the new stamp


class _Resp:
    def __init__(self, n, dim):
        self.data = [type("D", (), {"embedding": [0.0] * dim})() for _ in range(n)]


def _recording_client(calls, dim):
    class _Emb:
        def create(self, *, model, input, **kw):
            calls.append({"model": model, "n": len(input), "kw": kw})
            return _Resp(len(input), dim)
    return type("C", (), {"embeddings": _Emb()})()


def test_openai_embedder_sets_timeout_and_retries(monkeypatch):
    """MED-10: explicit timeout + retry budget, not the SDK's 600s default."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    emb = OpenAIEmbedder(model="text-embedding-3-small", dim=256, timeout=7, max_retries=1)
    assert emb._client.timeout == 7
    assert emb._client.max_retries == 1


def test_openai_embedder_batches_and_passes_dimensions(monkeypatch):
    """LOW-16: inputs batched at EMBED_BATCH. LOW-32: text-embedding-3-* sends `dimensions`."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    emb = OpenAIEmbedder(model="text-embedding-3-small", dim=256)
    calls: list[dict] = []
    emb._client = _recording_client(calls, 256)
    out = emb.embed(["t"] * (EMBED_BATCH * 2 + 3))
    assert len(out) == EMBED_BATCH * 2 + 3
    assert [c["n"] for c in calls] == [EMBED_BATCH, EMBED_BATCH, 3]
    assert all(c["kw"].get("dimensions") == 256 for c in calls)


def test_openai_embedder_omits_dimensions_for_non_three_models(monkeypatch):
    """A native-dim model (e.g. nomic-embed-text) must NOT receive `dimensions`."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    emb = OpenAIEmbedder(model="nomic-embed-text", dim=768)
    calls: list[dict] = []
    emb._client = _recording_client(calls, 768)
    emb.embed(["one"])
    assert "dimensions" not in calls[0]["kw"]


def test_openai_embedder_empty_input_makes_no_call(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    emb = OpenAIEmbedder(model="text-embedding-3-small", dim=256)
    calls: list[dict] = []
    emb._client = _recording_client(calls, 256)
    assert emb.embed([]) == [] and calls == []
