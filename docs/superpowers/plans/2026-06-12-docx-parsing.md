# Word (.docx) Parsing Implementation Plan

> REQUIRED SUB-SKILL: superpowers:subagent-driven-development.

**Goal:** Roadmap item 5 — ingest `.docx` (the default Google-Docs download format) via `mammoth`. PDF and Google-Drive-URL are deferred (spec §5). Design: `../specs/2026-06-12-team-readiness-design.md` §5.

**Architecture:** `.docx` is binary, so parsing needs a bytes entry point. Add `parse_bytes()` as the canonical parser (text types decode → existing `parse_content`; `.docx` → mammoth→HTML→markdownify). `parse_file` reads bytes and delegates. Ingestion gains `ingest_bytes()`; the API upload path feeds raw bytes (no lossy utf-8 decode). Heading styles survive docx→markdown so chunking/citations work.

**Hard rules:** tests zero-network (build a minimal `.docx` in-test with `zipfile` — no fixtures download); all SQL in storage.py; TDD.

---

### Task 1 — parsers: `.docx` via mammoth
- `pyproject.toml`: add `mammoth>=1.6.0` (pin to installed after `uv add`).
- `parsers.py`: add `.docx` to `SUPPORTED`; add `parse_bytes(fallback_title, data: bytes, suffix) -> (title, md)`:
  - `.docx` → `mammoth.convert_to_html(io.BytesIO(data)).value` → `markdownify(html, heading_style="ATX").strip()`; title from first `# h1` else `fallback_title`.
  - text suffixes → `data.decode("utf-8")` (raise `ValueError` on `UnicodeDecodeError`) → delegate to existing `parse_content`.
  - unknown suffix → `ValueError`.
- `parse_file(path)` → `parse_bytes(path.stem, path.read_bytes(), path.suffix.lower())` (removes its own decode).
- Tests (`tests/test_parsers.py` or extend `test_ingest.py`): a `_minimal_docx(text)` helper builds a valid docx via `zipfile` (`[Content_Types].xml`, `_rels/.rels`, `word/document.xml`); assert `parse_file` on it yields markdown containing the body text and the filename-stem title; assert the existing text/html paths still parse; unsupported suffix raises.

### Task 2 — ingestion + API + UI feed bytes
- `ingest.py`: add `ingest_bytes(name, data: bytes, *, suffix=".md", source_type="upload")` mirroring `ingest_text` (parse via `parse_bytes`, content-hash-qualified `upload/{sha8}-{name}` path, per-file isolation). Make `ingest_text` delegate: `return self.ingest_bytes(name, raw.encode("utf-8"), suffix=suffix)` (keeps str callers/tests working).
- `api.py` `/ingest` fallback (non-GitHub) branch: replace `raw = raw_bytes.decode(...); ingest_text(...)` with `ingestor.ingest_bytes(name, raw_bytes, suffix=suffix)` (no lossy decode; docx works). GitHub-commit branch unchanged (commits raw bytes; its char-check stays a rough byte-proxy — authoritative doc-char cap is enforced at parse/_index time).
- `ui/src/App.tsx`: add `.docx` to the upload `<input accept=...>`.
- Tests (`tests/test_ingest.py`, `tests/test_api_auth.py`): a `.docx` upload through `ingest_bytes` indexes and is searchable; API `/ingest` of a `.docx` (fallback mode) returns added/`versioned:false`; existing `.md` upload tests still pass.

### Task 3 — docs + gate
- README: note `.docx` supported (download a Google Doc as .docx → upload); PDF/Drive-URL still future.
- CLAUDE.md: parsers line mentions `.docx` via mammoth; bump test count; roadmap item 5 → built.
- Gate: `uv run pytest`, `cd ui && npm run build`, `docker build` (mammoth in the image), eval 4/4 if Ollama up.

## Self-review notes
- mammoth→HTML→markdownify reuses the proven HTML path (heading-aware). PDF explicitly excluded.
- The minimal in-test docx avoids any network/fixture dependency; if mammoth can't parse it, TDD surfaces it immediately.
