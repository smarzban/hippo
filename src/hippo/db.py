import sqlite3
from pathlib import Path

import sqlite_vec

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS folders (
    id INTEGER PRIMARY KEY,
    parent_id INTEGER REFERENCES folders(id) ON DELETE CASCADE,  -- NULL = a root
    name TEXT NOT NULL,
    min_role TEXT NOT NULL CHECK (min_role IN ('user','admin','owner')),
    origin TEXT NOT NULL DEFAULT 'manual' CHECK (origin IN ('manual','folder','repo')),
    location TEXT,                       -- fs path / owner/repo; NULL for manual
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(parent_id, name)
);
-- UNIQUE(parent_id, name) does NOT constrain roots (SQLite treats NULL parent_id
-- as distinct), so enforce unique root names explicitly.
CREATE UNIQUE INDEX IF NOT EXISTS folders_root_name
    ON folders(name) WHERE parent_id IS NULL;
-- A mounted filesystem path / repo maps to exactly one folder: enforce unique
-- non-null location so a second mount of the same path can't create an ambiguous
-- duplicate that sync would then populate into the wrong (older) folder row.
CREATE UNIQUE INDEX IF NOT EXISTS folders_location
    ON folders(location) WHERE location IS NOT NULL;

CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY,
    folder_id INTEGER NOT NULL REFERENCES folders(id) ON DELETE CASCADE,
    source_type TEXT NOT NULL,            -- folder | upload | repo
    path TEXT NOT NULL UNIQUE,            -- citation key (folder-qualified for uploads)
    title TEXT NOT NULL,
    content TEXT NOT NULL,                -- canonical markdown
    content_hash TEXT NOT NULL,
    summary TEXT,
    synced_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY,
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    heading_path TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text, content='chunks', content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', old.id, old.text);
    INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
END;

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL DEFAULT '',                        -- display name (self-editable)
    role TEXT NOT NULL DEFAULT 'user'
        CHECK (role IN ('user','admin','owner')),
    password_hash TEXT,                                  -- NULL for oidc/iap users
    failed_logins INTEGER NOT NULL DEFAULT 0,
    locked_until TEXT,                                   -- ISO ts; NULL = not locked
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS tokens (
    id INTEGER PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_used_at TEXT
);

CREATE TABLE IF NOT EXISTS config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# (name, min_role) for the three seeded roots; origin defaults to 'manual'.
_SEED_ROOTS = [("Default", "user"), ("Private", "admin"), ("Owner", "owner")]


def connect(db_path: Path | str, embedding_dim: int) -> sqlite3.Connection:
    """Open (creating if needed) the hub database with vec + FTS ready.

    SP1 introduced a fresh schema with NO migration: a pre-folders database
    (documents.source_id, no folders table) is rejected with a clear recreate
    message rather than half-migrated."""
    con = sqlite3.connect(db_path, check_same_thread=False)
    con.enable_load_extension(True)
    sqlite_vec.load(con)
    con.enable_load_extension(False)
    con.execute("PRAGMA journal_mode = WAL")
    con.execute("PRAGMA foreign_keys = ON")

    # Legacy-DB guard (spec §8): refuse the old sources/source_id schema loudly.
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "documents" in tables:
        doc_cols = {r[1] for r in con.execute("PRAGMA table_info(documents)")}
        # Reject on either signal: missing folder_id OR a leftover source_id column
        # (the pre-SP1 schema had documents.source_id and a sources table).
        if "folder_id" not in doc_cols or "source_id" in doc_cols:
            raise RuntimeError(
                "incompatible legacy schema (pre-SP1: documents.source_id / no folders "
                "table). SP1 uses a fresh schema with no data migration — recreate the "
                f"database: `rm {db_path}` and re-sync."
            )

    con.executescript(SCHEMA)
    # Additive migration: users.name was added after the SP1 schema. Unlike the
    # rejected legacy migration, this is a backward-compatible column add, so a
    # database created between SP1 and now upgrades in place (no data loss).
    user_cols = {r[1] for r in con.execute("PRAGMA table_info(users)")}
    if "name" not in user_cols:
        con.execute("ALTER TABLE users ADD COLUMN name TEXT NOT NULL DEFAULT ''")
    if con.execute("SELECT count(*) FROM folders").fetchone()[0] == 0:
        con.executemany(
            "INSERT INTO folders(parent_id, name, min_role, origin) "
            "VALUES (NULL, ?, ?, 'manual')",
            _SEED_ROOTS,
        )
    con.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS chunk_vec USING vec0(embedding float[{int(embedding_dim)}])"
    )
    con.commit()
    return con
