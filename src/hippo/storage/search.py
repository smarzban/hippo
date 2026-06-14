"""Retrieval + index maintenance: hybrid search (FTS5 BM25 + vec KNN via RRF),
the role-filtered candidate helpers, regex grep, the safe reindex, and the
VACUUM-INTO backup. All reads are role-filtered; network embedding happens
outside the connection lock."""

import re
import time
from pathlib import Path

import regex
import sqlite_vec

from ._common import SearchHit, _role_filter, log

# Grep + KNN tuning. Defined here (with the methods that read them) so this module
# is their single source of truth: tests monkeypatch `hippo.storage.search.
# GREP_TIMEOUT_S` etc. to exercise the bounds. They are deliberately NOT re-exported
# from the package __init__ — a package-level copy would be a stale binding that
# grep() doesn't read (so patching it would be silently inert).
GREP_MAX_PATTERN = 200      # reject absurdly long patterns
GREP_TIMEOUT_S = 2.0        # wall-clock cap per chunk scan (regex module)
GREP_MAX_CHUNKS = 5000      # cap chunks materialized+scanned per grep (memory + lock bound)
VEC_OVERFETCH = 10          # _search_vec candidate multiplier (single fetch, no backoff loop)


class _SearchMixin:
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
        if total == 0:
            return []
        # ONE generous over-fetch (capped at total), then role-filter the candidates.
        # The old code re-ran the full brute-force vec0 KNN in a *4 backoff loop until
        # enough survived the role filter — for a low-tier user whose readable folders
        # hold a small fraction of chunks that meant several full scans per query. We
        # accept fewer-than-limit results for a role-sparse corpus instead (MED-18).
        k = min(limit * VEC_OVERFETCH, total)
        rows = [r[0] for r in self.con.execute(
            "SELECT rowid FROM chunk_vec WHERE embedding MATCH ? AND k = ? ORDER BY distance",
            (serialized, k),
        )]
        return self._visible_ids(rows, role)[:limit]

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
        all embeddings succeed. Returns the number of chunks re-embedded.

        A concurrent ingest through THIS Storage is serialized by self._lock and the
        (id, text) re-check below aborts if the chunk set changed (MED-06). Across
        PROCESSES, though (e.g. `hippo reindex` CLI while `hippo serve` is ingesting),
        the two connections have independent locks — run reindex with no concurrent
        sync/upload to be safe (INF-06)."""
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
            # Detect a concurrent ingest during the (unlocked) embedding window: if the
            # chunk set changed, the snapshot is stale — rebuilding chunk_vec from it
            # would strand new chunks with no vector (silent retrieval gap, MED-06).
            # Compare full (id, text), not just ids: an update deletes+reinserts a doc's
            # chunks and SQLite can REUSE the freed rowids, so the id set can be unchanged
            # while the text differs — embedding old text under reused ids. Abort BEFORE
            # the DROP so the existing index stays intact; the operator re-runs reindex
            # with no concurrent sync/upload.
            current = self.con.execute("SELECT id, text FROM chunks ORDER BY id").fetchall()
            if current != rows:
                raise ValueError(
                    "documents changed during reindex (concurrent ingest detected); the "
                    "vector index was left untouched — re-run `hippo reindex` with no "
                    "concurrent sync/upload")
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
            self.con.execute(
                "INSERT INTO meta(key, value) VALUES ('embedding_dim', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(embedding_dim),),
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
        # Bound the materialization: without a LIMIT this fetchall pulls the full text of
        # EVERY role-visible chunk into memory (and holds the connection lock for the
        # whole fetch). Cap it so grep stays bounded in memory + lock-hold; if the cap
        # bites, say so rather than silently truncating (MED-16).
        with self._lock:
            rows = self.con.execute(
                f"""SELECT c.id, d.id, d.path, d.title, c.heading_path, c.text
                    FROM chunks c JOIN documents d ON d.id = c.document_id
                    JOIN folders f ON f.id = d.folder_id WHERE {where} LIMIT ?""",
                (*params, GREP_MAX_CHUNKS + 1),
            ).fetchall()
        if len(rows) > GREP_MAX_CHUNKS:
            rows = rows[:GREP_MAX_CHUNKS]
            log.warning("grep scanned only the first %d role-visible chunks (cap reached); "
                        "some matches beyond that may be missed", GREP_MAX_CHUNKS)
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
