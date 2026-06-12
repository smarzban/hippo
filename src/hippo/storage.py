import re
import sqlite3
import threading
from dataclasses import dataclass

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

    def get_document(self, doc_id: int) -> Document | None:
        with self._lock:
            row = self.con.execute(
                "SELECT id, source_type, path, title, content, content_hash, summary FROM documents WHERE id=?",
                (doc_id,),
            ).fetchone()
        return Document(*row) if row else None

    def list_documents(self, query: str | None = None) -> list[Document]:
        sql = "SELECT id, source_type, path, title, content, content_hash, summary FROM documents"
        args: tuple = ()
        if query:
            sql += " WHERE title LIKE ? OR path LIKE ? OR coalesce(summary,'') LIKE ?"
            like = f"%{query}%"
            args = (like, like, like)
        sql += " ORDER BY path"
        with self._lock:
            return [Document(*r) for r in self.con.execute(sql, args)]

    def paths_for_source(self, source_id: int) -> set[str]:
        with self._lock:
            return {r[0] for r in self.con.execute("SELECT path FROM documents WHERE source_id=?", (source_id,))}

    # -- sources -----------------------------------------------------------

    def register_source(self, kind: str, location: str) -> int:
        with self._lock:
            with self.con:
                self.con.execute(
                    "INSERT INTO sources(kind, location) VALUES (?,?) ON CONFLICT(location) DO NOTHING",
                    (kind, location),
                )
            return self.con.execute("SELECT id FROM sources WHERE location=?", (location,)).fetchone()[0]

    def list_sources(self) -> list[tuple[int, str, str]]:
        with self._lock:
            return list(self.con.execute("SELECT id, kind, location FROM sources ORDER BY id"))

    # -- users / roles -------------------------------------------------------

    def ensure_user(self, email: str) -> str:
        """Create on first sight with the default role; return the current role."""
        with self._lock:
            row = self.con.execute("SELECT role FROM users WHERE email=?", (email,)).fetchone()
            if row:
                return row[0]
            with self.con:
                self.con.execute("INSERT INTO users(email) VALUES (?)", (email,))
            return "developer"

    def set_role(self, email: str, role: str) -> None:
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

    # -- search --------------------------------------------------------------

    RRF_K = 60

    def search_hybrid(self, query: str, top_k: int = 8) -> list[SearchHit]:
        """FTS5 BM25 + vector KNN, merged with Reciprocal Rank Fusion."""
        if not query.strip():
            return []
        qvec = self.embedder.embed([query])[0]  # network: outside the lock
        with self._lock:
            fts_ranked = self._search_fts(query, limit=top_k * 3)
            vec_ranked = self._search_vec(qvec, limit=top_k * 3)
            scores: dict[int, float] = {}
            for ranked in (fts_ranked, vec_ranked):
                for rank, chunk_id in enumerate(ranked):
                    scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (self.RRF_K + rank + 1)
            best = sorted(scores, key=scores.__getitem__, reverse=True)[:top_k]
            hits = [self._hit(cid, scores[cid]) for cid in best]
        return [h for h in hits if h is not None]  # drop orphan vec rowids (see _hit)

    def _search_fts(self, query: str, limit: int) -> list[int]:
        # quote each token so user punctuation can't break FTS query syntax
        tokens = [t for t in re.findall(r"\w+", query) if t]
        if not tokens:
            return []
        match = " OR ".join(f'"{t}"' for t in tokens)
        rows = self.con.execute(
            "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY bm25(chunks_fts) LIMIT ?",
            (match, limit),
        )
        return [r[0] for r in rows]

    def _search_vec(self, vec: list[float], limit: int) -> list[int]:
        rows = self.con.execute(
            "SELECT rowid FROM chunk_vec WHERE embedding MATCH ? AND k = ? ORDER BY distance",
            (sqlite_vec.serialize_float32(vec), limit),
        )
        return [r[0] for r in rows]

    def _hit(self, chunk_id: int, score: float) -> SearchHit | None:
        row = self.con.execute(
            """SELECT c.id, d.id, d.path, d.title, c.heading_path, c.text
               FROM chunks c JOIN documents d ON d.id = c.document_id WHERE c.id=?""",
            (chunk_id,),
        ).fetchone()
        # An orphan chunk_vec rowid (no matching chunk) yields None: skip it rather
        # than crash on SearchHit(*None). FK CASCADE keeps chunks/FTS in sync but
        # not the vec0 table, so deletion stays centralized in _delete_chunks.
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

    def grep(self, pattern: str, limit: int = 20) -> list[SearchHit]:
        """Exact/regex scan over raw chunk text. Complements the indexes for
        identifiers and codenames. Corpus is small; a full scan is fine.

        Invalid regex (the agent may construct one) raises ValueError so the
        caller/tool layer can surface a correctable message."""
        try:
            rx = re.compile(pattern, re.IGNORECASE)
        except re.error as e:
            raise ValueError(f"invalid regex pattern {pattern!r}: {e}") from e
        # Materialize rows under the lock, then run the (potentially slow) regex
        # outside it so a scan never holds the connection against other threads.
        with self._lock:
            rows = self.con.execute(
                """SELECT c.id, d.id, d.path, d.title, c.heading_path, c.text
                   FROM chunks c JOIN documents d ON d.id = c.document_id"""
            ).fetchall()
        hits: list[SearchHit] = []
        for row in rows:
            if rx.search(row[5]):
                hits.append(SearchHit(*row, score=1.0))
                if len(hits) >= limit:
                    break
        return hits
