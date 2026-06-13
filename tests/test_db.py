# tests/test_db.py
import pytest

from hippo.db import connect


def test_vec_roundtrip(tmp_path):
    import sqlite_vec

    con = connect(tmp_path / "t.db", embedding_dim=4)
    con.execute(
        "INSERT INTO chunk_vec(rowid, embedding) VALUES (1, ?)",
        (sqlite_vec.serialize_float32([1.0, 0.0, 0.0, 0.0]),),
    )
    row = con.execute(
        "SELECT rowid, distance FROM chunk_vec WHERE embedding MATCH ? AND k = 1",
        (sqlite_vec.serialize_float32([1.0, 0.0, 0.0, 0.0]),),
    ).fetchone()
    assert row[0] == 1


def test_fts_sync_triggers(tmp_path):
    con = connect(tmp_path / "t.db", embedding_dim=4)
    fid = con.execute("SELECT id FROM folders WHERE parent_id IS NULL").fetchone()[0]
    con.execute(
        "INSERT INTO documents(folder_id, source_type, path, title, content, content_hash) "
        "VALUES (?,'upload','a.md','A','hello world','h1')",
        (fid,),
    )
    doc_id = con.execute("SELECT id FROM documents").fetchone()[0]
    con.execute(
        "INSERT INTO chunks(document_id, position, heading_path, text) VALUES (?,0,'','hello world')",
        (doc_id,),
    )
    hit = con.execute(
        "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH '\"hello\"'"
    ).fetchone()
    assert hit is not None
    con.execute("DELETE FROM chunks WHERE document_id = ?", (doc_id,))
    assert con.execute("SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH '\"hello\"'").fetchone()[0] == 0


def test_fresh_db_seeds_three_roots(tmp_path):
    con = connect(tmp_path / "t.db", embedding_dim=32)
    roots = con.execute(
        "SELECT name, min_role, origin FROM folders WHERE parent_id IS NULL ORDER BY id"
    ).fetchall()
    assert roots == [
        ("Default", "user", "manual"),
        ("Private", "admin", "manual"),
        ("Owner", "owner", "manual"),
    ]


def test_seed_is_idempotent_across_reopen(tmp_path):
    p = tmp_path / "t.db"
    connect(p, embedding_dim=32).close()
    con = connect(p, embedding_dim=32)  # second open must not re-seed
    assert con.execute("SELECT count(*) FROM folders").fetchone()[0] == 3


def test_documents_has_folder_id_not_source_id(tmp_path):
    con = connect(tmp_path / "t.db", embedding_dim=32)
    cols = {r[1] for r in con.execute("PRAGMA table_info(documents)")}
    assert "folder_id" in cols and "source_id" not in cols


def test_users_surrogate_key_and_tokens_user_id(tmp_path):
    con = connect(tmp_path / "t.db", embedding_dim=32)
    ucols = {r[1] for r in con.execute("PRAGMA table_info(users)")}
    assert "id" in ucols and "email" in ucols and "role" in ucols
    tcols = {r[1] for r in con.execute("PRAGMA table_info(tokens)")}
    assert "user_id" in tcols and "email" not in tcols


def test_legacy_schema_raises_clear_error(tmp_path):
    import sqlite3

    p = tmp_path / "legacy.db"
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE documents (id INTEGER PRIMARY KEY, source_id INTEGER, path TEXT)")
    con.commit()
    con.close()
    with pytest.raises(RuntimeError, match="recreate the database"):
        connect(p, embedding_dim=32)


def test_legacy_schema_with_source_id_present_still_rejected(tmp_path):
    """Defense-in-depth (PR #11 review, LOW): a hybrid table that grew a folder_id
    but kept the legacy source_id column is still rejected — the source_id signal
    also trips the guard."""
    import sqlite3

    p = tmp_path / "hybrid.db"
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE documents (id INTEGER PRIMARY KEY, folder_id INTEGER, "
                "source_id INTEGER, path TEXT)")
    con.commit()
    con.close()
    with pytest.raises(RuntimeError, match="recreate the database"):
        connect(p, embedding_dim=32)
