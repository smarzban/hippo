import pytest

from hippo.db import connect
from hippo.embeddings import FakeEmbedder
from hippo.ingest import Ingestor, sync_folder
from hippo.parsers import parse_file
from hippo.storage import Storage
from tests.test_parsers import _minimal_docx


@pytest.fixture
def store(tmp_path_factory):
    db_dir = tmp_path_factory.mktemp("db")
    con = connect(db_dir / "t.db", embedding_dim=32)
    return Storage(con, FakeEmbedder(dim=32))


def _default_root(store):
    """Return the id of the seeded Default (user-tier) root folder."""
    row = store.con.execute(
        "SELECT id FROM folders WHERE min_role='user' AND parent_id IS NULL"
    ).fetchone()
    return row[0]


def test_parse_markdown_title(tmp_path):
    f = tmp_path / "a.md"
    f.write_text("# Real Title\n\nbody")
    title, md = parse_file(f)
    assert title == "Real Title" and "body" in md


def test_parse_txt_and_html(tmp_path):
    t = tmp_path / "notes.txt"
    t.write_text("plain text notes")
    title, md = parse_file(t)
    assert title == "notes" and md == "plain text notes"

    h = tmp_path / "doc.html"
    h.write_text("<h1>Exported Doc</h1><p>from google docs</p>")
    title, md = parse_file(h)
    assert title == "Exported Doc" and "from google docs" in md


def test_ingest_add_update_skip(store, tmp_path):
    f = tmp_path / "a.md"
    f.write_text("# A\n\nfirst version")
    ing = Ingestor(store, max_chars=3000, overlap_chars=0)
    folder_id = _default_root(store)

    assert ing.ingest_file(f, source_type="folder", folder_id=folder_id).status == "added"
    assert ing.ingest_file(f, source_type="folder", folder_id=folder_id).status == "skipped"
    f.write_text("# A\n\nsecond version")
    assert ing.ingest_file(f, source_type="folder", folder_id=folder_id).status == "updated"
    hits = store.search_hybrid("second version", top_k=3, role="owner")
    assert hits and "second" in hits[0].text


def test_ingest_failure_isolated(store, tmp_path):
    bad = tmp_path / "bad.docx"
    bad.write_bytes(b"\x00\x01binary")
    good = tmp_path / "good.md"
    good.write_text("# Good\n\ncontent here")
    folder_id = _default_root(store)
    report = sync_folder(tmp_path, store, parent_id=folder_id, max_chars=3000, overlap_chars=0)
    assert report.added == 1 and report.failed == 1


def test_sync_removes_deleted_files(store, tmp_path):
    a = tmp_path / "a.md"
    a.write_text("# A\n\nalpha doc")
    (tmp_path / "b.md").write_text("# B\n\nbeta doc")
    folder_id = _default_root(store)
    sync_folder(tmp_path, store, parent_id=folder_id, max_chars=3000, overlap_chars=0)
    assert len(store.list_documents(role="owner")) == 2
    a.unlink()
    report = sync_folder(tmp_path, store, parent_id=folder_id, max_chars=3000, overlap_chars=0)
    assert report.removed == 1
    assert len(store.list_documents(role="owner")) == 1


def test_db_and_dotfiles_ignored_by_sync(store, tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.md").write_text("# A\n\nalpha")
    (docs / "hub.db").write_bytes(b"\x00")
    (docs / "hub.db-wal").write_bytes(b"\x00")
    (docs / ".hidden.md").write_text("# Hidden\n\nnope")
    folder_id = _default_root(store)
    report = sync_folder(docs, store, parent_id=folder_id, max_chars=3000, overlap_chars=0)
    assert report.added == 1 and report.failed == 0


def test_sync_skips_symlink_escaping_mount(store, tmp_path):
    """Security (PR-2 review, MED): a symlink planted inside an allowlisted mount
    whose target is OUTSIDE the mount must not be ingested — otherwise it
    exfiltrates arbitrary host files (allowed/x.md -> /etc/secret) through retrieval,
    bypassing the HIPPO_SOURCE_ROOTS containment that only checks the mount root."""
    import os
    secret = tmp_path / "secret.md"
    secret.write_text("# Secret\n\nzztopsecret exfil bait")
    docs = tmp_path / "mount"
    docs.mkdir()
    (docs / "ok.md").write_text("# OK\n\npublic content")
    os.symlink(secret, docs / "leak.md")   # symlink inside mount -> outside file
    folder_id = _default_root(store)
    report = sync_folder(docs, store, parent_id=folder_id, max_chars=3000, overlap_chars=0)
    assert report.added == 1                # only the real in-mount file
    paths = {d.path for d in store.list_documents(role="owner")}
    assert not any("leak" in p or "secret" in p for p in paths)
    # and the escaping file's actual content was never ingested (grep is exact text,
    # unlike FakeEmbedder hybrid search which matches deterministically)
    assert store.grep("zztopsecret", role="owner") == []


def test_upload_path_collision_distinct_contents_coexist(store):
    """L4: two different uploads sharing a filename must not silently overwrite."""
    ing = Ingestor(store, max_chars=3000, overlap_chars=0)
    folder_id = _default_root(store)
    r1 = ing.ingest_text("notes.md", "# Notes\n\nfirst project notes",
                         folder_id=folder_id, path_prefix="Default")
    r2 = ing.ingest_text("notes.md", "# Notes\n\ncompletely different content",
                         folder_id=folder_id, path_prefix="Default/uploads")
    assert r1.status == "added" and r2.status == "added"
    assert len(store.list_documents(role="owner")) == 2  # both kept, not overwritten


def test_empty_file_skipped_not_ghosted(store, tmp_path):
    docs = tmp_path / "docs2"
    docs.mkdir()
    (docs / "empty.md").write_text("")
    folder_id = _default_root(store)
    report = sync_folder(docs, store, parent_id=folder_id, max_chars=3000, overlap_chars=0)
    assert report.skipped == 1 and report.added == 0
    assert store.list_documents(role="owner") == []


def test_oversized_document_skipped_not_failed(store):
    ing = Ingestor(store, max_chars=3000, overlap_chars=0, max_doc_chars=50)
    folder_id = _default_root(store)
    r = ing.ingest_text("big.md", "# Big\n\n" + "x" * 200,
                        folder_id=folder_id, path_prefix="Default")
    assert r.status == "skipped" and "max_doc_chars" in (r.error or "")
    assert store.list_documents(role="owner") == []  # never indexed


def test_under_limit_document_still_added(store):
    ing = Ingestor(store, max_chars=3000, overlap_chars=0, max_doc_chars=10_000)
    folder_id = _default_root(store)
    assert ing.ingest_text("ok.md", "# OK\n\nsmall body",
                           folder_id=folder_id, path_prefix="Default").status == "added"


def test_ingest_emits_info_log(store, caplog):
    import logging
    ing = Ingestor(store, max_chars=3000, overlap_chars=0)
    folder_id = _default_root(store)
    with caplog.at_level(logging.INFO, logger="hippo.ingest"):
        ing.ingest_text("logged.md", "# L\n\nsome body text",
                        folder_id=folder_id, path_prefix="Default")
    msgs = [r.getMessage() for r in caplog.records]
    assert any("logged.md" in m and "added" in m for m in msgs)


def test_ingest_bytes_docx_indexes_and_searches(store):
    ing = Ingestor(store, max_chars=3000, overlap_chars=0)
    folder_id = _default_root(store)
    r = ing.ingest_bytes("Plan.docx", _minimal_docx("pubsub setup instructions here"),
                         suffix=".docx", folder_id=folder_id, path_prefix="Default")
    assert r.status == "added" and r.chunks >= 1
    hits = store.search_hybrid("pubsub setup", top_k=3, role="owner")
    assert hits and "pubsub" in hits[0].text.lower()


def test_ingest_text_still_works_via_delegate(store):
    ing = Ingestor(store, max_chars=3000, overlap_chars=0)
    folder_id = _default_root(store)
    assert ing.ingest_text("n.md", "# N\n\nhello body",
                           folder_id=folder_id, path_prefix="Default").status == "added"


def test_sync_folder_mounts_under_parent_and_tags_docs(tmp_path, store):
    # store fixture: connect() + Storage(FakeEmbedder); db lives OUTSIDE the synced dir
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.md").write_text("# A\n\nHello world.")
    user_root = store.con.execute(
        "SELECT id FROM folders WHERE min_role='user' AND parent_id IS NULL").fetchone()[0]
    report = sync_folder(docs, store, parent_id=user_root, max_chars=3000, overlap_chars=200)
    assert report.added == 1
    # the mount is a synced child of Default, upload-locked
    fid = store.folder_by_location(str(docs))
    f = store.get_folder(fid)
    assert f.origin == "folder" and f.min_role == "user" and f.parent_id == user_root
    assert store.list_documents(role="user")[0].path == str(docs / "a.md")


def test_ingest_bytes_into_folder_qualified_path(tmp_path, store):
    user_root = store.con.execute(
        "SELECT id FROM folders WHERE min_role='user' AND parent_id IS NULL").fetchone()[0]
    ing = Ingestor(store, max_chars=3000, overlap_chars=200)
    res = ing.ingest_bytes("notes.md", b"# Notes\n\nbody", folder_id=user_root,
                           path_prefix="Default")
    assert res.status == "added"
    assert res.path == "Default/notes.md"  # folder-qualified, unique across folders
