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


VALID_ROLES = ("developer", "manager", "admin")
MANAGER_ROLES = ("manager", "admin")

GREP_MAX_PATTERN = 200      # reject absurdly long patterns
GREP_TIMEOUT_S = 2.0        # wall-clock cap per chunk scan (regex module)


def _norm_email(e: str) -> str:
    return e.strip().lower()


def _visible(role: str, access: str | None) -> bool:
    """Source-level access check. access=None (uploads / no source) = everyone."""
    return role in MANAGER_ROLES or access != "managers"


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
        summary: str | None = None,
        source_id: int | None = None,
    ) -> int:
        """Insert or replace a document and all its chunks atomically."""
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
                       summary=?, source_id=?, synced_at=datetime('now') WHERE id=?""",
                    (source_type, title, content, content_hash, summary, source_id, doc_id),
                )
            else:
                cur = self.con.execute(
                    """INSERT INTO documents(source_type, path, title, content, content_hash, summary, source_id)
                       VALUES (?,?,?,?,?,?,?)""",
                    (source_type, path, title, content, content_hash, summary, source_id),
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
        with self._lock:
            row = self.con.execute(
                """SELECT d.id, d.source_type, d.path, d.title, d.content, d.content_hash,
                          d.summary, s.access
                   FROM documents d LEFT JOIN sources s ON s.id = d.source_id WHERE d.id=?""",
                (doc_id,),
            ).fetchone()
        if row is None or not _visible(role, row[7]):
            return None
        return Document(*row[:7])

    def list_documents(self, query: str | None = None, *, role: str) -> list[Document]:
        sql = ("SELECT d.id, d.source_type, d.path, d.title, d.content, d.content_hash, d.summary "
               "FROM documents d LEFT JOIN sources s ON s.id = d.source_id")
        where: list[str] = []
        args: list = []
        if role not in MANAGER_ROLES:
            where.append("(s.access IS NULL OR s.access != 'managers')")
        if query:
            where.append("(d.title LIKE ? OR d.path LIKE ? OR coalesce(d.summary,'') LIKE ?)")
            like = f"%{query}%"
            args += [like, like, like]
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY d.path"
        with self._lock:
            return [Document(*r) for r in self.con.execute(sql, args)]

    def paths_for_source(self, source_id: int) -> set[str]:
        with self._lock:
            return {r[0] for r in self.con.execute("SELECT path FROM documents WHERE source_id=?", (source_id,))}

    # -- sources -----------------------------------------------------------

    def register_source(self, kind: str, location: str, access: str | None = None) -> int:
        """Register (or re-register) a source. access=None preserves the existing
        level — a plain re-sync must never demote a managers source — and defaults
        new sources to 'everyone'."""
        if access is not None and access not in ("everyone", "managers"):
            raise ValueError(f"invalid access {access!r}; expected 'everyone' or 'managers'")
        with self._lock:
            with self.con:
                self.con.execute(
                    "INSERT INTO sources(kind, location, access) VALUES (?,?,COALESCE(?, 'everyone')) "
                    "ON CONFLICT(location) DO UPDATE SET access=COALESCE(?, sources.access)",
                    (kind, location, access, access),
                )
            return self.con.execute(
                "SELECT id FROM sources WHERE location=?", (location,)
            ).fetchone()[0]

    def list_sources(self, *, role: str) -> list[tuple[int, str, str, str]]:
        sql = "SELECT id, kind, location, access FROM sources"
        if role not in MANAGER_ROLES:
            sql += " WHERE access != 'managers'"
        sql += " ORDER BY id"
        with self._lock:
            return list(self.con.execute(sql))

    def delete_source(self, source_id: int) -> bool:
        """Remove a source and every document (and chunk/vector) ingested from it."""
        with self._lock:
            if not self.con.execute("SELECT 1 FROM sources WHERE id=?", (source_id,)).fetchone():
                return False
            doc_ids = [r[0] for r in self.con.execute(
                "SELECT id FROM documents WHERE source_id=?", (source_id,))]
            with self.con:
                for did in doc_ids:
                    self._delete_chunks(did)
                self.con.execute("DELETE FROM documents WHERE source_id=?", (source_id,))
                self.con.execute("DELETE FROM sources WHERE id=?", (source_id,))
            return True

    # -- users / roles -------------------------------------------------------

    def ensure_user(self, email: str) -> str:
        """Create on first sight with the default role; return the current role."""
        email = _norm_email(email)
        with self._lock:
            row = self.con.execute("SELECT role FROM users WHERE email=?", (email,)).fetchone()
            if row:
                return row[0]
            with self.con:
                self.con.execute("INSERT INTO users(email) VALUES (?)", (email,))
            return "developer"

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

    def create_token(self, email: str, name: str = "") -> str:
        """Mint a bearer token for MCP/CLI clients. Only its sha256 is stored."""
        email = _norm_email(email)
        token = "hk_" + secrets.token_urlsafe(32)
        self.ensure_user(email)
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self._lock, self.con:
            self.con.execute(
                "INSERT INTO tokens(token_hash, email, name) VALUES (?,?,?)",
                (digest, email, name),
            )
        return token

    def resolve_token(self, token: str) -> str | None:
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self._lock:
            row = self.con.execute(
                "SELECT email FROM tokens WHERE token_hash=?", (digest,)
            ).fetchone()
            if row:
                with self.con:
                    self.con.execute(
                        "UPDATE tokens SET last_used_at=datetime('now') WHERE token_hash=?",
                        (digest,),
                    )
        return row[0] if row else None

    def list_tokens(self, email: str) -> list[tuple[int, str, str, str | None]]:
        """Return (id, name, created_at, last_used_at) for all tokens belonging to email."""
        email = _norm_email(email)
        with self._lock:
            return list(self.con.execute(
                "SELECT id, name, created_at, last_used_at FROM tokens WHERE email=? ORDER BY id",
                (email,),
            ))

    def revoke_token(self, token_id: int, email: str) -> bool:
        """Delete the token matching both id and email. Returns True if a row was deleted."""
        email = _norm_email(email)
        with self._lock, self.con:
            cur = self.con.execute(
                "DELETE FROM tokens WHERE id=? AND email=?", (token_id, email)
            )
        return cur.rowcount > 0

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
        # quote each token so user punctuation can't break FTS query syntax
        tokens = [t for t in re.findall(r"\w+", query) if t]
        if not tokens:
            return []
        match = " OR ".join(f'"{t}"' for t in tokens)
        sql = """SELECT chunks_fts.rowid FROM chunks_fts
                 JOIN chunks c ON c.id = chunks_fts.rowid
                 JOIN documents d ON d.id = c.document_id
                 LEFT JOIN sources s ON s.id = d.source_id
                 WHERE chunks_fts MATCH ?"""
        if role not in MANAGER_ROLES:
            sql += " AND (s.access IS NULL OR s.access != 'managers')"
        sql += " ORDER BY bm25(chunks_fts) LIMIT ?"
        rows = self.con.execute(sql, (match, limit))
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
            # done when we have enough, the KNN returned fewer than asked (index
            # exhausted), or we've already asked for every chunk
            if len(visible) >= limit or len(rows) < k or k >= total:
                return visible[:limit]
            k = min(k * 4, total)

    def _visible_ids(self, chunk_ids: list[int], role: str) -> list[int]:
        """Filter chunk ids to those the role may see, preserving order. Also
        drops orphan vec rowids (no chunks row) via the join."""
        if not chunk_ids:
            return []
        ph = ",".join("?" * len(chunk_ids))
        sql = f"""SELECT c.id FROM chunks c
                  JOIN documents d ON d.id = c.document_id
                  LEFT JOIN sources s ON s.id = d.source_id
                  WHERE c.id IN ({ph})"""
        if role not in MANAGER_ROLES:
            sql += " AND (s.access IS NULL OR s.access != 'managers')"
        vis = {r[0] for r in self.con.execute(sql, chunk_ids)}
        return [cid for cid in chunk_ids if cid in vis]

    def _hit(self, chunk_id: int, score: float, role: str) -> SearchHit | None:
        row = self.con.execute(
            """SELECT c.id, d.id, d.path, d.title, c.heading_path, c.text, s.access
               FROM chunks c JOIN documents d ON d.id = c.document_id
               LEFT JOIN sources s ON s.id = d.source_id WHERE c.id=?""",
            (chunk_id,),
        ).fetchone()
        # An orphan chunk_vec rowid (no matching chunk) yields None: skip it rather
        # than crash on SearchHit(*None). FK CASCADE keeps chunks/FTS in sync but
        # not the vec0 table, so deletion stays centralized in _delete_chunks.
        # Rows invisible to the caller's role are also filtered here.
        if row is None or not _visible(role, row[6]):
            return None
        return SearchHit(*row[:6], score=score)

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
        """Exact/regex scan over raw chunk text. Complements the indexes for
        identifiers and codenames. Corpus is small; a full scan is fine.

        Invalid regex (the agent may construct one) raises ValueError so the
        caller/tool layer can surface a correctable message.
        Patterns exceeding GREP_MAX_PATTERN chars or those whose scan exceeds
        GREP_TIMEOUT_S wall-clock seconds also raise ValueError."""
        if len(pattern) > GREP_MAX_PATTERN:
            raise ValueError(f"pattern too long (max {GREP_MAX_PATTERN} chars)")
        try:
            rx = regex.compile(pattern, regex.IGNORECASE)
        except regex.error as e:
            raise ValueError(f"invalid regex pattern {pattern!r}: {e}") from e
        # Materialize rows under the lock, then run the (potentially slow) regex
        # outside it so a scan never holds the connection against other threads.
        with self._lock:
            rows = self.con.execute(
                """SELECT c.id, d.id, d.path, d.title, c.heading_path, c.text, s.access
                   FROM chunks c JOIN documents d ON d.id = c.document_id
                   LEFT JOIN sources s ON s.id = d.source_id"""
            ).fetchall()
        hits: list[SearchHit] = []
        deadline = time.monotonic() + GREP_TIMEOUT_S
        for row in rows:
            if not _visible(role, row[6]):
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError(f"pattern took too long (>{GREP_TIMEOUT_S}s)")
            try:
                matched = rx.search(row[5], timeout=remaining)
            except TimeoutError as e:
                raise ValueError(f"pattern took too long (>{GREP_TIMEOUT_S}s)") from e
            if matched:
                hits.append(SearchHit(*row[:6], score=1.0))
                if len(hits) >= limit:
                    break
        return hits

    def backup(self, dest: str | Path) -> None:
        """Write a consistent single-file snapshot to dest via VACUUM INTO.
        Works regardless of WAL state; dest must not already exist."""
        with self._lock:
            self.con.execute("VACUUM INTO ?", (str(dest),))
