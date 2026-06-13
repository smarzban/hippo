import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path

from .chunking import chunk_markdown
from .parsers import SUPPORTED, parse_bytes, parse_content, parse_file
from .storage import Storage

log = logging.getLogger("hippo.ingest")


@dataclass
class IngestResult:
    path: str
    status: str  # added | updated | skipped | failed
    chunks: int = 0
    error: str | None = None


@dataclass
class SyncReport:
    results: list[IngestResult] = field(default_factory=list)
    removed: int = 0

    @property
    def added(self) -> int:
        return sum(1 for r in self.results if r.status == "added")

    @property
    def updated(self) -> int:
        return sum(1 for r in self.results if r.status == "updated")

    @property
    def skipped(self) -> int:
        return sum(1 for r in self.results if r.status == "skipped")

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if r.status == "failed")

    def summary(self) -> str:
        line = f"synced {self.added + self.updated}, skipped {self.skipped} unchanged, removed {self.removed}, failed {self.failed}"
        for r in self.results:
            if r.status == "failed":
                line += f"\n  failed: {r.path}: {r.error}"
        return line


class Ingestor:
    """The one pipeline: parse -> hash/dedupe -> chunk -> (enrich) -> embed+index."""

    def __init__(self, store: Storage, *, max_chars: int, overlap_chars: int,
                 enricher=None, max_doc_chars: int | None = None,
                 max_decompressed_bytes: int | None = None):
        self.store = store
        self.max_chars = max_chars
        self.overlap_chars = overlap_chars
        self.enricher = enricher  # Task 9; None = enrichment off
        self.max_doc_chars = max_doc_chars
        self.max_decompressed_bytes = max_decompressed_bytes

    def ingest_file(self, path: Path, *, source_type: str, folder_id: int) -> IngestResult:
        try:
            title, md = parse_file(path)
            return self._index(str(path), title, md, source_type=source_type, folder_id=folder_id)
        except Exception as e:  # per-file isolation: one bad file never kills a sync
            log.warning("failed %s: %s", path, e)
            return IngestResult(path=str(path), status="failed", error=str(e))

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

    def ingest_text(self, name: str, raw: str, *, folder_id: int, path_prefix: str = "",
                    suffix: str = ".md", source_type: str = "upload") -> IngestResult:
        return self.ingest_bytes(name, raw.encode("utf-8"), folder_id=folder_id,
                                 path_prefix=path_prefix, suffix=suffix, source_type=source_type)

    def _index(self, path: str, title: str, md: str, *, source_type: str, folder_id: int) -> IngestResult:
        if not md.strip():
            log.debug("skipped %s: empty content", path)
            return IngestResult(path=path, status="skipped")  # no ghost documents
        if self.max_doc_chars and len(md) > self.max_doc_chars:
            log.info("skipped %s: exceeds max_doc_chars", path)
            return IngestResult(path=path, status="skipped",
                                error=f"document exceeds max_doc_chars ({self.max_doc_chars})")
        content_hash = hashlib.sha256(md.encode()).hexdigest()
        existed = self.store.document_exists(path)  # via Storage; no SQL outside storage.py
        if self.store.is_unchanged(path, content_hash):
            log.debug("skipped %s: unchanged", path)
            return IngestResult(path=path, status="skipped")

        chunks = chunk_markdown(md, max_chars=self.max_chars, overlap_chars=self.overlap_chars)
        summary = None
        embed_inputs = [c.text for c in chunks]
        if self.enricher is not None:
            summary = self.enricher.summarize(title, md)
            embed_inputs = [
                self.enricher.contextualize(title, c.heading_path, c.text) + "\n" + c.text
                for c in chunks
            ]
        self.store.upsert_document(
            source_type=source_type, path=path, title=title, content=md,
            content_hash=content_hash, chunks=chunks, embed_inputs=embed_inputs,
            summary=summary, folder_id=folder_id,
        )
        result = IngestResult(path=path, status="updated" if existed else "added", chunks=len(chunks))
        log.info("ingested %s: %s (%d chunks)", path, result.status, result.chunks)
        return result


# infrastructure noise we never even try to ingest (vs unsupported docs, which
# we attempt so they surface in the failure report)
IGNORED_EXTENSIONS = {".db", ".db-wal", ".db-shm", ".sqlite", ".pyc"}
IGNORED_DIRS = {".git", "__pycache__", "node_modules", ".venv"}


def _ignored(path: Path) -> bool:
    if path.suffix.lower() in IGNORED_EXTENSIONS or path.name.startswith("."):
        return True
    return any(part in IGNORED_DIRS for part in path.parts)


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
