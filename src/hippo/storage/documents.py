"""Document + chunk persistence: ingest upsert, dedup checks, role-filtered reads,
and the embedding-model stamp. `_delete_chunks` lives here and is reused by the
folders mixin's cascade delete (resolved via the Storage facade's MRO)."""

import sqlite_vec

from ..chunking import Chunk
from ._common import Document, DocumentMeta, _role_filter


class _DocumentsMixin:
    # -- documents ---------------------------------------------------------

    def _ensure_embedding_model(self) -> None:
        """Record the embedding model AND dimension on first write; refuse to mix
        embedding spaces. A same-dimension model swap followed by `sync` (not
        `reindex`) would silently blend two incompatible vector spaces; a pure
        dimension change is kept silently by chunk_vec's `IF NOT EXISTS` on reopen
        and otherwise surfaces only as a raw sqlite-vec "Dimension mismatch" error
        on the next insert, with no pointer to the fix. Stamping + validating the
        dim turns that into a clear reindex instruction. The stamp is global (two
        meta rows); `reindex` re-stamps both."""
        with self._lock, self.con:
            meta = dict(self.con.execute(
                "SELECT key, value FROM meta WHERE key IN ('embedding_model', 'embedding_dim')"
            ).fetchall())
            model_row, dim_row = meta.get("embedding_model"), meta.get("embedding_dim")
            if model_row is None:
                self.con.execute(
                    "INSERT INTO meta(key, value) VALUES ('embedding_model', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (self.embedder.model,))
                self.con.execute(
                    "INSERT INTO meta(key, value) VALUES ('embedding_dim', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(self.embedder.dim),))
                return
            if model_row != self.embedder.model:
                raise ValueError(
                    f"database was indexed with embedding model {model_row!r} but the "
                    f"configured model is {self.embedder.model!r}; run `hippo reindex` "
                    f"to re-embed, or set HIPPO_EMBEDDING_MODEL={model_row}"
                )
            if dim_row is None:
                # Legacy DB stamped before dim tracking: do NOT backfill from the
                # current embedder — the live chunk_vec width is unknown here, so a
                # blind stamp could record a dim that disagrees with the table (then
                # the insert fails raw AND the meta is wrong, masking it next time).
                # Leave it unstamped; the next `hippo reindex` stamps the true dim.
                pass
            elif int(dim_row) != self.embedder.dim:
                raise ValueError(
                    f"database was indexed at embedding dimension {dim_row} but the "
                    f"configured dimension is {self.embedder.dim}; run `hippo reindex` "
                    f"to rebuild the vector index, or set HIPPO_EMBEDDING_DIM={dim_row}"
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

    def list_document_meta(self, query: str | None = None, *, role: str) -> list[DocumentMeta]:
        """Like list_documents but WITHOUT the content column — for callers that only
        need id/path/title/summary (browse/list). Avoids reading the entire corpus's
        canonical markdown into memory just to discard it (MED-17)."""
        where, params = _role_filter(role)
        sql = ("SELECT d.id, d.path, d.title, d.summary "
               "FROM documents d JOIN folders f ON f.id = d.folder_id WHERE " + where)
        args: list = list(params)
        if query:
            sql += " AND (d.title LIKE ? OR d.path LIKE ? OR coalesce(d.summary,'') LIKE ?)"
            like = f"%{query}%"
            args += [like, like, like]
        sql += " ORDER BY d.path"
        with self._lock:
            return [DocumentMeta(*r) for r in self.con.execute(sql, args)]

    def paths_for_folder(self, folder_id: int) -> set[str]:
        with self._lock:
            return {r[0] for r in self.con.execute(
                "SELECT path FROM documents WHERE folder_id=?", (folder_id,))}
