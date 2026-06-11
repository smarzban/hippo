import re
import sqlite3
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


class Storage:
    """All database access. The agent and ingestion never touch SQL directly."""

    def __init__(self, con: sqlite3.Connection, embedder: Embedder):
        self.con = con
        self.embedder = embedder

    # -- documents ---------------------------------------------------------

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
        vectors = self.embedder.embed(embed_inputs)
        with self.con:  # one transaction per document
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
        row = self.con.execute("SELECT id FROM documents WHERE path=?", (path,)).fetchone()
        if not row:
            return False
        with self.con:
            self._delete_chunks(row[0])
            self.con.execute("DELETE FROM documents WHERE id=?", (row[0],))
        return True

    def is_unchanged(self, path: str, content_hash: str) -> bool:
        row = self.con.execute("SELECT content_hash FROM documents WHERE path=?", (path,)).fetchone()
        return bool(row and row[0] == content_hash)

    def get_document(self, doc_id: int) -> Document | None:
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
        return [Document(*r) for r in self.con.execute(sql, args)]

    def paths_for_source(self, source_id: int) -> set[str]:
        return {r[0] for r in self.con.execute("SELECT path FROM documents WHERE source_id=?", (source_id,))}

    # -- sources -----------------------------------------------------------

    def register_source(self, kind: str, location: str) -> int:
        with self.con:
            self.con.execute(
                "INSERT INTO sources(kind, location) VALUES (?,?) ON CONFLICT(location) DO NOTHING",
                (kind, location),
            )
        return self.con.execute("SELECT id FROM sources WHERE location=?", (location,)).fetchone()[0]

    def list_sources(self) -> list[tuple[int, str, str]]:
        return list(self.con.execute("SELECT id, kind, location FROM sources ORDER BY id"))

    # -- search --------------------------------------------------------------

    RRF_K = 60

    def search_hybrid(self, query: str, top_k: int = 8) -> list[SearchHit]:
        """FTS5 BM25 + vector KNN, merged with Reciprocal Rank Fusion."""
        fts_ranked = self._search_fts(query, limit=top_k * 3)
        vec_ranked = self._search_vec(query, limit=top_k * 3)
        scores: dict[int, float] = {}
        for ranked in (fts_ranked, vec_ranked):
            for rank, chunk_id in enumerate(ranked):
                scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (self.RRF_K + rank + 1)
        best = sorted(scores, key=scores.__getitem__, reverse=True)[:top_k]
        return [self._hit(cid, scores[cid]) for cid in best]

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

    def _search_vec(self, query: str, limit: int) -> list[int]:
        vec = self.embedder.embed([query])[0]
        rows = self.con.execute(
            "SELECT rowid FROM chunk_vec WHERE embedding MATCH ? AND k = ? ORDER BY distance",
            (sqlite_vec.serialize_float32(vec), limit),
        )
        return [r[0] for r in rows]

    def _hit(self, chunk_id: int, score: float) -> SearchHit:
        row = self.con.execute(
            """SELECT c.id, d.id, d.path, d.title, c.heading_path, c.text
               FROM chunks c JOIN documents d ON d.id = c.document_id WHERE c.id=?""",
            (chunk_id,),
        ).fetchone()
        return SearchHit(*row, score=score)

    def grep(self, pattern: str, limit: int = 20) -> list[SearchHit]:
        """Exact/regex scan over raw chunk text. Complements the indexes for
        identifiers and codenames. Corpus is small; a full scan is fine."""
        rx = re.compile(pattern, re.IGNORECASE)
        hits: list[SearchHit] = []
        rows = self.con.execute(
            """SELECT c.id, d.id, d.path, d.title, c.heading_path, c.text
               FROM chunks c JOIN documents d ON d.id = c.document_id"""
        )
        for row in rows:
            if rx.search(row[5]):
                hits.append(SearchHit(*row, score=1.0))
                if len(hits) >= limit:
                    break
        return hits
