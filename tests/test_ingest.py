import pytest

from knowledgehub.db import connect
from knowledgehub.embeddings import FakeEmbedder
from knowledgehub.ingest import Ingestor, sync_folder
from knowledgehub.parsers import parse_file
from knowledgehub.storage import Storage


@pytest.fixture
def store(tmp_path_factory):
    db_dir = tmp_path_factory.mktemp("db")
    con = connect(db_dir / "t.db", embedding_dim=32)
    return Storage(con, FakeEmbedder(dim=32))


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

    assert ing.ingest_file(f, source_type="folder").status == "added"
    assert ing.ingest_file(f, source_type="folder").status == "skipped"
    f.write_text("# A\n\nsecond version")
    assert ing.ingest_file(f, source_type="folder").status == "updated"
    hits = store.search_hybrid("second version", top_k=3)
    assert hits and "second" in hits[0].text


def test_ingest_failure_isolated(store, tmp_path):
    bad = tmp_path / "bad.docx"
    bad.write_bytes(b"\x00\x01binary")
    good = tmp_path / "good.md"
    good.write_text("# Good\n\ncontent here")
    report = sync_folder(tmp_path, store, max_chars=3000, overlap_chars=0)
    assert report.added == 1 and report.failed == 1


def test_sync_removes_deleted_files(store, tmp_path):
    a = tmp_path / "a.md"
    a.write_text("# A\n\nalpha doc")
    (tmp_path / "b.md").write_text("# B\n\nbeta doc")
    sync_folder(tmp_path, store, max_chars=3000, overlap_chars=0)
    assert len(store.list_documents()) == 2
    a.unlink()
    report = sync_folder(tmp_path, store, max_chars=3000, overlap_chars=0)
    assert report.removed == 1
    assert len(store.list_documents()) == 1


def test_db_and_dotfiles_ignored_by_sync(store, tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.md").write_text("# A\n\nalpha")
    (docs / "hub.db").write_bytes(b"\x00")
    (docs / "hub.db-wal").write_bytes(b"\x00")
    (docs / ".hidden.md").write_text("# Hidden\n\nnope")
    report = sync_folder(docs, store, max_chars=3000, overlap_chars=0)
    assert report.added == 1 and report.failed == 0


def test_empty_file_skipped_not_ghosted(store, tmp_path):
    docs = tmp_path / "docs2"
    docs.mkdir()
    (docs / "empty.md").write_text("")
    report = sync_folder(docs, store, max_chars=3000, overlap_chars=0)
    assert report.skipped == 1 and report.added == 0
    assert store.list_documents() == []
