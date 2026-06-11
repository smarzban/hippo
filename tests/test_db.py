from knowledgehub.db import connect


def test_schema_created(tmp_path):
    con = connect(tmp_path / "t.db", embedding_dim=32)
    tables = {
        r[0]
        for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','virtual table') OR type='table'"
        )
    }
    names = {r[0] for r in con.execute("SELECT name FROM sqlite_master")}
    for required in ("meta", "sources", "documents", "chunks", "chunks_fts", "chunk_vec"):
        assert required in names, f"missing {required}"
    assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


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
    con.execute(
        "INSERT INTO documents(source_type, path, title, content, content_hash) VALUES ('upload','a.md','A','hello world','h1')"
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
