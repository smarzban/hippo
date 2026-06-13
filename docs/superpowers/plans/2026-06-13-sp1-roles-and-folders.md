# SP1 — Roles & Content-Folder Model Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace Hippo's flat source-level access model with a role-tiered folder tree (`user` < `admin` < `owner`) that admins manage from the UI, gating both read (retrieval) and write (upload) by `caller_rank >= folder_tier_rank`.

**Architecture:** A new pure `roles.py` module owns the rank comparison (single source of truth). `db.py` gets a fresh schema — a `folders` adjacency-tree (replacing `sources`), `documents.folder_id` (replacing the nullable `source_id`), and a surrogate-keyed `users(id PK, email UNIQUE)` with `tokens.user_id`. All SQL stays in `storage.py`, which filters retrieval by translating the caller's rank into the set of readable `min_role` values. The API exposes `/folders` CRUD (admin+) and a multi-destination `/ingest`; the React UI gets a folder tree and a role-scoped upload modal. **No data migration** — fresh schema; an old DB raises a clear recreate error (spec §8).

**Tech Stack:** Python 3 / FastAPI / pydantic-ai / sqlite3 + sqlite-vec + FTS5; React 19 / Vite / Vitest. Tests are zero-network (`FakeEmbedder`, pydantic-ai `TestModel`).

**Spec:** `docs/superpowers/specs/2026-06-13-roles-and-collections-design.md`

## Role rename mapping (used throughout)

The whole codebase moves from `developer/manager/admin` to `user/admin/owner`. Because the OLD top role was `admin` and the NEW top role is `owner`, the rank-preserving mapping is:

| old | new | rank |
|-----|-----|------|
| `developer` | `user` | 0 |
| `manager` | `admin` | 1 |
| `admin` | `owner` | 2 |

Every hard-coded `role="admin"` that meant "see everything / top tier" (cli.py search/eval, settings_status counts, resync) becomes `role="owner"`. Every `role="developer"` default (slack channel surface) becomes `role="user"`. The none-mode implicit user becomes `owner`.

## File structure

- `src/hippo/roles.py` — **NEW.** Pure access-control: `ROLE_RANK`, `VALID_ROLES`, `DEFAULT_ROLE`, `rank()`, `can_read()`, `can_write()`, `readable_min_roles()`. No imports from the rest of hippo. The one place the rank rule lives.
- `src/hippo/db.py` — schema rewrite: `folders` tree, `documents.folder_id`, surrogate `users`/`tokens`, new role CHECK, seed three roots, legacy-DB guard.
- `src/hippo/storage.py` — all SQL: folder CRUD + tree ops, `documents.folder_id`, surrogate users/tokens, rank-based retrieval filtering. Drops `sources`/`_visible`/`MANAGER_ROLES`.
- `src/hippo/auth.py` — `resolve_role` defaults to `user`; `admin_emails` bootstrap → `owner`. `AuthenticatedUser` doc.
- `src/hippo/agent.py`, `mcp_server.py`, `slack_bot.py`, `cli.py` — role-string updates per the mapping table.
- `src/hippo/ingest.py` — ingest into a `folder_id`; `sync_folder` mounts/uses a folder row.
- `src/hippo/api.py` — `/folders` CRUD + `require_owner`; rank-based `require_admin`; `/ingest` takes `folder_ids`; role-list/validation updates.
- `ui/src/folders.ts` — **NEW.** Pure helpers: `flattenTree`, `writableFolders`, `uploadReducer` + types. Vitest-covered.
- `ui/src/App.tsx`, `ui/src/Settings.tsx` — Folders tab (tree) replacing Sources; upload modal.
- Tests: `tests/test_roles.py` (new), `tests/test_db.py`, `tests/test_storage.py`, `tests/test_storage_tokens.py`, `tests/test_search.py`, `tests/test_auth.py`, `tests/test_ingest.py`, `tests/test_api.py`, `tests/test_api_folders.py` (new), `tests/test_api_auth.py`, `tests/test_api_settings.py`, `tests/test_cli.py`, `tests/test_config.py`, `tests/test_env_example.py`, `ui/src/folders.test.ts` (new).

---

### Task 1: Pure access-control helpers (`roles.py`)

The rank rule, isolated and unit-tested, with zero dependencies. Everything else imports from here so the comparison is defined exactly once.

**Files:**
- Create: `src/hippo/roles.py`
- Test: `tests/test_roles.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_roles.py
import pytest

from hippo.roles import (
    DEFAULT_ROLE,
    ROLE_RANK,
    VALID_ROLES,
    can_read,
    can_write,
    rank,
    readable_min_roles,
)


def test_ranks_are_ordered():
    assert ROLE_RANK == {"user": 0, "admin": 1, "owner": 2}
    assert VALID_ROLES == ("user", "admin", "owner")
    assert DEFAULT_ROLE == "user"
    assert rank("user") < rank("admin") < rank("owner")


def test_rank_rejects_unknown_role():
    with pytest.raises(ValueError):
        rank("manager")  # old role name is gone


@pytest.mark.parametrize(
    "caller,folder,expected",
    [
        ("user", "user", True),
        ("user", "admin", False),
        ("user", "owner", False),
        ("admin", "user", True),
        ("admin", "admin", True),
        ("admin", "owner", False),
        ("owner", "user", True),
        ("owner", "admin", True),
        ("owner", "owner", True),
    ],
)
def test_can_read_is_rank_gte(caller, folder, expected):
    assert can_read(caller, folder) is expected


def test_can_write_requires_manual_origin_and_rank():
    assert can_write("owner", "owner", "manual") is True
    assert can_write("owner", "owner", "folder") is False  # synced = upload-locked
    assert can_write("owner", "owner", "repo") is False
    assert can_write("user", "admin", "manual") is False   # below tier


def test_readable_min_roles_grows_with_rank():
    assert readable_min_roles("user") == ("user",)
    assert readable_min_roles("admin") == ("user", "admin")
    assert readable_min_roles("owner") == ("user", "admin", "owner")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_roles.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'hippo.roles'`

- [ ] **Step 3: Write the implementation**

```python
# src/hippo/roles.py
"""Access-control primitives for the role-tiered folder model (SP1).

The single source of truth for the rank comparison. `storage.py` filters
retrieval with `readable_min_roles`; `api.py` gates writes with `can_write`.
Pure — imports nothing else from hippo — so the rule is testable in isolation
and there is exactly one definition of "who can see/write what"."""

ROLE_RANK: dict[str, int] = {"user": 0, "admin": 1, "owner": 2}
VALID_ROLES: tuple[str, ...] = ("user", "admin", "owner")
DEFAULT_ROLE = "user"


def rank(role: str) -> int:
    """Numeric rank for a role. Raises ValueError on an unknown role so a typo or
    a stale 'manager'/'developer' value fails loudly instead of silently denying."""
    try:
        return ROLE_RANK[role]
    except KeyError:
        raise ValueError(f"unknown role {role!r}; expected one of {VALID_ROLES}") from None


def can_read(caller_role: str, folder_min_role: str) -> bool:
    """A caller may read a folder iff their rank is at least the folder's tier."""
    return rank(caller_role) >= rank(folder_min_role)


def can_write(caller_role: str, folder_min_role: str, origin: str) -> bool:
    """A caller may upload into a folder iff it is a manual folder AND their rank
    is at least the folder's tier. Synced ('folder'/'repo') folders are pull-only."""
    return origin == "manual" and rank(caller_role) >= rank(folder_min_role)


def readable_min_roles(caller_role: str) -> tuple[str, ...]:
    """The set of folder tiers a caller may read, as a tuple of role names — used
    to build a `min_role IN (...)` SQL filter without rank math in SQL."""
    cr = rank(caller_role)
    return tuple(r for r in VALID_ROLES if ROLE_RANK[r] <= cr)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_roles.py -q`
Expected: PASS (all parametrized cases green)

- [ ] **Step 5: Commit**

```bash
git add src/hippo/roles.py tests/test_roles.py
git commit -m "feat(roles): pure rank-based access helpers (SP1)"
```

---

### Task 2: Data layer — folder tree, surrogate users, rank filtering

The schema rewrite (`db.py`) and the SQL rewrite (`storage.py`) move together so the suite is green at the task boundary. This is the largest task; follow the steps in order.

**Files:**
- Modify: `src/hippo/db.py`
- Modify: `src/hippo/storage.py`
- Test: `tests/test_db.py`, `tests/test_storage.py`, `tests/test_storage_tokens.py`, `tests/test_search.py`

#### 2A — Schema (`db.py`)

- [ ] **Step 1: Write the failing schema tests**

Replace the body of `tests/test_db.py` with these (keep any unrelated existing tests that still pass; the ones below assert the new schema):

```python
# tests/test_db.py
import pytest

from hippo.db import connect


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
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_db.py -q`
Expected: FAIL (old schema has `sources`/`source_id`, `users.email` PK, no `folders`).

- [ ] **Step 3: Rewrite `src/hippo/db.py`**

Replace the entire file with:

```python
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
    role TEXT NOT NULL DEFAULT 'user'
        CHECK (role IN ('user','admin','owner')),
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
        if "folder_id" not in doc_cols:
            raise RuntimeError(
                "incompatible legacy schema (pre-SP1, no folders table). SP1 uses a "
                "fresh schema with no data migration — recreate the database: "
                f"`rm {db_path}` and re-sync."
            )

    con.executescript(SCHEMA)
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
```

- [ ] **Step 4: Run schema tests**

Run: `uv run pytest tests/test_db.py -q`
Expected: PASS

- [ ] **Step 5: Commit the schema**

```bash
git add src/hippo/db.py tests/test_db.py
git commit -m "feat(db): folder-tree schema, surrogate users, legacy guard (SP1)"
```

#### 2B — Storage layer (`storage.py`)

The whole data-access layer now speaks folders + ranks. Implement the methods below, then update the data-layer tests.

- [ ] **Step 6: Replace the module preamble (imports, dataclasses, constants)**

In `src/hippo/storage.py`, replace lines 1–53 (the imports through `_visible`) with:

```python
import hashlib
import re
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import regex
import sqlite_vec

from .chunking import Chunk
from .embeddings import Embedder
from .roles import DEFAULT_ROLE, VALID_ROLES, readable_min_roles


@dataclass
class Document:
    id: int
    source_type: str
    path: str
    title: str
    content: str
    content_hash: str
    summary: str | None


@dataclass
class SearchHit:
    chunk_id: int
    document_id: int
    path: str
    title: str
    heading_path: str
    text: str
    score: float


@dataclass
class Folder:
    id: int
    parent_id: int | None
    name: str
    min_role: str
    origin: str          # manual | folder | repo
    location: str | None
    doc_count: int


GREP_MAX_PATTERN = 200      # reject absurdly long patterns
GREP_TIMEOUT_S = 2.0        # wall-clock cap per chunk scan (regex module)


def _norm_email(e: str) -> str:
    return e.strip().lower()


def _role_filter(role: str) -> tuple[str, tuple[str, ...]]:
    """Return an SQL fragment + params restricting documents to folders the role
    may read. Joins assume the folders table is aliased `f`."""
    allowed = readable_min_roles(role)  # raises ValueError on an unknown role
    placeholders = ",".join("?" * len(allowed))
    return f"f.min_role IN ({placeholders})", allowed
```

- [ ] **Step 7: Rewrite the document read/write methods**

Replace `upsert_document`, `get_document`, `list_documents`, and `paths_for_source` (lines ~95–199 in the old file) with the versions below. `upsert_document` now takes a required `folder_id` (no `source_id`); reads join `documents → folders` and filter by rank.

```python
    def upsert_document(
        self,
        *,
        source_type: str,
        path: str,
        title: str,
        content: str,
        content_hash: str,
        chunks: list[Chunk],
        embed_inputs: list[str],
        folder_id: int,
        summary: str | None = None,
    ) -> int:
        """Insert or replace a document (and all its chunks) atomically. Every
        document lives in exactly one folder, which carries its access tier."""
        assert len(chunks) == len(embed_inputs)
        self._ensure_embedding_model()  # fail fast before spending an embed call
        vectors = self.embedder.embed(embed_inputs)  # network: outside the lock
        with self._lock, self.con:  # one transaction per document
            row = self.con.execute("SELECT id FROM documents WHERE path=?", (path,)).fetchone()
            if row:
                doc_id = row[0]
                self._delete_chunks(doc_id)
                self.con.execute(
                    """UPDATE documents SET source_type=?, title=?, content=?, content_hash=?,
                       summary=?, folder_id=?, synced_at=datetime('now') WHERE id=?""",
                    (source_type, title, content, content_hash, summary, folder_id, doc_id),
                )
            else:
                cur = self.con.execute(
                    """INSERT INTO documents(source_type, path, title, content, content_hash, summary, folder_id)
                       VALUES (?,?,?,?,?,?,?)""",
                    (source_type, path, title, content, content_hash, summary, folder_id),
                )
                doc_id = cur.lastrowid
            for chunk, vec in zip(chunks, vectors):
                cur = self.con.execute(
                    "INSERT INTO chunks(document_id, position, heading_path, text) VALUES (?,?,?,?)",
                    (doc_id, chunk.position, chunk.heading_path, chunk.text),
                )
                self.con.execute(
                    "INSERT INTO chunk_vec(rowid, embedding) VALUES (?,?)",
                    (cur.lastrowid, sqlite_vec.serialize_float32(vec)),
                )
        return doc_id

    def get_document(self, doc_id: int, *, role: str) -> Document | None:
        where, params = _role_filter(role)
        with self._lock:
            row = self.con.execute(
                f"""SELECT d.id, d.source_type, d.path, d.title, d.content, d.content_hash, d.summary
                    FROM documents d JOIN folders f ON f.id = d.folder_id
                    WHERE d.id=? AND {where}""",
                (doc_id, *params),
            ).fetchone()
        return Document(*row) if row else None

    def list_documents(self, query: str | None = None, *, role: str) -> list[Document]:
        where, params = _role_filter(role)
        sql = ("SELECT d.id, d.source_type, d.path, d.title, d.content, d.content_hash, d.summary "
               "FROM documents d JOIN folders f ON f.id = d.folder_id WHERE " + where)
        args: list = list(params)
        if query:
            sql += " AND (d.title LIKE ? OR d.path LIKE ? OR coalesce(d.summary,'') LIKE ?)"
            like = f"%{query}%"
            args += [like, like, like]
        sql += " ORDER BY d.path"
        with self._lock:
            return [Document(*r) for r in self.con.execute(sql, args)]

    def paths_for_folder(self, folder_id: int) -> set[str]:
        with self._lock:
            return {r[0] for r in self.con.execute(
                "SELECT path FROM documents WHERE folder_id=?", (folder_id,))}
```

- [ ] **Step 8: Replace the sources section with folder CRUD + tree ops**

Replace the entire `# -- sources --` block (`register_source`, `list_sources`, `delete_source`, lines ~201–240) with:

```python
    # -- folders -----------------------------------------------------------

    def get_folder(self, folder_id: int) -> Folder | None:
        """Fetch one folder (no role filter — callers gate on the returned
        min_role/origin). doc_count is the folder's own documents."""
        with self._lock:
            row = self.con.execute(
                """SELECT f.id, f.parent_id, f.name, f.min_role, f.origin, f.location,
                          (SELECT count(*) FROM documents d WHERE d.folder_id = f.id)
                   FROM folders f WHERE f.id=?""",
                (folder_id,),
            ).fetchone()
        return Folder(*row) if row else None

    def list_folders(self, *, role: str) -> list[Folder]:
        """Every folder the caller may read, ordered for tree rendering (roots
        first, then by name). Filtered by rank on the folder's own tier."""
        allowed = readable_min_roles(role)
        ph = ",".join("?" * len(allowed))
        with self._lock:
            rows = self.con.execute(
                f"""SELECT f.id, f.parent_id, f.name, f.min_role, f.origin, f.location,
                           (SELECT count(*) FROM documents d WHERE d.folder_id = f.id)
                    FROM folders f WHERE f.min_role IN ({ph})
                    ORDER BY (f.parent_id IS NOT NULL), f.parent_id, f.name""",
                allowed,
            ).fetchall()
        return [Folder(*r) for r in rows]

    def create_folder(self, *, parent_id: int, name: str,
                      origin: str = "manual", location: str | None = None) -> int:
        """Create a child folder inheriting the parent's tier. parent_id is
        required (the three roots are seeded, never created here). Raises
        ValueError on a missing parent or a duplicate sibling name."""
        name = name.strip()
        if not name:
            raise ValueError("folder name cannot be empty")
        with self._lock, self.con:
            prow = self.con.execute(
                "SELECT min_role FROM folders WHERE id=?", (parent_id,)).fetchone()
            if prow is None:
                raise ValueError(f"no folder with id {parent_id}")
            try:
                cur = self.con.execute(
                    "INSERT INTO folders(parent_id, name, min_role, origin, location) "
                    "VALUES (?,?,?,?,?)",
                    (parent_id, name, prow[0], origin, location),
                )
            except sqlite3.IntegrityError as e:
                raise ValueError(f"a folder named {name!r} already exists here") from e
            return cur.lastrowid

    def folder_by_location(self, location: str) -> int | None:
        with self._lock:
            row = self.con.execute(
                "SELECT id FROM folders WHERE location=?", (location,)).fetchone()
        return row[0] if row else None

    def folder_path(self, folder_id: int) -> str:
        """The slash-joined ancestor path, e.g. 'Default/Retail'. Used to
        folder-qualify upload document paths so the same filename in two folders
        stays unique and the citation reads meaningfully."""
        with self._lock:
            parts: list[str] = []
            cur_id: int | None = folder_id
            while cur_id is not None:
                row = self.con.execute(
                    "SELECT parent_id, name FROM folders WHERE id=?", (cur_id,)).fetchone()
                if row is None:
                    break
                parts.append(row[1])
                cur_id = row[0]
        return "/".join(reversed(parts))

    def rename_folder(self, folder_id: int, new_name: str) -> None:
        new_name = new_name.strip()
        if not new_name:
            raise ValueError("folder name cannot be empty")
        with self._lock, self.con:
            try:
                cur = self.con.execute(
                    "UPDATE folders SET name=? WHERE id=?", (new_name, folder_id))
            except sqlite3.IntegrityError as e:
                raise ValueError(f"a folder named {new_name!r} already exists here") from e
            if cur.rowcount == 0:
                raise ValueError(f"no folder with id {folder_id}")

    def _subtree_ids(self, folder_id: int) -> list[int]:
        """folder_id plus all descendants (recursive). Caller holds the lock."""
        rows = self.con.execute(
            """WITH RECURSIVE sub(id) AS (
                   SELECT ? UNION ALL
                   SELECT f.id FROM folders f JOIN sub ON f.parent_id = sub.id)
               SELECT id FROM sub""",
            (folder_id,),
        ).fetchall()
        return [r[0] for r in rows]

    def move_folder(self, folder_id: int, new_parent_id: int) -> None:
        """Reparent a folder; rewrites the whole moved subtree's tier to the new
        parent's tier (no per-subfolder overrides in SP1). Refuses moving a root,
        moving under itself/a descendant (cycle), or a duplicate sibling name."""
        with self._lock, self.con:
            row = self.con.execute(
                "SELECT parent_id FROM folders WHERE id=?", (folder_id,)).fetchone()
            if row is None:
                raise ValueError(f"no folder with id {folder_id}")
            if row[0] is None:
                raise ValueError("cannot move a root folder")
            prow = self.con.execute(
                "SELECT min_role FROM folders WHERE id=?", (new_parent_id,)).fetchone()
            if prow is None:
                raise ValueError(f"no folder with id {new_parent_id}")
            subtree = self._subtree_ids(folder_id)
            if new_parent_id in subtree:
                raise ValueError("cannot move a folder under itself")
            try:
                self.con.execute(
                    "UPDATE folders SET parent_id=? WHERE id=?", (new_parent_id, folder_id))
            except sqlite3.IntegrityError as e:
                raise ValueError("a folder with that name already exists in the target") from e
            ph = ",".join("?" * len(subtree))
            self.con.execute(
                f"UPDATE folders SET min_role=? WHERE id IN ({ph})", (prow[0], *subtree))

    def delete_folder(self, folder_id: int) -> bool:
        """Delete a folder, its descendants, and all their documents/chunks/vectors.
        Roots cannot be deleted. Returns False if the folder does not exist."""
        with self._lock:
            row = self.con.execute(
                "SELECT parent_id FROM folders WHERE id=?", (folder_id,)).fetchone()
            if row is None:
                return False
            if row[0] is None:
                raise ValueError("cannot delete a root folder")
            subtree = self._subtree_ids(folder_id)
            ph = ",".join("?" * len(subtree))
            doc_ids = [r[0] for r in self.con.execute(
                f"SELECT id FROM documents WHERE folder_id IN ({ph})", subtree)]
            with self.con:
                for did in doc_ids:
                    self._delete_chunks(did)
                # ON DELETE CASCADE removes descendant folders + their documents,
                # but chunk_vec (vec0) is not FK-managed, so chunks were cleared above.
                self.con.execute("DELETE FROM folders WHERE id=?", (folder_id,))
            return True
```

- [ ] **Step 9: Rewrite the users/tokens methods for the surrogate key**

Replace the `# -- users / roles --` and `# -- personal access tokens --` blocks (lines ~242–337). Public signatures still take `email` (the login identity); internally they resolve to `user_id`.

```python
    # -- users / roles -------------------------------------------------------

    def _user_id_for(self, email: str) -> int:
        """Resolve email → user_id, creating the user (DEFAULT_ROLE) on first sight.
        Caller holds the lock."""
        email = _norm_email(email)
        row = self.con.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
        if row:
            return row[0]
        cur = self.con.execute("INSERT INTO users(email) VALUES (?)", (email,))
        return cur.lastrowid

    def ensure_user(self, email: str) -> str:
        """Create on first sight with the default role; return the current role."""
        email = _norm_email(email)
        with self._lock:
            row = self.con.execute("SELECT role FROM users WHERE email=?", (email,)).fetchone()
            if row:
                return row[0]
            with self.con:
                self.con.execute("INSERT INTO users(email) VALUES (?)", (email,))
            return DEFAULT_ROLE

    def set_role(self, email: str, role: str) -> None:
        email = _norm_email(email)
        if role not in VALID_ROLES:
            raise ValueError(f"invalid role {role!r}; expected one of {VALID_ROLES}")
        with self._lock, self.con:
            self.con.execute(
                "INSERT INTO users(email, role) VALUES (?,?) "
                "ON CONFLICT(email) DO UPDATE SET role=excluded.role",
                (email, role),
            )

    def list_users(self) -> list[tuple[str, str]]:
        with self._lock:
            return list(self.con.execute("SELECT email, role FROM users ORDER BY email"))

    # -- personal access tokens ---------------------------------------------

    def create_token_returning_id(self, email: str, name: str = "") -> tuple[int, str]:
        """Mint a bearer token tied to a user_id; return (id, plaintext). Only the
        sha256 is stored. The id is the insert's lastrowid (same statement)."""
        token = "hk_" + secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self._lock, self.con:
            uid = self._user_id_for(email)
            cur = self.con.execute(
                "INSERT INTO tokens(token_hash, user_id, name) VALUES (?,?,?)",
                (digest, uid, name),
            )
        return cur.lastrowid, token

    def create_token(self, email: str, name: str = "") -> str:
        return self.create_token_returning_id(email, name)[1]

    def resolve_token(self, token: str) -> str | None:
        """Return the owning user's email for a valid token, else None."""
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self._lock:
            row = self.con.execute(
                "SELECT u.email FROM tokens t JOIN users u ON u.id = t.user_id "
                "WHERE t.token_hash=?", (digest,)
            ).fetchone()
            if row:
                with self.con:
                    self.con.execute(
                        "UPDATE tokens SET last_used_at=datetime('now') WHERE token_hash=?",
                        (digest,),
                    )
        return row[0] if row else None

    def list_tokens(self, email: str) -> list[tuple[int, str, str, str | None]]:
        """(id, name, created_at, last_used_at) for all tokens belonging to email."""
        email = _norm_email(email)
        with self._lock:
            return list(self.con.execute(
                "SELECT t.id, t.name, t.created_at, t.last_used_at FROM tokens t "
                "JOIN users u ON u.id = t.user_id WHERE u.email=? ORDER BY t.id",
                (email,),
            ))

    def revoke_token(self, token_id: int, email: str) -> bool:
        """Delete the token matching both id and owner-email."""
        email = _norm_email(email)
        with self._lock, self.con:
            cur = self.con.execute(
                "DELETE FROM tokens WHERE id=? AND user_id=(SELECT id FROM users WHERE email=?)",
                (token_id, email),
            )
        return cur.rowcount > 0

    def list_all_tokens(self) -> list[tuple[int, str, str, str, str | None]]:
        """All users' tokens (admin view): (id, email, name, created_at, last_used_at)."""
        with self._lock:
            return list(self.con.execute(
                "SELECT t.id, u.email, t.name, t.created_at, t.last_used_at "
                "FROM tokens t JOIN users u ON u.id = t.user_id ORDER BY u.email, t.id"
            ))

    def revoke_token_any(self, token_id: int) -> bool:
        with self._lock, self.con:
            cur = self.con.execute("DELETE FROM tokens WHERE id = ?", (token_id,))
        return cur.rowcount > 0
```

- [ ] **Step 10: Update the search/grep joins to folders + rank filtering**

In the search section, replace the three `LEFT JOIN sources s ON s.id = d.source_id` joins and their `s.access` filters. Replace `_search_fts`, `_search_vec`, `_visible_ids`, `_hit`, and `grep` with these bodies (the public `search_hybrid` keeps its signature; `_hit` no longer needs the access column):

```python
    def _search_fts(self, query: str, limit: int, role: str) -> list[int]:
        tokens = [t for t in re.findall(r"\w+", query) if t]
        if not tokens:
            return []
        match = " OR ".join(f'"{t}"' for t in tokens)
        where, params = _role_filter(role)
        sql = f"""SELECT chunks_fts.rowid FROM chunks_fts
                  JOIN chunks c ON c.id = chunks_fts.rowid
                  JOIN documents d ON d.id = c.document_id
                  JOIN folders f ON f.id = d.folder_id
                  WHERE chunks_fts MATCH ? AND {where}
                  ORDER BY bm25(chunks_fts) LIMIT ?"""
        rows = self.con.execute(sql, (match, *params, limit))
        return [r[0] for r in rows]

    def _search_vec(self, vec: list[float], limit: int, role: str) -> list[int]:
        serialized = sqlite_vec.serialize_float32(vec)
        total = self.con.execute("SELECT count(*) FROM chunks").fetchone()[0]
        k = limit
        while True:
            rows = [r[0] for r in self.con.execute(
                "SELECT rowid FROM chunk_vec WHERE embedding MATCH ? AND k = ? ORDER BY distance",
                (serialized, k),
            )]
            visible = self._visible_ids(rows, role)
            if len(visible) >= limit or len(rows) < k or k >= total:
                return visible[:limit]
            k = min(k * 4, total)

    def _visible_ids(self, chunk_ids: list[int], role: str) -> list[int]:
        """Filter chunk ids to those the role may see, preserving order. Also drops
        orphan vec rowids (no chunks row) via the join."""
        if not chunk_ids:
            return []
        where, params = _role_filter(role)
        ph = ",".join("?" * len(chunk_ids))
        sql = f"""SELECT c.id FROM chunks c
                  JOIN documents d ON d.id = c.document_id
                  JOIN folders f ON f.id = d.folder_id
                  WHERE c.id IN ({ph}) AND {where}"""
        vis = {r[0] for r in self.con.execute(sql, (*chunk_ids, *params))}
        return [cid for cid in chunk_ids if cid in vis]

    def _hit(self, chunk_id: int, score: float, role: str) -> SearchHit | None:
        where, params = _role_filter(role)
        row = self.con.execute(
            f"""SELECT c.id, d.id, d.path, d.title, c.heading_path, c.text
                FROM chunks c JOIN documents d ON d.id = c.document_id
                JOIN folders f ON f.id = d.folder_id
                WHERE c.id=? AND {where}""",
            (chunk_id, *params),
        ).fetchone()
        return SearchHit(*row, score=score) if row else None
```

And replace `grep`'s SQL + visibility loop (it currently selects `s.access` and calls `_visible`):

```python
    def grep(self, pattern: str, limit: int = 20, *, role: str) -> list[SearchHit]:
        """Exact/regex scan over raw chunk text, role-filtered by folder tier."""
        if len(pattern) > GREP_MAX_PATTERN:
            raise ValueError(f"pattern too long (max {GREP_MAX_PATTERN} chars)")
        try:
            rx = regex.compile(pattern, regex.IGNORECASE)
        except regex.error as e:
            raise ValueError(f"invalid regex pattern {pattern!r}: {e}") from e
        where, params = _role_filter(role)
        with self._lock:
            rows = self.con.execute(
                f"""SELECT c.id, d.id, d.path, d.title, c.heading_path, c.text
                    FROM chunks c JOIN documents d ON d.id = c.document_id
                    JOIN folders f ON f.id = d.folder_id WHERE {where}""",
                params,
            ).fetchall()
        hits: list[SearchHit] = []
        deadline = time.monotonic() + GREP_TIMEOUT_S
        for row in rows:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError(f"pattern took too long (>{GREP_TIMEOUT_S}s)")
            try:
                matched = rx.search(row[5], timeout=remaining)
            except TimeoutError as e:
                raise ValueError(f"pattern took too long (>{GREP_TIMEOUT_S}s)") from e
            if matched:
                hits.append(SearchHit(*row, score=1.0))
                if len(hits) >= limit:
                    break
        return hits
```

> Note: `search_hybrid`'s body is unchanged except it now relies on the rewritten helpers; the `_hit` comment about role filtering still holds. Leave `_ensure_embedding_model`, `_delete_chunks`, `delete_document_by_path`, `document_exists`, `is_unchanged`, `reindex`, and `backup` as they are.

- [ ] **Step 11: Update the data-layer tests for folders + ranks**

In `tests/test_storage.py` and `tests/test_search.py`, apply these concrete changes:

1. Helpers that build docs must pass a `folder_id`. Add a fixture that grabs the seeded roots and update `_add_doc`/`_doc` to accept and pass `folder_id` (default to the Default root). Example for `test_storage.py`:

```python
def _roots(store):
    """(user_root_id, admin_root_id, owner_root_id) from the seeded tree."""
    rows = store.con.execute(
        "SELECT min_role, id FROM folders WHERE parent_id IS NULL").fetchall()
    by_role = {r: i for r, i in rows}
    return by_role["user"], by_role["admin"], by_role["owner"]


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
```

2. Replace every `role="admin"` (meaning "see all") with `role="owner"`, every `role="manager"` with `role="admin"`, and every `role="developer"` with `role="user"`, in assertions across `test_storage.py` and `test_search.py`.

3. Replace any access/sources test (`register_source`, `list_sources`, `delete_source`, `access="managers"`) with the folder-tier equivalents below; add them to `test_storage.py`:

```python
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
```

4. In `test_storage_tokens.py`: tokens are now keyed by user_id, but every public method still takes `email`, so the existing assertions hold **except** any that read the `tokens.email` column via raw SQL — change such raw reads to join `users`. Add:

```python
def test_token_resolves_after_email_attribute_change(store):
    store.set_role("dev@x.com", "user")
    tok = store.create_token("dev@x.com", "laptop")
    # email is now a mutable attribute on the surrogate-keyed row
    store.con.execute("UPDATE users SET email='dev2@x.com' WHERE email='dev@x.com'")
    store.con.commit()
    assert store.resolve_token(tok) == "dev2@x.com"  # token followed the user_id
```

- [ ] **Step 12: Run the data-layer tests**

Run: `uv run pytest tests/test_db.py tests/test_storage.py tests/test_storage_tokens.py tests/test_search.py tests/test_roles.py -q`
Expected: PASS

- [ ] **Step 13: Commit the storage layer**

```bash
git add src/hippo/storage.py tests/test_storage.py tests/test_storage_tokens.py tests/test_search.py
git commit -m "feat(storage): folder-tree CRUD + rank-filtered retrieval, surrogate users/tokens (SP1)"
```

---

### Task 3: Identity & role-string call sites

Point identity resolution at the new defaults and update the four role-string consumers per the mapping table.

**Files:**
- Modify: `src/hippo/auth.py`, `src/hippo/agent.py`, `src/hippo/mcp_server.py`, `src/hippo/slack_bot.py`, `src/hippo/cli.py`
- Test: `tests/test_auth.py`, `tests/test_slack_bot.py`, `tests/test_mcp_server.py`

- [ ] **Step 1: Write/adjust failing tests**

In `tests/test_auth.py`, update `resolve_role` expectations and add the owner-bootstrap assertion:

```python
def test_resolve_role_defaults_to_user_and_bootstraps_owner(tmp_path):
    from hippo.config import Settings
    from hippo.db import connect
    from hippo.embeddings import FakeEmbedder
    from hippo.storage import Storage
    from hippo.auth import resolve_role

    con = connect(tmp_path / "t.db", embedding_dim=32)
    store = Storage(con, FakeEmbedder(dim=32))
    s = Settings(_env_file=None, admin_emails="boss@x.com")
    assert resolve_role(store, s, "newbie@x.com") == "user"
    assert resolve_role(store, s, "boss@x.com") == "owner"
```

In `tests/test_slack_bot.py`, the `surface_role` channel default changes from `"developer"` to `"user"`:

```python
def test_surface_role_channel_forces_user():
    from hippo.slack_bot import surface_role
    assert surface_role("owner", is_dm=False) == "user"
    assert surface_role("owner", is_dm=True) == "owner"
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_auth.py::test_resolve_role_defaults_to_user_and_bootstraps_owner tests/test_slack_bot.py -q`
Expected: FAIL (resolve_role returns "developer"; surface_role returns "developer").

- [ ] **Step 3: Update `auth.py`**

In `src/hippo/auth.py`:
- Change the `AuthenticatedUser.role` comment to `# user | admin | owner`.
- In `resolve_role`, change the bootstrap line from `role = "admin"` to `role = "owner"`:

```python
    role = store.ensure_user(email)
    if email in settings.admin_email_list:
        role = "owner"  # env bootstrap is the top tier (spec §3)
    return role
```

(The default-role text in the docstring should read "first-timers default to 'user'".)

- [ ] **Step 4: Update the other call sites**

`src/hippo/agent.py` — `HubDeps.role` comment → `# user | admin | owner — filters every tool's retrieval`.

`src/hippo/mcp_server.py` — in `build_mcp_server`, the local-default fallback `return "admin"` becomes `return "owner"` (stdio local owner). Update the docstring "defaults to the single local admin user" → "owner".

`src/hippo/slack_bot.py` — `surface_role`: change the channel branch to return `"user"` and update the docstring ("force the 'everyone'-access view (user)").

`src/hippo/cli.py`:
- `_mcp_role.set("admin")` → `_mcp_role.set("owner")` in `mcp()`.
- `search()`: `role="admin"` → `role="owner"`.
- `eval()`: `role="admin"` → `role="owner"`.
- `sync()` / `run_all`: `store.list_sources(role="admin")` calls and the `_, kind, loc, _access` unpacking must change — `list_sources` is gone. Replace with iterating synced folders: see Task 4 (CLI sync is rewritten there). For now, in this task, change the `role` strings only and leave the `list_sources` calls; they will be replaced in Task 4. To keep this task's suite green, temporarily guard `sync` by listing synced folders via the new API: change both `store.list_sources(role="admin")` occurrences to `store.list_folders(role="owner")` and the unpacking to use `.origin`/`.location`:

```python
        folders = ([Path(folder)] if folder
                   else [Path(f.location) for f in store.list_folders(role="owner")
                         if f.origin == "folder" and f.location])
```

```python
        targets = ([folder] if folder
                   else [f.location for f in store.list_folders(role="owner")
                         if f.origin == "folder" and f.location])
```

- `role` Typer help text: `role_app = typer.Typer(help="Manage user roles (user | admin | owner).")`.

- [ ] **Step 5: Run the affected tests**

Run: `uv run pytest tests/test_auth.py tests/test_slack_bot.py tests/test_mcp_server.py -q`
Expected: PASS (update any remaining `developer`/`manager`/`admin`→top assertions in these files per the mapping table).

- [ ] **Step 6: Commit**

```bash
git add src/hippo/auth.py src/hippo/agent.py src/hippo/mcp_server.py src/hippo/slack_bot.py src/hippo/cli.py tests/test_auth.py tests/test_slack_bot.py tests/test_mcp_server.py
git commit -m "feat: role rename (user/admin/owner) across identity + call sites (SP1)"
```

---

### Task 4: Ingestion into folders

`Ingestor` now writes into a `folder_id`; `sync_folder` mounts (or re-uses) a folder row of `origin='folder'` under a chosen parent and tags its documents with that folder.

**Files:**
- Modify: `src/hippo/ingest.py`
- Test: `tests/test_ingest.py`

- [ ] **Step 1: Write the failing tests**

In `tests/test_ingest.py`, update the fixture to seed roots and add:

```python
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
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_ingest.py -q`
Expected: FAIL (`sync_folder`/`ingest_bytes` don't take these args; `register_source` gone).

- [ ] **Step 3: Update `ingest.py`**

- `ingest_file` keeps `source_type`/`folder_id` (rename `source_id` → `folder_id`, required):

```python
    def ingest_file(self, path: Path, *, source_type: str, folder_id: int) -> IngestResult:
        try:
            title, md = parse_file(path)
            return self._index(str(path), title, md, source_type=source_type, folder_id=folder_id)
        except Exception as e:  # per-file isolation: one bad file never kills a sync
            log.warning("failed %s: %s", path, e)
            return IngestResult(path=str(path), status="failed", error=str(e))
```

- `ingest_bytes` takes `folder_id` (required) + `path_prefix` (the folder's tree path) and stores a folder-qualified path:

```python
    def ingest_bytes(self, name: str, data: bytes, *, folder_id: int, path_prefix: str,
                     suffix: str = ".md", source_type: str = "upload") -> IngestResult:
        try:
            if suffix.lower() not in SUPPORTED:
                raise ValueError(f"unsupported file type: {suffix}")
            title, md = parse_bytes(name, data, suffix,
                                    max_decompressed=self.max_decompressed_bytes)
            path = f"{path_prefix}/{name}" if path_prefix else name
            return self._index(path, title, md, source_type=source_type, folder_id=folder_id)
        except Exception as e:
            log.warning("failed %s: %s", name, e)
            return IngestResult(path=name, status="failed", error=str(e))
```

- `ingest_text` forwards the new kwargs:

```python
    def ingest_text(self, name: str, raw: str, *, folder_id: int, path_prefix: str = "",
                    suffix: str = ".md", source_type: str = "upload") -> IngestResult:
        return self.ingest_bytes(name, raw.encode("utf-8"), folder_id=folder_id,
                                 path_prefix=path_prefix, suffix=suffix, source_type=source_type)
```

- `_index` takes `folder_id` (replace `source_id`), passing it to `upsert_document(folder_id=folder_id)`.

- `sync_folder` mounts a synced folder row and tags docs:

```python
def sync_folder(folder: Path, store: Storage, *, parent_id: int, max_chars: int,
                overlap_chars: int, enricher=None, max_doc_chars: int | None = None) -> SyncReport:
    """Sync one filesystem folder as a pull-only ('folder' origin) node under
    parent_id, inheriting the parent's tier. Ingest new/changed, remove vanished."""
    folder_id = store.folder_by_location(str(folder))
    if folder_id is None:
        folder_id = store.create_folder(parent_id=parent_id, name=folder.name,
                                         origin="folder", location=str(folder))
    ing = Ingestor(store, max_chars=max_chars, overlap_chars=overlap_chars,
                   enricher=enricher, max_doc_chars=max_doc_chars)
    report = SyncReport()
    seen: set[str] = set()
    for path in sorted(folder.rglob("*")):
        if _ignored(path):
            continue
        if path.is_file() and path.suffix.lower() in SUPPORTED:
            seen.add(str(path))
            report.results.append(ing.ingest_file(path, source_type="folder", folder_id=folder_id))
        elif path.is_file():
            report.results.append(ing.ingest_file(path, source_type="folder", folder_id=folder_id))
    for stale in store.paths_for_folder(folder_id) - seen:
        if store.delete_document_by_path(stale):
            report.removed += 1
    return report
```

- [ ] **Step 4: Run ingest tests**

Run: `uv run pytest tests/test_ingest.py -q`
Expected: PASS (update the existing fixture so the db path is outside the synced dir, per the file's existing pattern, and pass `folder_id`/`parent_id`).

- [ ] **Step 5: Wire CLI `sync` to mount under Default**

In `src/hippo/cli.py` `sync()`/`run_all`, resolve the Default root and pass `parent_id`:

```python
        default_root = next(
            f.id for f in store.list_folders(role="owner")
            if f.parent_id is None and f.min_role == "user")
        for f in folders:
            report = sync_folder(
                f, store, parent_id=default_root, max_chars=settings.chunk_max_chars,
                overlap_chars=settings.chunk_overlap_chars, enricher=enricher,
                max_doc_chars=settings.max_doc_chars,
            )
            typer.echo(f"{f}: {report.summary()}")
```

Update `cli.add()` to ingest into Default too:

```python
@app.command()
def add(file: str):
    """Ingest a single file into the Default folder."""
    settings = Settings()
    store, ing = _store(settings)
    default_root = next(
        f.id for f in store.list_folders(role="owner")
        if f.parent_id is None and f.min_role == "user")
    res = ing.ingest_file(Path(file), source_type="upload", folder_id=default_root)
    typer.echo(f"{res.path}: {res.status} ({res.chunks} chunks)"
               + (f" error: {res.error}" if res.error else ""))
    if res.status == "failed":
        raise typer.Exit(1)
```

- [ ] **Step 6: Run ingest + cli tests**

Run: `uv run pytest tests/test_ingest.py tests/test_cli.py -q`
Expected: PASS (update `test_cli.py` calls to match new signatures / role names).

- [ ] **Step 7: Commit**

```bash
git add src/hippo/ingest.py src/hippo/cli.py tests/test_ingest.py tests/test_cli.py
git commit -m "feat(ingest): ingest into folder_id; sync_folder mounts pull-only node (SP1)"
```

---

### Task 5: `/folders` API + owner guard

Replace the `/sources` endpoints with `/folders` CRUD, add `require_owner`, and make `require_admin` rank-based.

**Files:**
- Modify: `src/hippo/api.py`
- Test: `tests/test_api_folders.py` (new), `tests/test_api.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_api_folders.py
from fastapi.testclient import TestClient

from hippo.api import build_app
from hippo.config import Settings


def _settings(tmp_path, **over):
    base = dict(_env_file=None, db_path=tmp_path / "t.db", embedding_model="fake",
                embedding_dim=32, enrich_enabled=False)
    base.update(over)
    return Settings(**base)


def test_get_folders_returns_seeded_tree(tmp_path):
    c = TestClient(build_app(_settings(tmp_path)))  # none-mode caller is owner
    rows = c.get("/folders").json()
    names = {r["name"]: r for r in rows}
    assert {"Default", "Private", "Owner"} <= set(names)
    assert names["Default"]["tier"] == "user" and names["Default"]["writable"] is True
    assert names["Owner"]["tier"] == "owner"


def test_create_rename_move_delete_folder(tmp_path):
    c = TestClient(build_app(_settings(tmp_path)))
    rows = c.get("/folders").json()
    default_id = next(r["id"] for r in rows if r["name"] == "Default")
    owner_id = next(r["id"] for r in rows if r["name"] == "Owner")
    # create
    r = c.post("/folders", json={"parent_id": default_id, "name": "Retail"})
    assert r.status_code == 200
    fid = r.json()["id"]
    assert r.json()["tier"] == "user"
    # duplicate sibling rejected
    assert c.post("/folders", json={"parent_id": default_id, "name": "Retail"}).status_code == 400
    # rename
    assert c.patch(f"/folders/{fid}", json={"name": "RetailOps"}).status_code == 200
    # move across roots rewrites tier
    assert c.patch(f"/folders/{fid}", json={"parent_id": owner_id}).status_code == 200
    moved = next(x for x in c.get("/folders").json() if x["id"] == fid)
    assert moved["tier"] == "owner"
    # delete
    assert c.delete(f"/folders/{fid}").status_code == 200
    # roots are undeletable
    assert c.delete(f"/folders/{default_id}").status_code == 400


def test_non_owner_cannot_create_folder_in_iap_mode(tmp_path):
    import time
    import jwt
    from cryptography.hazmat.primitives.asymmetric import ec
    from hippo.auth import IapVerifier

    AUD = "/projects/1/global/backendServices/2"
    s = _settings(tmp_path, auth_mode="iap", iap_audience=AUD)
    key = ec.generate_private_key(ec.SECP256R1())
    app = build_app(s, iap_verifier=IapVerifier(AUD, key_fetcher=lambda: {"k1": key.public_key()}))
    c = TestClient(app)
    tok = jwt.encode({"aud": AUD, "iss": "https://cloud.google.com/iap",
                      "exp": int(time.time()) + 600, "email": "dev@x.com"},
                     key, algorithm="ES256", headers={"kid": "k1"})
    h = {"x-goog-iap-jwt-assertion": tok}
    default_id = next(r["id"] for r in c.get("/folders", headers=h).json() if r["name"] == "Default")
    # a plain user (rank 0) cannot create folders (admin+ only)
    assert c.post("/folders", json={"parent_id": default_id, "name": "X"}, headers=h).status_code == 403
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_api_folders.py -q`
Expected: FAIL (`/folders` doesn't exist; `/sources` still mounted).

- [ ] **Step 3: Update `api.py` guards + models**

Replace `require_admin` and add `require_owner` (import `rank` at top: `from .roles import rank`):

```python
    async def require_admin(user: AuthenticatedUser = Depends(verify_request)) -> AuthenticatedUser:
        if rank(user.role) < 1:  # admin or owner
            raise HTTPException(status_code=403, detail="admin only")
        return user

    async def require_owner(user: AuthenticatedUser = Depends(verify_request)) -> AuthenticatedUser:
        if rank(user.role) < 2:
            raise HTTPException(status_code=403, detail="owner only")
        return user
```

Replace the `SourceIn` model with folder models:

```python
class FolderIn(BaseModel):
    parent_id: int
    name: str
    origin: Literal["manual", "folder", "repo"] = "manual"
    location: str | None = None


class FolderPatch(BaseModel):
    name: str | None = None
    parent_id: int | None = None
```

- [ ] **Step 4: Replace the `/sources` routes with `/folders`**

Remove the four `/sources*` routes and add:

```python
    @app.get("/folders")
    async def folders(user: AuthenticatedUser = Depends(verify_request)):
        from .roles import can_write
        return [
            {"id": f.id, "parent_id": f.parent_id, "name": f.name, "tier": f.min_role,
             "origin": f.origin, "doc_count": f.doc_count,
             "writable": can_write(user.role, f.min_role, f.origin)}
            for f in store.list_folders(role=user.role)
        ]

    @app.post("/folders")
    async def create_folder(body: FolderIn, user: AuthenticatedUser = Depends(require_admin)):
        from .roles import rank as _rank
        parent = store.get_folder(body.parent_id)
        if parent is None:
            raise HTTPException(status_code=404, detail="parent folder not found")
        if _rank(user.role) < _rank(parent.min_role):
            raise HTTPException(status_code=403, detail="cannot create a folder above your tier")
        try:
            if body.origin == "manual":
                fid = store.create_folder(parent_id=body.parent_id, name=body.name)
            else:
                # mount a synced folder / repo, then sync it
                if not body.location:
                    raise HTTPException(status_code=400, detail="location required for synced origin")
                if body.origin == "folder":
                    folder = Path(body.location).resolve()
                    roots = settings.source_root_list
                    if settings.auth_mode != "none" and not roots:
                        raise HTTPException(status_code=403,
                            detail="folder mounts disabled: no HIPPO_SOURCE_ROOTS configured")
                    if roots and not any(folder == r or r in folder.parents for r in roots):
                        raise HTTPException(status_code=403,
                            detail=f"{folder} is outside HIPPO_SOURCE_ROOTS")
                    if not folder.is_dir():
                        raise HTTPException(status_code=400, detail=f"not a directory: {folder}")
                    fid = store.create_folder(parent_id=body.parent_id, name=body.name,
                                              origin="folder", location=str(folder))
                    await run_in_threadpool(
                        sync_folder, folder, store, parent_id=body.parent_id,
                        max_chars=settings.chunk_max_chars,
                        overlap_chars=settings.chunk_overlap_chars, enricher=enricher,
                        max_doc_chars=settings.max_doc_chars)
                else:  # repo
                    fid = store.create_folder(parent_id=body.parent_id, name=body.name,
                                              origin="repo", location=body.location)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        f = store.get_folder(fid)
        return {"id": f.id, "name": f.name, "tier": f.min_role, "origin": f.origin}

    @app.patch("/folders/{folder_id}")
    async def patch_folder(folder_id: int, body: FolderPatch,
                           user: AuthenticatedUser = Depends(require_admin)):
        if store.get_folder(folder_id) is None:
            raise HTTPException(status_code=404, detail="folder not found")
        try:
            if body.name is not None:
                store.rename_folder(folder_id, body.name)
            if body.parent_id is not None:
                store.move_folder(folder_id, body.parent_id)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"id": folder_id}

    @app.delete("/folders/{folder_id}")
    async def delete_folder(folder_id: int, user: AuthenticatedUser = Depends(require_admin)):
        try:
            ok = store.delete_folder(folder_id)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        if not ok:
            raise HTTPException(status_code=404, detail="folder not found")
        return {"deleted": folder_id}

    @app.post("/folders/{folder_id}/resync")
    async def resync_folder(folder_id: int, user: AuthenticatedUser = Depends(require_admin)):
        f = store.get_folder(folder_id)
        if f is None:
            raise HTTPException(status_code=404, detail="folder not found")
        if f.origin != "folder" or not f.location:
            raise HTTPException(status_code=400, detail="only filesystem-synced folders resync")
        if not Path(f.location).is_dir():
            raise HTTPException(status_code=400,
                detail=f"folder path is not currently a directory: {f.location}")
        report = await run_in_threadpool(
            sync_folder, Path(f.location), store, parent_id=f.parent_id,
            max_chars=settings.chunk_max_chars,
            overlap_chars=settings.chunk_overlap_chars, enricher=enricher,
            max_doc_chars=settings.max_doc_chars)
        return {"report": {"added": report.added, "updated": report.updated,
                           "skipped": report.skipped, "removed": report.removed,
                           "failed": report.failed}}
```

Update the SPA `RESERVED` tuple: replace `"sources"` with `"folders"`.

- [ ] **Step 5: Run folder API tests**

Run: `uv run pytest tests/test_api_folders.py -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/hippo/api.py tests/test_api_folders.py
git commit -m "feat(api): /folders CRUD + require_owner, replacing /sources (SP1)"
```

---

### Task 6: Multi-destination `/ingest` + role-aware endpoints

Rework `/ingest` to take `folder_ids` (write-gated, manual-only); update `/me`, `/users`, `/settings/status` to the new roles.

**Files:**
- Modify: `src/hippo/api.py`
- Test: `tests/test_api.py`, `tests/test_api_settings.py`, `tests/test_api_auth.py`

- [ ] **Step 1: Write the failing tests**

```python
# in tests/test_api.py
def test_ingest_into_two_folders_creates_two_docs(tmp_path):
    from fastapi.testclient import TestClient
    from hippo.api import build_app
    from hippo.config import Settings
    s = Settings(_env_file=None, db_path=tmp_path / "t.db", embedding_model="fake",
                 embedding_dim=32, enrich_enabled=False)
    c = TestClient(build_app(s))
    rows = c.get("/folders").json()
    default_id = next(r["id"] for r in rows if r["name"] == "Default")
    sub = c.post("/folders", json={"parent_id": default_id, "name": "Retail"}).json()["id"]
    r = c.post("/ingest", files={"file": ("note.md", b"# Note\n\nhi", "text/markdown")},
               data={"folder_ids": [str(default_id), str(sub)]})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "added" and len(body["paths"]) == 2
    paths = {d["path"] for d in c.get("/documents").json()}
    assert "Default/note.md" in paths and "Default/Retail/note.md" in paths


def test_ingest_into_higher_tier_folder_is_forbidden(tmp_path):
    import time, jwt
    from cryptography.hazmat.primitives.asymmetric import ec
    from fastapi.testclient import TestClient
    from hippo.api import build_app
    from hippo.auth import IapVerifier
    from hippo.config import Settings
    AUD = "/projects/1/global/backendServices/2"
    s = Settings(_env_file=None, db_path=tmp_path / "t.db", embedding_model="fake",
                 embedding_dim=32, enrich_enabled=False, auth_mode="iap", iap_audience=AUD)
    key = ec.generate_private_key(ec.SECP256R1())
    app = build_app(s, iap_verifier=IapVerifier(AUD, key_fetcher=lambda: {"k1": key.public_key()}))
    c = TestClient(app)
    tok = jwt.encode({"aud": AUD, "iss": "https://cloud.google.com/iap",
                      "exp": int(time.time()) + 600, "email": "dev@x.com"},
                     key, algorithm="ES256", headers={"kid": "k1"})
    h = {"x-goog-iap-jwt-assertion": tok}
    owner_id = next(r["id"] for r in c.get("/folders", headers=h).json() if r["name"] == "Owner")
    # a user cannot even see the Owner folder, so the upload is rejected (403)
    r = c.post("/ingest", files={"file": ("x.md", b"# X\n\nhi", "text/markdown")},
               data={"folder_ids": [str(owner_id)]}, headers=h)
    assert r.status_code == 403
```

In `tests/test_api_auth.py`, update the `test_iap_mode_rejects_without_assertion` `/me` expectation: `"role": "developer"` → `"role": "user"`, and `test_none_mode_is_implicit_admin` → role `"owner"` (rename the test to `..._is_implicit_owner`). In `tests/test_api_settings.py`, update any `role` assertions to the new names and counts keys (`sources` count becomes `folders`).

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_api.py -q -k ingest`
Expected: FAIL (`/ingest` still takes `repo`, returns `path` not `paths`).

- [ ] **Step 3: Rewrite `/ingest`**

Replace the `/ingest` route with a multi-destination version (drop the GitHub `repo` branch — repo upload now happens via mounting a `repo`-origin folder, Task 5; per-file upload is folder-scoped):

```python
    @app.post("/ingest")
    async def ingest(request: Request, file: UploadFile,
                     folder_ids: list[int] = Form(...),
                     user: AuthenticatedUser = Depends(verify_request)):
        from .roles import can_write
        cl = request.headers.get("content-length")
        if cl and cl.isdigit() and int(cl) > settings.max_upload_bytes:
            raise HTTPException(status_code=413, detail="file too large")
        raw_bytes = await file.read()
        if len(raw_bytes) > settings.max_upload_bytes:
            raise HTTPException(status_code=413, detail="file too large")
        name = _safe_filename(file.filename or "upload.md")
        suffix = Path(name).suffix or ".md"
        targets = []
        for fid in folder_ids:
            f = store.get_folder(fid)
            if f is None:
                raise HTTPException(status_code=404, detail=f"folder {fid} not found")
            if not can_write(user.role, f.min_role, f.origin):
                raise HTTPException(status_code=403,
                    detail=f"cannot upload into {f.name!r} (tier or synced-folder lock)")
            targets.append(f)
        results = []
        for f in targets:
            prefix = store.folder_path(f.id)
            res = await run_in_threadpool(
                ingestor.ingest_bytes, name, raw_bytes,
                folder_id=f.id, path_prefix=prefix, suffix=suffix)
            if res.status == "failed":
                raise HTTPException(status_code=422, detail=res.error)
            results.append({"path": res.path, "chunks": res.chunks})
        # one document per destination folder
        return {"status": "added", "paths": [r["path"] for r in results],
                "results": results, "versioned": False}
```

> Note: `folder_ids` as a repeated form field arrives as `list[int]` via FastAPI's `Form(...)`. The TestClient sends `data={"folder_ids": [..]}`.

- [ ] **Step 4: Update `/me`, `/users`, `/settings/status`**

`/me` — drop the GitHub-repo `upload` block (repo upload is now a folder mount); return identity only:

```python
    @app.get("/me")
    async def me(user: AuthenticatedUser = Depends(verify_request)):
        return {"email": user.email, "role": user.role, "auth_mode": settings.auth_mode}
```

`/users` — bootstrap emails now resolve to `owner`; valid roles updated; reflect effective role:

```python
    @app.get("/users")
    async def list_users(user: AuthenticatedUser = Depends(require_admin)):
        admins = settings.admin_email_list
        return [{"email": e, "role": "owner" if e in admins else r}
                for e, r in store.list_users()]
```

`PUT /users/{email}/role` — new valid roles + tiered authority (admins can grant up to `admin`; only owners grant `owner`); keep anti-self-demotion and bootstrap guard:

```python
    @app.put("/users/{email}/role")
    async def set_user_role(email: str, body: RoleIn,
                            user: AuthenticatedUser = Depends(require_admin)):
        from .roles import VALID_ROLES, rank as _rank
        target = email.strip().lower()
        if body.role not in VALID_ROLES:
            raise HTTPException(status_code=400,
                detail=f"invalid role {body.role!r}; expected one of {list(VALID_ROLES)}")
        if _rank(body.role) > _rank(user.role):
            raise HTTPException(status_code=403,
                detail="you cannot grant a role above your own")
        if target == user.email and _rank(body.role) < _rank(user.role):
            raise HTTPException(status_code=400, detail="you can't lower your own role")
        if target in settings.admin_email_list and body.role != "owner":
            raise HTTPException(status_code=400,
                detail="this user is a bootstrap admin (HIPPO_ADMIN_EMAILS); "
                       "remove them from that env var to change their role")
        store.set_role(target, body.role)
        return {"email": target, "role": body.role}
```

`/settings/status` — counts use `role="owner"`; `sources` → `folders`:

```python
            "counts": {
                "documents": len(store.list_documents(role="owner")),
                "folders": len(store.list_folders(role="owner")),
                "users": len(store.list_users()),
            },
```

(Drop the `repos` block only if it references the removed `/me.upload`; keep `repos` from `settings` as-is — it reads `settings.github_*`, unaffected.)

- [ ] **Step 5: Run the API suite**

Run: `uv run pytest tests/test_api.py tests/test_api_auth.py tests/test_api_settings.py -q`
Expected: PASS (sweep these files for `developer`/`manager`/`admin`-as-top and `upload` assertions; update per the mapping table).

- [ ] **Step 6: Full Python suite green**

Run: `uv run pytest -q`
Expected: PASS (zero network). Fix any remaining stragglers (grep the tests dir for `"developer"`, `"manager"`, `source_id`, `register_source`, `list_sources`, `/sources`).

- [ ] **Step 7: Commit**

```bash
git add src/hippo/api.py tests/
git commit -m "feat(api): multi-destination /ingest + role-aware endpoints (SP1)"
```

---

### Task 7: UI pure helpers + Vitest

The testable logic (tree flattening, writable-folder filtering, upload-state reducer) lives apart from React so Vitest covers it.

**Files:**
- Create: `ui/src/folders.ts`
- Test: `ui/src/folders.test.ts`

- [ ] **Step 1: Write the failing tests**

```typescript
// ui/src/folders.test.ts
import { describe, expect, it } from "vitest";
import {
  type Folder,
  flattenTree,
  writableFolders,
  uploadReducer,
  type UploadState,
} from "./folders";

const TREE: Folder[] = [
  { id: 1, parent_id: null, name: "Default", tier: "user", origin: "manual", doc_count: 0, writable: true },
  { id: 2, parent_id: 1, name: "Retail", tier: "user", origin: "manual", doc_count: 2, writable: true },
  { id: 3, parent_id: null, name: "Private", tier: "admin", origin: "manual", doc_count: 0, writable: true },
  { id: 4, parent_id: 1, name: "Mirror", tier: "user", origin: "folder", doc_count: 5, writable: false },
];

describe("flattenTree", () => {
  it("orders children under parents with depth", () => {
    const flat = flattenTree(TREE);
    expect(flat.map((f) => [f.id, f.depth])).toEqual([
      [1, 0], [2, 1], [4, 1], [3, 0],
    ]);
  });
});

describe("writableFolders", () => {
  it("keeps only manual + writable folders", () => {
    expect(writableFolders(TREE).map((f) => f.id)).toEqual([1, 2, 3]);
  });
});

describe("uploadReducer", () => {
  const init: UploadState = { status: "idle", file: null, dests: [], done: 0, error: null };
  it("walks idle → uploading → done", () => {
    let s = uploadReducer(init, { type: "start", file: { name: "a.md" } as File, dests: [1, 2] });
    expect(s.status).toBe("uploading");
    s = uploadReducer(s, { type: "progress" });
    expect(s.done).toBe(1);
    s = uploadReducer(s, { type: "progress" });
    expect(s).toMatchObject({ status: "done", done: 2 });
  });
  it("captures failure", () => {
    let s = uploadReducer(init, { type: "start", file: { name: "a.md" } as File, dests: [1] });
    s = uploadReducer(s, { type: "error", error: "too large" });
    expect(s).toMatchObject({ status: "error", error: "too large" });
  });
});
```

- [ ] **Step 2: Run to verify failure**

Run: `cd ui && npx vitest run src/folders.test.ts`
Expected: FAIL — cannot find `./folders`.

- [ ] **Step 3: Implement `ui/src/folders.ts`**

```typescript
export type Tier = "user" | "admin" | "owner";

export type Folder = {
  id: number;
  parent_id: number | null;
  name: string;
  tier: Tier;
  origin: "manual" | "folder" | "repo";
  doc_count: number;
  writable: boolean;
};

export type FlatFolder = Folder & { depth: number };

/** Depth-first flatten: each parent immediately followed by its children, roots
 *  in input order. Children inherit depth = parent.depth + 1. */
export function flattenTree(folders: Folder[]): FlatFolder[] {
  const byParent = new Map<number | null, Folder[]>();
  for (const f of folders) {
    const key = f.parent_id;
    (byParent.get(key) ?? byParent.set(key, []).get(key)!).push(f);
  }
  const out: FlatFolder[] = [];
  const walk = (parent: number | null, depth: number) => {
    for (const f of byParent.get(parent) ?? []) {
      out.push({ ...f, depth });
      walk(f.id, depth + 1);
    }
  };
  walk(null, 0);
  return out;
}

/** Folders a caller may upload into: server already set `writable`
 *  (rank ≥ tier ∧ manual); this is the picker's source list. */
export function writableFolders(folders: Folder[]): Folder[] {
  return folders.filter((f) => f.writable);
}

export type UploadState = {
  status: "idle" | "uploading" | "done" | "error";
  file: File | null;
  dests: number[];
  done: number;
  error: string | null;
};

export type UploadAction =
  | { type: "start"; file: File; dests: number[] }
  | { type: "progress" }
  | { type: "error"; error: string }
  | { type: "reset" };

/** Drives the upload modal: one `progress` per finished destination; flips to
 *  `done` when every destination is uploaded. */
export function uploadReducer(state: UploadState, action: UploadAction): UploadState {
  switch (action.type) {
    case "start":
      return { status: "uploading", file: action.file, dests: action.dests, done: 0, error: null };
    case "progress": {
      const done = state.done + 1;
      return { ...state, done, status: done >= state.dests.length ? "done" : "uploading" };
    }
    case "error":
      return { ...state, status: "error", error: action.error };
    case "reset":
      return { status: "idle", file: null, dests: [], done: 0, error: null };
  }
}
```

- [ ] **Step 4: Run Vitest**

Run: `cd ui && npx vitest run src/folders.test.ts`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add ui/src/folders.ts ui/src/folders.test.ts
git commit -m "feat(ui): pure folder-tree + upload-state helpers with vitest (SP1)"
```

---

### Task 8: UI — Folders tab + upload modal

Wire the helpers into the Settings "Folders" tab (tree CRUD, admin+) and a role-scoped upload modal in `App.tsx`.

**Files:**
- Modify: `ui/src/Settings.tsx`, `ui/src/App.tsx`

- [ ] **Step 1: Retype roles + rename the Sources tab**

In `ui/src/Settings.tsx`:
- `type Role = "user" | "admin" | "owner";`
- `tabsForRole`: admin+ (`role !== "user"`) sees `["Folders", "Users", "Tokens", "Status"]`; a `user` sees `["Tokens"]`:

```typescript
export function tabsForRole(role: string): string[] {
  return role === "user" ? ["Tokens"] : ["Folders", "Users", "Tokens", "Status"];
}
```

- Replace `<TokensPanel admin={role === "admin"} />` gating with `admin={role !== "user"}`.
- Replace the `Sources` tab + `SourcesPanel` with a `FoldersPanel` rendering the flattened tree, create-subfolder, rename, delete, and re-sync controls. Render tier + a "synced" badge for non-manual origins; only show create/upload affordances where `writable`.

```tsx
import { flattenTree, type Folder } from "./folders";

function FoldersPanel() {
  const [rows, setRows] = useState<Folder[]>([]);
  const [note, setNote] = useState("");
  const [name, setName] = useState("");
  const [parent, setParent] = useState<number | "">("");
  const load = useCallback(() => {
    getJSON("/folders").then(setRows).catch(() => setRows([]));
  }, []);
  useEffect(() => { load(); }, [load]);
  const create = async () => {
    if (parent === "" || !name.trim()) return;
    const r = await fetch("/folders", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ parent_id: parent, name }) });
    if (r.ok) { setName(""); load(); setNote(""); }
    else setNote(await r.json().then((b) => b.detail).catch(() => `error ${r.status}`));
  };
  const rename = async (id: number, current: string) => {
    const next = window.prompt("New name", current);
    if (!next) return;
    const r = await fetch(`/folders/${id}`, { method: "PATCH",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: next }) });
    setNote(r.ok ? "" : `error ${r.status}`); load();
  };
  const del = async (id: number) => {
    const r = await fetch(`/folders/${id}`, { method: "DELETE" });
    setNote(r.ok ? "" : await r.json().then((b) => b.detail).catch(() => `error ${r.status}`));
    load();
  };
  const resync = async (id: number) => {
    setNote("syncing…");
    const r = await fetch(`/folders/${id}/resync`, { method: "POST" });
    setNote(r.ok ? "synced" : `error ${r.status}`); load();
  };
  const flat = flattenTree(rows);
  return (
    <div className="panel">
      <div className="row">
        <select value={parent} onChange={(e) => setParent(e.target.value ? Number(e.target.value) : "")}>
          <option value="">parent folder…</option>
          {flat.filter((f) => f.writable).map((f) => (
            <option key={f.id} value={f.id}>{" ".repeat(f.depth * 2) + f.name}</option>
          ))}
        </select>
        <input placeholder="new subfolder name" value={name} onChange={(e) => setName(e.target.value)} />
        <button onClick={create}>Create</button>
        <span className="note">{note}</span>
      </div>
      <table><tbody>
        {flat.map((f) => (
          <tr key={f.id}>
            <td style={{ paddingLeft: f.depth * 16 }}>
              {f.name} <span className="sec">{f.tier}</span>
              {f.origin !== "manual" && <span className="sec"> · synced ({f.origin})</span>}
            </td>
            <td>{f.doc_count} docs</td>
            <td>
              {f.parent_id !== null && <button onClick={() => rename(f.id, f.name)}>Rename</button>}
              {f.origin === "folder" && <button onClick={() => resync(f.id)}>Re-sync</button>}
              {f.parent_id !== null && <button onClick={() => del(f.id)}>Delete</button>}
            </td>
          </tr>
        ))}
      </tbody></table>
    </div>
  );
}
```

- Update `UsersPanel`'s role `<select>` options from `["developer","manager","admin"]` to `["user","admin","owner"]`.
- In the `Settings` component body, replace `{tab === "Sources" && <SourcesPanel />}` with `{tab === "Folders" && <FoldersPanel />}`.

- [ ] **Step 2: Replace the App header upload control with a role-scoped modal**

In `ui/src/App.tsx`:
- Drop the `Me.upload` field and `uploadRepo` state + the repo `<select>`.
- Fetch `/folders` for the writable picker; replace the bare "Add doc" `<label>` with a modal driven by `uploadReducer`.

```tsx
import { flattenTree, writableFolders, uploadReducer, type Folder } from "./folders";
```

Replace the `Me` type:

```tsx
type Me = { email: string; role: string; auth_mode: string };
```

Add folder state + a modal. The upload posts `folder_ids` (repeated form field) and ticks the reducer once per destination:

```tsx
  const [folders, setFolders] = useState<Folder[]>([]);
  const [showUpload, setShowUpload] = useState(false);
  const [up, dispatchUp] = useReducer(uploadReducer,
    { status: "idle", file: null, dests: [], done: 0, error: null });
  const [pickFile, setPickFile] = useState<File | null>(null);
  const [picked, setPicked] = useState<number[]>([]);

  const refreshFolders = useCallback(() => {
    fetch("/folders").then((r) => r.json()).then(setFolders).catch(() => {});
  }, []);
  useEffect(() => { refreshFolders(); }, [refreshFolders]);

  async function runUpload() {
    if (!pickFile || picked.length === 0) return;
    dispatchUp({ type: "start", file: pickFile, dests: picked });
    const form = new FormData();
    form.append("file", pickFile);
    for (const id of picked) form.append("folder_ids", String(id));
    const res = await fetch("/ingest", { method: "POST", body: form });
    if (!res.ok) {
      const b = await res.json().catch(() => ({ detail: `error ${res.status}` }));
      dispatchUp({ type: "error", error: b.detail });
      return;
    }
    // server ingests into every destination; advance the bar to done
    for (let i = 0; i < picked.length; i++) dispatchUp({ type: "progress" });
    refreshDocs();
  }
```

Render the modal (button switches to "Done" when `up.status === "done"`), gated to logged-in users; the destination list is `writableFolders(folders)` flattened with indentation. Keep the existing `header`/chat structure; just swap the upload control. The "Add doc" button opens the modal:

```tsx
        <button className="upload-btn" onClick={() => { setShowUpload(true); dispatchUp({ type: "reset" }); }}>
          Add doc
        </button>
```

```tsx
      {showUpload && (
        <div className="modal-backdrop" onClick={() => setShowUpload(false)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <h3>Add a document</h3>
            <input type="file" accept=".md,.markdown,.txt,.html,.htm,.docx"
              onChange={(e) => setPickFile(e.target.files?.[0] ?? null)} />
            <p>Destination folders</p>
            <div className="dest-list">
              {flattenTree(writableFolders(folders)).map((f) => (
                <label key={f.id} style={{ paddingLeft: f.depth * 12 }}>
                  <input type="checkbox" checked={picked.includes(f.id)}
                    onChange={(e) => setPicked((p) =>
                      e.target.checked ? [...p, f.id] : p.filter((x) => x !== f.id))} />
                  {f.name} <span className="sec">{f.tier}</span>
                </label>
              ))}
            </div>
            {up.status === "uploading" && <p>Uploading… {up.done}/{up.dests.length}</p>}
            {up.status === "error" && <p className="error">{up.error}</p>}
            {up.status === "done"
              ? <button onClick={() => setShowUpload(false)}>Done</button>
              : <button disabled={!pickFile || picked.length === 0 || up.status === "uploading"}
                  onClick={runUpload}>Upload</button>}
          </div>
        </div>
      )}
```

> `flattenTree(writableFolders(folders))` keeps indentation only for writable folders; a `user` sees just Default-tier manual folders, an `admin` sees Default+Private, etc. — the server's `writable` flag already enforces this.

Update the `<Settings role={...} />` cast to `"user" | "admin" | "owner"`. Add `useReducer` to the React import.

- [ ] **Step 3: Build the UI**

Run: `cd ui && npm run build`
Expected: build succeeds (TypeScript clean). Add minimal CSS for `.modal-backdrop`/`.modal`/`.dest-list` in `ui/src/index.css` (reuse existing tokens) if needed for layout — not required for the build to pass.

- [ ] **Step 4: Run Vitest (regression)**

Run: `cd ui && npm test`
Expected: PASS (folders + citations suites).

- [ ] **Step 5: Commit**

```bash
git add ui/src/App.tsx ui/src/Settings.tsx ui/src/index.css
git commit -m "feat(ui): Folders tree tab + role-scoped upload modal (SP1)"
```

---

### Task 9: Docs, eval, config drift

Bring the prose, config example, and eval harness in line with the folder model and new roles.

**Files:**
- Modify: `README.md`, `CLAUDE.md`, `.env.example`, `tests/test_env_example.py`, `tests/test_config.py`, `eval/golden.yaml` (only if paths changed)

- [ ] **Step 1: Verify eval still passes on a fresh DB**

The eval fixtures sync into the Default root (Task 4 CLI change), keeping their source-relative paths, so `expect_path` values in `eval/golden.yaml` are unchanged. Confirm:

Run:
```bash
rm -f /tmp/eval.db
HIPPO_DB_PATH=/tmp/eval.db HIPPO_EMBEDDING_MODEL=fake HIPPO_EMBEDDING_DIM=32 uv run hippo sync eval/fixtures
HIPPO_DB_PATH=/tmp/eval.db HIPPO_EMBEDDING_MODEL=fake HIPPO_EMBEDDING_DIM=32 uv run hippo eval eval/golden.yaml
```
Expected: `recall@5: 4/4` (FakeEmbedder is deterministic; if a fixture path moved, fix `expect_path`).

- [ ] **Step 2: Update `test_config.py` if it pins role/auth literals**

If `tests/test_config.py` asserts the `auth_mode` Literal or any role default, leave `auth_mode` as-is (unchanged in SP1) — no edit needed unless a test references the removed `sources.access`. Run it:

Run: `uv run pytest tests/test_config.py -q`
Expected: PASS.

- [ ] **Step 3: Update docs prose**

- `README.md`: replace any "sources"/"developer/manager/admin"/`access=everyone|managers` language with the folder-tree + `user/admin/owner` model; document the `/folders` flow and the upload modal. Note the legacy-DB recreate requirement.
- `CLAUDE.md`: update the `storage.py`/`db.py`/`api.py` architecture lines (sources → folders, source_id → folder_id, roles, `/folders`, surrogate users/tokens) and the "Hard rules" line about role filtering (now rank-based via folders). Update the State block to record SP1.

- [ ] **Step 4: `.env.example` drift guard stays green**

SP1 adds no new `HIPPO_` settings (root-folder names are SP3). Confirm the drift guard:

Run: `uv run pytest tests/test_env_example.py -q`
Expected: PASS (no Settings fields added/removed). If it fails, reconcile `.env.example` with `Settings.model_fields`.

- [ ] **Step 5: Full suite + build, then commit**

Run:
```bash
uv run pytest -q
cd ui && npm test && npm run build
```
Expected: all green.

```bash
git add README.md CLAUDE.md .env.example tests/test_env_example.py tests/test_config.py eval/golden.yaml
git commit -m "docs: folder model + role rename across README/CLAUDE/.env (SP1)"
```

---

## Self-Review

**1. Spec coverage** (against `2026-06-13-roles-and-collections-design.md`):
- §3 Roles (user/admin/owner ranks, owner bootstrap, require_admin/require_owner) → Tasks 1, 3, 5.
- §4 Data model (folders tree, denormalized min_role, documents.folder_id, surrogate user_id, tokens.user_id, origin manual/folder/repo) → Task 2.
- §5 Access enforcement (keyword-only `role` no default — preserved; rank read filter; write gate manual+rank; SQL only in storage.py) → Tasks 1, 2, 5, 6.
- §6 API (/folders GET/POST/PATCH/DELETE/resync; /ingest folder_ids) → Tasks 5, 6.
- §7 UI (Folders tree tab, role-scoped multi-destination upload modal with progress→Done, pure helpers) → Tasks 7, 8.
- §8 Fresh schema, seed three roots, legacy-DB clear error, folder-qualified upload paths, fixtures keep paths → Tasks 2, 4, 9.
- §9 Testing (rank filtering, write gating, tree ops, schema/fresh start, Vitest, eval) → Tasks 1, 2, 5, 6, 7, 9.

**2. Invariants preserved:** retrieval methods keep keyword-only `role` with no default (Task 2 signatures unchanged on that axis); all SQL stays in storage.py; tool output framing untouched; tests zero-network (FakeEmbedder/TestModel). ✓

**3. Type consistency:** `folder_id` (not `source_id`) everywhere; `Folder` dataclass fields match the `/folders` JSON keys via explicit mapping (`min_role`→`tier`); `readable_min_roles`/`can_read`/`can_write` names consistent across roles.py, storage.py, api.py; UI `Folder.tier` matches the API's `tier` key. ✓

**Known coupling:** Task 2 is large (schema + storage + data-layer tests) because the role rename and folder model cannot be split without leaving the suite red between commits; it is sequenced to return to green at its boundary.
