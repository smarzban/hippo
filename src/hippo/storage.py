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


class Storage:
    """All database access. The agent and ingestion never touch SQL directly."""

    def __init__(self, con: sqlite3.Connection, embedder: Embedder):
        self.con = con
        self.embedder = embedder
        # One shared connection is used from the event loop AND run_in_threadpool
        # workers (agent tools + ingest). sqlite3's per-statement mutex does NOT
        # prevent interleaved statement-stepping across threads, which raises
        # InterfaceError. Serialize every DB critical section through this lock.
        # Network embedding is always done OUTSIDE the lock so a slow ingest can't
        # block concurrent reads. (Team scale: swap for per-worker/pooled conns.)
        self._lock = threading.Lock()

    # -- documents ---------------------------------------------------------

    def _ensure_embedding_model(self) -> None:
        """Record the embedding model on first write; refuse to mix embedding
        spaces. A same-dimension model swap followed by `sync` (not `reindex`)
        would otherwise silently blend two incompatible vector spaces — a
        different dimension already fails loudly in sqlite-vec, the same dim does
        not. The stamp is global (one row), not per-vector; `reindex` re-stamps it."""
        with self._lock:
            row = self.con.execute(
                "SELECT value FROM meta WHERE key='embedding_model'"
            ).fetchone()
            if row is None:
                with self.con:
                    self.con.execute(
                        "INSERT INTO meta(key, value) VALUES ('embedding_model', ?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (self.embedder.model,),
                    )
            elif row[0] != self.embedder.model:
                raise ValueError(
                    f"database was indexed with embedding model {row[0]!r} but the "
                    f"configured model is {self.embedder.model!r}; run `hippo reindex` "
                    f"to re-embed, or set HIPPO_EMBEDDING_MODEL={row[0]}"
                )

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

    def _delete_chunks(self, doc_id: int) -> None:
        ids = [r[0] for r in self.con.execute("SELECT id FROM chunks WHERE document_id=?", (doc_id,))]
        if ids:
            ph = ",".join("?" * len(ids))
            self.con.execute(f"DELETE FROM chunk_vec WHERE rowid IN ({ph})", ids)
            self.con.execute(f"DELETE FROM chunks WHERE id IN ({ph})", ids)

    def delete_document_by_path(self, path: str) -> bool:
        with self._lock:
            row = self.con.execute("SELECT id FROM documents WHERE path=?", (path,)).fetchone()
            if not row:
                return False
            with self.con:
                self._delete_chunks(row[0])
                self.con.execute("DELETE FROM documents WHERE id=?", (row[0],))
            return True

    def document_exists(self, path: str) -> bool:
        with self._lock:
            return self.con.execute(
                "SELECT 1 FROM documents WHERE path=?", (path,)
            ).fetchone() is not None

    def is_unchanged(self, path: str, content_hash: str) -> bool:
        with self._lock:
            row = self.con.execute("SELECT content_hash FROM documents WHERE path=?", (path,)).fetchone()
        return bool(row and row[0] == content_hash)

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
                # Either the (parent_id, name) sibling-uniqueness or the non-null
                # location-uniqueness index fired.
                if location is not None and self.con.execute(
                    "SELECT 1 FROM folders WHERE location=?", (location,)).fetchone():
                    raise ValueError(f"location {location!r} is already mounted") from e
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

    def get_profile(self, email: str) -> dict | None:
        """{email, name, role} for a user, or None. Used by /me and PATCH /me."""
        email = _norm_email(email)
        with self._lock:
            row = self.con.execute(
                "SELECT email, name, role FROM users WHERE email=?", (email,)).fetchone()
        return {"email": row[0], "name": row[1], "role": row[2]} if row else None

    def set_name(self, email: str, name: str) -> None:
        """Update a user's display name. No-op if the user does not exist."""
        email = _norm_email(email)
        with self._lock, self.con:
            self.con.execute("UPDATE users SET name=? WHERE email=?", (name, email))

    LOCKOUT_MAX_FAILURES = 5
    LOCKOUT_MINUTES = 15

    def set_password(self, email: str, password_hash: str, *, role: str | None = None) -> None:
        """Create-or-update a local credential. Creates the user (with `role` or
        the default) if absent; on an existing user updates the hash and (only if
        `role` is given) the role. Clears any lockout state. The caller hashes."""
        email = _norm_email(email)
        if role is not None and role not in VALID_ROLES:
            raise ValueError(f"invalid role {role!r}; expected one of {VALID_ROLES}")
        with self._lock, self.con:
            row = self.con.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
            if row is None:
                self.con.execute(
                    "INSERT INTO users(email, role, password_hash) VALUES (?,?,?)",
                    (email, role or DEFAULT_ROLE, password_hash),
                )
            elif role is not None:
                self.con.execute(
                    "UPDATE users SET password_hash=?, role=?, failed_logins=0, "
                    "locked_until=NULL WHERE id=?",
                    (password_hash, role, row[0]),
                )
            else:
                self.con.execute(
                    "UPDATE users SET password_hash=?, failed_logins=0, "
                    "locked_until=NULL WHERE id=?",
                    (password_hash, row[0]),
                )

    def get_credentials(self, email: str) -> dict | None:
        """Return {user_id, email, role, password_hash, failed_logins, locked_until}
        for an email, or None if no such user. Used only by the login path."""
        email = _norm_email(email)
        with self._lock:
            row = self.con.execute(
                "SELECT id, email, role, password_hash, failed_logins, locked_until "
                "FROM users WHERE email=?", (email,),
            ).fetchone()
        if row is None:
            return None
        return {"user_id": row[0], "email": row[1], "role": row[2],
                "password_hash": row[3], "failed_logins": row[4], "locked_until": row[5]}

    def get_user_by_id(self, user_id: int) -> tuple[str, str] | None:
        """(email, role) for a surrogate id, or None. Used by the session auth path."""
        with self._lock:
            row = self.con.execute(
                "SELECT email, role FROM users WHERE id=?", (user_id,)).fetchone()
        return (row[0], row[1]) if row else None

    def record_failed_login(self, email: str) -> None:
        """Increment the failure counter; lock for LOCKOUT_MINUTES once it reaches
        LOCKOUT_MAX_FAILURES. Lock timestamp is DB-clock based for testability."""
        email = _norm_email(email)
        with self._lock, self.con:
            self.con.execute(
                "UPDATE users SET failed_logins = failed_logins + 1 WHERE email=?", (email,))
            self.con.execute(
                f"UPDATE users SET locked_until = datetime('now', '+{self.LOCKOUT_MINUTES} minutes') "
                "WHERE email=? AND failed_logins >= ?",
                (email, self.LOCKOUT_MAX_FAILURES),
            )

    def reset_login_state(self, email: str) -> None:
        """Clear the failure counter + lock (called on a successful login)."""
        email = _norm_email(email)
        with self._lock, self.con:
            self.con.execute(
                "UPDATE users SET failed_logins=0, locked_until=NULL WHERE email=?", (email,))

    def is_locked(self, email: str) -> bool:
        """True iff the account is currently within its lockout window."""
        email = _norm_email(email)
        with self._lock:
            row = self.con.execute(
                "SELECT locked_until > datetime('now') FROM users WHERE email=?", (email,)
            ).fetchone()
        return bool(row and row[0])

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

    # -- config store (SP3) --------------------------------------------------

    SETUP_COMPLETE_KEY = "setup_complete"

    def get_config(self, key: str) -> str | None:
        with self._lock:
            row = self.con.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_config(self, key: str, value: str) -> None:
        with self._lock, self.con:
            self.con.execute(
                "INSERT INTO config(key, value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def all_config(self) -> dict[str, str]:
        with self._lock:
            return {k: v for k, v in self.con.execute("SELECT key, value FROM config")}

    def is_setup_complete(self) -> bool:
        return self.get_config(self.SETUP_COMPLETE_KEY) == "1"

    def mark_setup_complete(self) -> None:
        self.set_config(self.SETUP_COMPLETE_KEY, "1")

    def claim_setup(self) -> bool:
        """Atomically claim the first-run setup. Sets setup_complete='1' ONLY if
        it is not already set, returning True iff THIS call did the claiming.
        Concurrent /setup requests that race past is_setup_complete() converge
        here so exactly one creates the owner; the loser gets a 409."""
        with self._lock, self.con:
            cur = self.con.execute(
                "INSERT INTO config(key, value) VALUES (?, '1') "
                "ON CONFLICT(key) DO NOTHING",
                (self.SETUP_COMPLETE_KEY,),
            )
            return cur.rowcount > 0

    def document_count(self) -> int:
        with self._lock:
            return self.con.execute("SELECT count(*) FROM documents").fetchone()[0]

    # -- search --------------------------------------------------------------

    RRF_K = 60

    def search_hybrid(self, query: str, top_k: int = 8, *, role: str) -> list[SearchHit]:
        """FTS5 BM25 + vector KNN, merged with Reciprocal Rank Fusion."""
        if not query.strip():
            return []
        qvec = self.embedder.embed([query])[0]  # network: outside the lock
        with self._lock:
            fts_ranked = self._search_fts(query, limit=top_k * 3, role=role)
            vec_ranked = self._search_vec(qvec, limit=top_k * 3, role=role)
            scores: dict[int, float] = {}
            for ranked in (fts_ranked, vec_ranked):
                for rank, chunk_id in enumerate(ranked):
                    scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (self.RRF_K + rank + 1)
            hits: list[SearchHit] = []
            for cid in sorted(scores, key=scores.__getitem__, reverse=True):
                # _hit is now redundant defense-in-depth; candidates are already role-filtered
                hit = self._hit(cid, scores[cid], role)
                if hit is not None:
                    hits.append(hit)
                if len(hits) >= top_k:
                    break
        return hits

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

    def reindex(self, embedding_dim: int, *, batch: int = 64) -> int:
        """Re-embed every chunk with the current embedder and rebuild chunk_vec.

        Embeds everything BEFORE destroying the old index, so a mid-run failure
        (bad key, rate limit, wrong dimension) leaves the existing vectors intact.
        The destroy+repopulate+stamp happens in a single transaction only after
        all embeddings succeed. Returns the number of chunks re-embedded."""
        with self._lock:
            rows = self.con.execute("SELECT id, text FROM chunks ORDER BY id").fetchall()
        new_vectors: list[tuple[int, bytes]] = []
        for i in range(0, len(rows), batch):
            part = rows[i : i + batch]
            vecs = self.embedder.embed([t for _, t in part])  # network: outside the lock
            for (cid, _), v in zip(part, vecs):
                if len(v) != embedding_dim:
                    raise ValueError(
                        f"embedding model {self.embedder.model!r} returned dimension "
                        f"{len(v)}, expected {embedding_dim}; check HIPPO_EMBEDDING_DIM"
                    )
                new_vectors.append((cid, sqlite_vec.serialize_float32(v)))
        with self._lock, self.con:  # atomic swap, only reached if all embeds succeeded
            self.con.execute("DROP TABLE IF EXISTS chunk_vec")
            self.con.execute(
                f"CREATE VIRTUAL TABLE chunk_vec USING vec0(embedding float[{int(embedding_dim)}])"
            )
            self.con.executemany(
                "INSERT INTO chunk_vec(rowid, embedding) VALUES (?,?)", new_vectors
            )
            self.con.execute(
                "INSERT INTO meta(key, value) VALUES ('embedding_model', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (self.embedder.model,),
            )
        return len(rows)

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

    def backup(self, dest: str | Path) -> None:
        """Write a consistent single-file snapshot to dest via VACUUM INTO.
        Works regardless of WAL state; dest must not already exist."""
        with self._lock:
            self.con.execute("VACUUM INTO ?", (str(dest),))
