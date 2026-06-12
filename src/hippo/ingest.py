import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from .chunking import chunk_markdown
from .parsers import SUPPORTED, parse_content, parse_file
from .storage import Storage


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

    def __init__(self, store: Storage, *, max_chars: int, overlap_chars: int, enricher=None):
        self.store = store
        self.max_chars = max_chars
        self.overlap_chars = overlap_chars
        self.enricher = enricher  # Task 9; None = enrichment off

    def ingest_file(self, path: Path, *, source_type: str, source_id: int | None = None) -> IngestResult:
        try:
            title, md = parse_file(path)
            return self._index(str(path), title, md, source_type=source_type, source_id=source_id)
        except Exception as e:  # per-file isolation: one bad file never kills a sync
            return IngestResult(path=str(path), status="failed", error=str(e))

    def ingest_text(self, name: str, raw: str, *, suffix: str = ".md", source_type: str = "upload") -> IngestResult:
        try:
            if suffix.lower() not in SUPPORTED:
                raise ValueError(f"unsupported file type: {suffix}")
            title, md = parse_content(name, raw, suffix)
            # Uploads have no stable source path: two different files both named
            # "notes.md" would collide on the UNIQUE documents.path and silently
            # overwrite. Qualify with a short content hash so distinct contents
            # coexist, while an identical re-upload still dedupes via is_unchanged.
            digest = hashlib.sha256(md.encode()).hexdigest()[:8]
            return self._index(f"upload/{digest}-{name}", title, md, source_type=source_type, source_id=None)
        except Exception as e:
            return IngestResult(path=name, status="failed", error=str(e))

    def _index(self, path: str, title: str, md: str, *, source_type: str, source_id: int | None) -> IngestResult:
        if not md.strip():
            return IngestResult(path=path, status="skipped")  # no ghost documents
        content_hash = hashlib.sha256(md.encode()).hexdigest()
        existed = self.store.document_exists(path)  # via Storage; no SQL outside storage.py
        if self.store.is_unchanged(path, content_hash):
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
            summary=summary, source_id=source_id,
        )
        return IngestResult(path=path, status="updated" if existed else "added", chunks=len(chunks))


# infrastructure noise we never even try to ingest (vs unsupported docs, which
# we attempt so they surface in the failure report)
IGNORED_EXTENSIONS = {".db", ".db-wal", ".db-shm", ".sqlite", ".pyc"}
IGNORED_DIRS = {".git", "__pycache__", "node_modules", ".venv"}


def _ignored(path: Path) -> bool:
    if path.suffix.lower() in IGNORED_EXTENSIONS or path.name.startswith("."):
        return True
    return any(part in IGNORED_DIRS for part in path.parts)


def sync_folder(folder: Path, store: Storage, *, max_chars: int, overlap_chars: int, enricher=None) -> SyncReport:
    """Sync one folder: ingest new/changed, remove vanished. Per-file isolation."""
    source_id = store.register_source("folder", str(folder))
    ing = Ingestor(store, max_chars=max_chars, overlap_chars=overlap_chars, enricher=enricher)
    report = SyncReport()
    seen: set[str] = set()
    for path in sorted(folder.rglob("*")):
        if _ignored(path):
            continue
        if path.is_file() and path.suffix.lower() in SUPPORTED:
            seen.add(str(path))
            report.results.append(ing.ingest_file(path, source_type="folder", source_id=source_id))
        elif path.is_file():
            # unsupported extensions are attempted so they show up in the failure report
            report.results.append(ing.ingest_file(path, source_type="folder", source_id=source_id))
    for stale in store.paths_for_source(source_id) - seen:
        if store.delete_document_by_path(stale):
            report.removed += 1
    return report
