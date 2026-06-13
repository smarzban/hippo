import pytest

from hippo.chunking import Chunk
from hippo.db import connect
from hippo.embeddings import FakeEmbedder
from hippo.storage import Storage


@pytest.fixture
def store(tmp_path):
    con = connect(tmp_path / "t.db", embedding_dim=32)
    return Storage(con, FakeEmbedder(dim=32))


def _roots(store):
    """(user_root_id, admin_root_id, owner_root_id) from the seeded tree."""
    rows = store.con.execute(
        "SELECT min_role, id FROM folders WHERE parent_id IS NULL").fetchall()
    by_role = {r: i for r, i in rows}
    return by_role["user"], by_role["admin"], by_role["owner"]


def _add_doc(store, path, text, folder_id=None, title=None):
    if folder_id is None:
        folder_id, _, _ = _roots(store)
    return store.upsert_document(
        source_type="folder", path=path, title=title or path, content=text,
        content_hash=path + "h", chunks=[Chunk(position=0, heading_path=path, text=text)],
        embed_inputs=[text], folder_id=folder_id,
    )


def _doc(store, path="polly/integrations.md", text="Telegram webhook setup for polly.",
         folder_id=None):
    if folder_id is None:
        folder_id, _, _ = _roots(store)
    chunks = [Chunk(position=0, heading_path="Integrations > Telegram", text=text)]
    return store.upsert_document(
        source_type="folder", path=path, title="Polly Integrations",
        content=f"# Polly Integrations\n\n{text}", content_hash="hash1",
        chunks=chunks, embed_inputs=[c.text for c in chunks], folder_id=folder_id,
    )


def test_upsert_and_get(store):
    doc_id = _doc(store)
    doc = store.get_document(doc_id, role="owner")
    assert doc.title == "Polly Integrations"
    assert "Telegram webhook" in doc.content


def test_unchanged_detection(store):
    _doc(store)
    assert store.is_unchanged("polly/integrations.md", "hash1") is True
    assert store.is_unchanged("polly/integrations.md", "other") is False
    assert store.is_unchanged("missing.md", "hash1") is False


def test_update_replaces_chunks(store):
    user_root, _, _ = _roots(store)
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
        folder_id=user_root,
    )
    assert new_id == doc_id  # same document row, replaced contents
    rows = store.con.execute("SELECT count(*) FROM chunks WHERE document_id=?", (doc_id,)).fetchone()
    assert rows[0] == 1
    assert store.con.execute("SELECT count(*) FROM chunk_vec").fetchone()[0] == 1


def test_delete_document(store):
    doc_id = _doc(store)
    store.delete_document_by_path("polly/integrations.md")
    assert store.get_document(doc_id, role="owner") is None
    assert store.con.execute("SELECT count(*) FROM chunk_vec").fetchone()[0] == 0
    assert store.con.execute("SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH '\"telegram\"'").fetchone()[0] == 0


def test_list_documents(store):
    _doc(store)
    _doc(store, path="other/budget.md", text="Quarterly budget numbers.")
    docs = store.list_documents(role="owner")
    assert len(docs) == 2
    filtered = store.list_documents(query="budget", role="owner")
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
    hits = store.search_hybrid("telegram webhook", top_k=8, role="owner")
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
    assert len(store.list_documents(role="owner")) == 2


def test_reindex_rebuilds_vectors(store):
    _doc(store)
    n = store.reindex(embedding_dim=32)
    assert n == 1
    assert store.con.execute("SELECT count(*) FROM chunk_vec").fetchone()[0] == 1
    # still searchable after rebuild
    assert store.search_hybrid("telegram webhook", top_k=3, role="owner")


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


def test_ensure_user_defaults_user(store):
    assert store.ensure_user("a@x.com") == "user"
    assert store.ensure_user("a@x.com") == "user"  # idempotent
    assert store.list_users() == [("a@x.com", "user")]


def test_set_role_and_validation(store):
    store.set_role("a@x.com", "admin")
    assert store.ensure_user("a@x.com") == "admin"
    store.set_role("new@x.com", "owner")  # creates the row too
    assert ("new@x.com", "owner") in store.list_users()
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


def test_rank_filtering_by_folder_tier(store):
    user_root, admin_root, owner_root = _roots(store)
    _doc(store, path="u.md", text="user tier doc", folder_id=user_root)
    _doc(store, path="a.md", text="admin tier doc", folder_id=admin_root)
    _doc(store, path="o.md", text="owner tier doc", folder_id=owner_root)
    paths = lambda role: {d.path for d in store.list_documents(role=role)}
    assert paths("user") == {"u.md"}
    assert paths("admin") == {"u.md", "a.md"}
    assert paths("owner") == {"u.md", "a.md", "o.md"}


def test_get_document_respects_tier(store):
    _, admin_root, _ = _roots(store)
    doc_id = _doc(store, path="a.md", text="secret", folder_id=admin_root)
    assert store.get_document(doc_id, role="user") is None
    assert store.get_document(doc_id, role="admin") is not None


def test_create_nested_folder_inherits_tier_and_unique_siblings(store):
    _, admin_root, _ = _roots(store)
    child = store.create_folder(parent_id=admin_root, name="Retail")
    assert store.get_folder(child).min_role == "admin"
    assert store.folder_path(child) == "Private/Retail"
    with pytest.raises(ValueError, match="already exists"):
        store.create_folder(parent_id=admin_root, name="Retail")


def test_move_across_roots_rewrites_subtree_tier(store):
    user_root, _, owner_root = _roots(store)
    parent = store.create_folder(parent_id=user_root, name="Team")
    leaf = store.create_folder(parent_id=parent, name="Sub")
    store.move_folder(parent, owner_root)
    assert store.get_folder(parent).min_role == "owner"
    assert store.get_folder(leaf).min_role == "owner"  # whole subtree rewritten


def test_delete_folder_cascades_docs_and_refuses_roots(store):
    user_root, _, _ = _roots(store)
    sub = store.create_folder(parent_id=user_root, name="Temp")
    doc_id = _doc(store, path="Default/Temp/x.md", text="bye", folder_id=sub)
    assert store.delete_folder(sub) is True
    assert store.get_document(doc_id, role="owner") is None
    with pytest.raises(ValueError, match="root"):
        store.delete_folder(user_root)


@pytest.fixture
def rbac_store(store):
    user_root, admin_root, _ = _roots(store)
    _add_doc(store, "team/a.md", "public quarterly roadmap zebra", folder_id=user_root)
    _add_doc(store, "mgr/comp.md", "manager compensation zebra", folder_id=admin_root)
    _add_doc(store, "upload/x.md", "uploaded note zebra", folder_id=user_root)
    return store


def test_search_filters_admin_tier_folders(rbac_store):
    dev_paths = {h.path for h in rbac_store.search_hybrid("zebra", top_k=10, role="user")}
    assert "mgr/comp.md" not in dev_paths and "team/a.md" in dev_paths and "upload/x.md" in dev_paths
    mgr_paths = {h.path for h in rbac_store.search_hybrid("zebra", top_k=10, role="admin")}
    assert "mgr/comp.md" in mgr_paths


def test_list_get_and_grep_filter_by_role(rbac_store):
    assert {d.path for d in rbac_store.list_documents(role="user")} == {"team/a.md", "upload/x.md"}
    assert {d.path for d in rbac_store.list_documents(role="owner")} >= {"mgr/comp.md"}
    mgr_id = next(d.id for d in rbac_store.list_documents(role="owner") if d.path == "mgr/comp.md")
    assert rbac_store.get_document(mgr_id, role="user") is None
    assert rbac_store.get_document(mgr_id, role="admin") is not None
    assert all(h.path != "mgr/comp.md" for h in rbac_store.grep("compensation", role="user"))
    assert any(h.path == "mgr/comp.md" for h in rbac_store.grep("compensation", role="owner"))


def test_list_folders_role_filtered(store):
    user_root, admin_root, owner_root = _roots(store)
    assert {f.name for f in store.list_folders(role="user")} == {"Default"}
    assert {f.name for f in store.list_folders(role="admin")} == {"Default", "Private"}
    assert {f.name for f in store.list_folders(role="owner")} == {"Default", "Private", "Owner"}


def test_search_not_starved_by_higher_ranked_admin_chunks(store):
    """Codex review: candidate pools must be role-filtered, or higher-tier docs
    crowd user-visible docs out of the top_k*3 pool entirely."""
    user_root, admin_root, _ = _roots(store)
    _add_doc(store, "team/a.md", "zebra appears once here", folder_id=user_root)
    for i in range(30):  # dominate BM25 with high-tf admin-tier chunks
        _add_doc(store, f"mgr/{i}.md", "zebra " * 40, folder_id=admin_root)
    dev_hits = store.search_hybrid("zebra", top_k=5, role="user")
    assert any(h.path == "team/a.md" for h in dev_hits)
    assert all(not h.path.startswith("mgr/") for h in dev_hits)


def test_user_email_is_case_normalized(store):
    store.set_role("Foo@X.com", "admin")
    assert store.ensure_user("foo@x.com") == "admin"   # same user regardless of casing
    assert store.ensure_user("FOO@x.COM") == "admin"
    assert store.list_users() == [("foo@x.com", "admin")]  # one row, normalized


def test_token_email_normalized(store):
    t = store.create_token("Bar@X.com")
    assert store.resolve_token(t) == "bar@x.com"


def test_fts_candidates_are_role_filtered(store):
    user_root, admin_root, _ = _roots(store)
    _add_doc(store, "team/a.md", "zebra appears once here", folder_id=user_root)
    for i in range(30):
        _add_doc(store, f"mgr/{i}.md", "zebra " * 40, folder_id=admin_root)
    with store._lock:
        ids = store._search_fts("zebra", limit=10, role="user")
        mgr_ids = store._search_fts("zebra", limit=10, role="admin")
    team_doc_ids = {d.id for d in store.list_documents(role="user")}
    # user candidates contain ONLY user-visible chunks
    rows = {r[0] for r in store.con.execute(
        "SELECT document_id FROM chunks WHERE id IN (%s)" % ",".join(map(str, ids)))}
    assert rows <= team_doc_ids
    assert len(mgr_ids) == 10


def test_grep_rejects_overlong_pattern(store):
    import pytest
    from hippo import storage
    _add_doc(store, "a.md", "hello world")
    with pytest.raises(ValueError, match="too long"):
        store.grep("x" * (storage.GREP_MAX_PATTERN + 1), role="owner")


def test_grep_times_out_on_catastrophic_pattern(store, monkeypatch):
    import time, pytest
    from hippo import storage
    monkeypatch.setattr(storage, "GREP_TIMEOUT_S", 0.2)
    # a chunk that triggers catastrophic backtracking for (a|aa)+$
    _add_doc(store, "evil.md", "a" * 50 + "!")
    t0 = time.monotonic()
    with pytest.raises(ValueError, match="too long|too long to|took too long"):
        store.grep(r"(a|aa)+$", role="owner")
    assert time.monotonic() - t0 < 2.0  # bounded by the 0.2s timeout, not hanging


def test_grep_normal_pattern_still_matches(store):
    _add_doc(store, "doc.md", "the POLLY_WEBHOOK_URL config value")
    hits = store.grep(r"POLLY_\w+", role="owner")
    assert hits and hits[0].path == "doc.md"


def test_grep_whole_operation_time_budget(store, monkeypatch):
    import time
    from hippo import storage
    monkeypatch.setattr(storage, "GREP_TIMEOUT_S", 0.3)
    # many chunks that each backtrack a little; aggregate must stay bounded by the
    # whole-operation budget, not budget-per-chunk.
    for i in range(20):
        _add_doc(store, f"d{i}.md", "a" * 35 + "!")
    t0 = time.monotonic()
    import pytest
    with pytest.raises(ValueError, match="too long"):
        store.grep(r"(a|aa)+$", role="owner")
    assert time.monotonic() - t0 < 1.5  # ~0.3s budget, not 20×0.3


def test_backup_produces_readable_snapshot(store, tmp_path):
    from hippo.db import connect
    from hippo.embeddings import FakeEmbedder
    _add_doc(store, "a.md", "snapshot me please")
    dest = tmp_path / "snap.db"
    store.backup(dest)
    assert dest.exists()
    # reopen the snapshot independently and confirm the document is there
    con2 = connect(dest, embedding_dim=32)
    store2 = Storage(con2, FakeEmbedder(dim=32))
    assert any(d.path == "a.md" for d in store2.list_documents(role="owner"))


def test_token_revoke_and_list(store):
    t1 = store.create_token("a@x.com", name="laptop")
    store.create_token("a@x.com", name="ci")
    rows = store.list_tokens("A@x.com")  # casing-insensitive
    assert len(rows) == 2 and {r[1] for r in rows} == {"laptop", "ci"}
    assert all(r[3] is None for r in rows)  # last_used_at null before use
    assert store.resolve_token(t1) == "a@x.com"
    assert store.list_tokens("a@x.com")[0][3] is not None  # last_used stamped
    tid = rows[0][0]
    assert store.revoke_token(tid, "a@x.com") is True
    assert store.resolve_token(t1) is None  # revoked token no longer resolves
    assert len(store.list_tokens("a@x.com")) == 1
    assert store.revoke_token(tid, "a@x.com") is False  # already gone
    # cannot revoke another user's token
    t3 = store.create_token("b@x.com")
    other_id = store.list_tokens("b@x.com")[0][0]
    assert store.revoke_token(other_id, "a@x.com") is False
    assert store.resolve_token(t3) == "b@x.com"


LOCKOUT_MAX = 5


def test_set_password_creates_user_and_stores_hash(store):
    store.set_password("alice@x.com", "hashed-1", role="admin")
    creds = store.get_credentials("alice@x.com")
    assert creds is not None
    assert creds["password_hash"] == "hashed-1" and creds["role"] == "admin"
    # set_password on an existing user updates the hash, keeps the role
    store.set_password("alice@x.com", "hashed-2")
    assert store.get_credentials("alice@x.com")["password_hash"] == "hashed-2"
    assert store.get_credentials("alice@x.com")["role"] == "admin"


def test_get_credentials_unknown_email_is_none(store):
    assert store.get_credentials("nobody@x.com") is None


def test_get_user_by_id_roundtrip(store):
    store.set_password("bob@x.com", "h", role="owner")
    uid = store.get_credentials("bob@x.com")["user_id"]
    assert store.get_user_by_id(uid) == ("bob@x.com", "owner")
    assert store.get_user_by_id(999999) is None


def test_lockout_after_max_failures_then_reset(store):
    store.set_password("eve@x.com", "h")
    for _ in range(LOCKOUT_MAX):
        store.record_failed_login("eve@x.com")
    creds = store.get_credentials("eve@x.com")
    assert creds["failed_logins"] >= LOCKOUT_MAX
    assert creds["locked_until"] is not None       # lock set
    assert store.is_locked("eve@x.com") is True
    store.reset_login_state("eve@x.com")           # successful login clears it
    creds = store.get_credentials("eve@x.com")
    assert creds["failed_logins"] == 0 and creds["locked_until"] is None
    assert store.is_locked("eve@x.com") is False


def test_config_get_set_and_setup_flag(store):
    assert store.get_config("chat_model") is None
    store.set_config("chat_model", "openai:gpt-5.2")
    assert store.get_config("chat_model") == "openai:gpt-5.2"
    store.set_config("chat_model", "ollama:llama3")   # upsert
    assert store.get_config("chat_model") == "ollama:llama3"
    assert store.all_config()["chat_model"] == "ollama:llama3"
    assert store.is_setup_complete() is False
    store.mark_setup_complete()
    assert store.is_setup_complete() is True


def test_document_count(store):
    assert store.document_count() == 0
