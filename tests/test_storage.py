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


def test_name_defaults_empty_and_is_editable(store):
    store.ensure_user("a@x.com")
    prof = store.get_profile("a@x.com")
    assert prof == {"email": "a@x.com", "name": "", "role": "user"}
    store.set_name("a@x.com", "Alice X")
    assert store.get_profile("a@x.com")["name"] == "Alice X"
    # email is normalized on lookup
    assert store.get_profile("A@X.COM")["name"] == "Alice X"


def test_get_profile_unknown_user_is_none(store):
    assert store.get_profile("nobody@x.com") is None


def test_create_user_is_insert_only(store):
    assert store.create_user("new@x.com", role="admin", password_hash="h") is True
    assert store.get_profile("new@x.com")["role"] == "admin"
    # a second create is a no-op and reports False — never overwrites role/hash
    assert store.create_user("NEW@x.com", role="user", password_hash="h2") is False
    assert store.get_profile("new@x.com")["role"] == "admin"
    assert store.get_credentials("new@x.com")["password_hash"] == "h"
    with pytest.raises(ValueError):
        store.create_user("bad@x.com", role="superuser")


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
    from hippo.storage import search  # grep tuning constants live with grep (LOW-01 split)
    _add_doc(store, "a.md", "hello world")
    with pytest.raises(ValueError, match="too long"):
        store.grep("x" * (search.GREP_MAX_PATTERN + 1), role="owner")


def test_grep_times_out_on_catastrophic_pattern(store, monkeypatch):
    import time, pytest
    from hippo.storage import search  # grep tuning constants live with grep (LOW-01 split)
    monkeypatch.setattr(search, "GREP_TIMEOUT_S", 0.2)
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
    from hippo.storage import search  # grep tuning constants live with grep (LOW-01 split)
    monkeypatch.setattr(search, "GREP_TIMEOUT_S", 0.3)
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


def test_lockout_auto_expires_and_counter_decays(store):
    """MED-20: the lock auto-expires once locked_until passes (the core promised
    behavior, previously untested). LOW-15: an elapsed window also decays the failure
    counter, so the account isn't permanently soft-locked (re-locked on the next single
    failure)."""
    store.set_password("late@x.com", "h")
    for _ in range(LOCKOUT_MAX):
        store.record_failed_login("late@x.com")
    assert store.is_locked("late@x.com") is True
    # force the window into the PAST (failure counter still maxed)
    with store.con:
        store.con.execute(
            "UPDATE users SET locked_until=datetime('now','-1 minute') WHERE email=?",
            ("late@x.com",))
    assert store.is_locked("late@x.com") is False                          # auto-expired
    assert store.get_credentials("late@x.com")["failed_logins"] >= LOCKOUT_MAX  # not yet decayed
    store.clear_lock_if_expired("late@x.com")                              # LOW-15 decay
    creds = store.get_credentials("late@x.com")
    assert creds["failed_logins"] == 0 and creds["locked_until"] is None
    # a fresh single failure now starts the count at 1 — not an instant re-lock
    store.record_failed_login("late@x.com")
    assert store.is_locked("late@x.com") is False
    assert store.get_credentials("late@x.com")["failed_logins"] == 1


def test_retrieval_methods_require_role_keyword(store):
    """LOW-38: pin the access-control invariant — search_hybrid/grep/list_documents/
    get_document take `role` keyword-only with NO default, so a forgotten call site is a
    loud TypeError, never a silent low-privilege read. A refactor adding a default would
    fail here instead of leaking."""
    with pytest.raises(TypeError):
        store.search_hybrid("q")
    with pytest.raises(TypeError):
        store.grep("q")
    with pytest.raises(TypeError):
        store.list_documents()
    with pytest.raises(TypeError):
        store.get_document(1)


def test_config_get_set_and_setup_flag(store):
    assert store.get_config("chat_model") is None
    store.set_config("chat_model", "openai:gpt-5.2")
    assert store.get_config("chat_model") == "openai:gpt-5.2"
    store.set_config("chat_model", "ollama:llama3")   # upsert
    assert store.get_config("chat_model") == "ollama:llama3"
    assert store.is_setup_complete() is False
    store.mark_setup_complete()
    assert store.is_setup_complete() is True


def test_document_count(store):
    assert store.document_count() == 0


def test_claim_setup_is_atomic_once(store):
    # First claim wins; every subsequent claim loses (idempotent flag already set).
    assert store.is_setup_complete() is False
    assert store.claim_setup() is True
    assert store.is_setup_complete() is True
    assert store.claim_setup() is False
    assert store.claim_setup() is False


# ---------------------------------------------------------------------------
# PR-5 perf/concurrency regressions
# ---------------------------------------------------------------------------

def _user_root_id(store):
    return store.con.execute(
        "SELECT id FROM folders WHERE min_role='user' AND parent_id IS NULL").fetchone()[0]


def _admin_root_id(store):
    return store.con.execute(
        "SELECT id FROM folders WHERE min_role='admin' AND parent_id IS NULL").fetchone()[0]


def _add(store, path, text, folder_id, chash=None):
    store.upsert_document(source_type="folder", path=path, title=path, content=text,
        content_hash=chash or (path + "h"),
        chunks=[Chunk(position=0, heading_path=path, text=text)],
        embed_inputs=[text], folder_id=folder_id)


def test_list_document_meta_is_role_filtered_projection(store):
    """MED-17: list_document_meta returns id/path/title/summary only (no content) and
    is role-filtered like list_documents."""
    _add(store, "team/a.md", "alpha", _user_root_id(store))
    _add(store, "mgr/b.md", "beta", _admin_root_id(store))
    user_docs = store.list_document_meta(role="user")
    assert {d.path for d in user_docs} == {"team/a.md"}          # admin-tier hidden
    assert not hasattr(user_docs[0], "content")                  # projection: no content
    owner_docs = store.list_document_meta(role="owner")
    assert {d.path for d in owner_docs} == {"team/a.md", "mgr/b.md"}


def test_document_and_folder_count(store):
    """LOW-33: count-only helpers (no full-list materialization)."""
    assert store.document_count() == 0
    _add(store, "a.md", "x", _user_root_id(store))
    _add(store, "b.md", "y", _user_root_id(store))
    assert store.document_count() == 2
    assert store.folder_count() == 3                             # the three seeded roots


def test_documents_folder_id_index_exists(store):
    """LOW-34: the documents.folder_id index backs the per-folder doc_count subquery."""
    names = {r[0] for r in store.con.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    assert "idx_documents_folder_id" in names


def test_search_handles_sparse_low_tier_role(store):
    """MED-18: with most chunks in an admin-tier folder, a user-tier search returns
    only the visible (user-tier) hit in a single KNN over-fetch — no crash, no loop."""
    for i in range(20):
        _add(store, f"mgr/{i}.md", f"secret zebra {i}", _admin_root_id(store), chash=f"m{i}")
    _add(store, "team/pub.md", "public zebra note", _user_root_id(store))
    hits = store.search_hybrid("zebra", top_k=5, role="user")
    assert hits and all(h.path == "team/pub.md" for h in hits)   # only the user-tier doc


def test_grep_caps_chunks_scanned(store, monkeypatch, caplog):
    """MED-16: grep bounds how many chunks it materializes/scans, and logs (doesn't
    silently truncate) when the cap is reached."""
    import logging
    from hippo.storage import search  # grep tuning constants live with grep (LOW-01 split)
    monkeypatch.setattr(search, "GREP_MAX_CHUNKS", 2)
    for i in range(5):
        _add(store, f"d{i}.md", f"needle {i}", _user_root_id(store), chash=f"d{i}")
    with caplog.at_level(logging.WARNING, logger="hippo.storage"):
        store.grep("needle", role="owner")
    assert any("cap reached" in r.getMessage() for r in caplog.records)


def test_reindex_aborts_on_concurrent_chunk_change(store):
    """MED-06: a concurrent ingest during reindex's (unlocked) embedding window must
    abort the rebuild — otherwise the new chunk is stranded with no vector."""
    _add(store, "a.md", "alpha", _user_root_id(store))

    class _SneakyEmbedder:
        model, dim = "fake", 32
        def __init__(self, s): self._s, self._fired = s, False
        def embed(self, texts):
            if not self._fired:                                  # simulate a concurrent ingest
                self._fired = True
                did = self._s.con.execute("SELECT id FROM documents LIMIT 1").fetchone()[0]
                with self._s.con:
                    self._s.con.execute(
                        "INSERT INTO chunks(document_id, position, heading_path, text) "
                        "VALUES (?,1,'','sneaked in')", (did,))
            return FakeEmbedder(dim=32).embed(texts)

    store.embedder = _SneakyEmbedder(store)
    with pytest.raises(ValueError, match="changed during reindex"):
        store.reindex(32)
    # the original vector index is left intact (abort happened before the DROP)
    assert store.search_hybrid("alpha", top_k=3, role="owner")


def test_reindex_aborts_on_concurrent_text_change_same_ids(store):
    """Codex review (PR-5): comparing only chunk IDS misses a delete+reinsert with
    SQLite rowid reuse (same id set, different text) — which would embed stale text
    under reused ids. reindex compares (id, text), so a text change during the embed
    window also aborts."""
    _add(store, "a.md", "alpha", _user_root_id(store))

    class _TextMutatingEmbedder:
        model, dim = "fake", 32
        def __init__(self, s): self._s, self._fired = s, False
        def embed(self, texts):
            if not self._fired:                              # mutate text, keep id set
                self._fired = True
                with self._s.con:
                    self._s.con.execute(
                        "UPDATE chunks SET text='REPLACED' "
                        "WHERE id=(SELECT min(id) FROM chunks)")
            return FakeEmbedder(dim=32).embed(texts)

    store.embedder = _TextMutatingEmbedder(store)
    with pytest.raises(ValueError, match="changed during reindex"):
        store.reindex(32)
