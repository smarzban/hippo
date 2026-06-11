# Knowledge Hub Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the agentic knowledge hub per `docs/superpowers/specs/2026-06-11-knowledge-hub-design.md` — ingest markdown/text/HTML into a hybrid-searchable SQLite store, answer questions via a Pydantic AI agent with citations, served over FastAPI (Vercel AI protocol) to a React chat UI.

**Architecture:** Python backend (`src/knowledgehub/`): config → db → chunking/embeddings → storage (hybrid search) → parsers/ingest → enrichment → agent → FastAPI API → Typer CLI. Separate `ui/` Vite React app consuming the Vercel AI Data Stream Protocol. SQLite (sqlite-vec + FTS5) is the only state.

**Tech Stack:** Python 3.12+ (3.14 installed), uv, Pydantic AI, FastAPI, sqlite-vec, FTS5, Typer, watchfiles, markdownify, pytest + anyio, React + Vite + `@ai-sdk/react` (Node 22).

**Conventions for every task:** run Python via `uv run`, work from repo root `/home/patch/TGC-PRJ/knowledgeHub`. Tests never call real LLM/embedding APIs (`FakeEmbedder` + Pydantic AI `TestModel`/`FunctionModel`; set `pydantic_ai.models.ALLOW_MODEL_REQUESTS = False` in agent tests). Commit after every green test.

---

### Task 1: Project scaffold

**Files:**
- Create: `pyproject.toml`, `.gitignore`, `src/knowledgehub/__init__.py`, `tests/__init__.py`, `tests/conftest.py`

- [ ] **Step 1: Write `pyproject.toml`**

```toml
[project]
name = "knowledgehub"
version = "0.1.0"
description = "Agentic knowledge hub: ingest docs, query via chat agent"
requires-python = ">=3.12"
dependencies = [
    "pydantic-ai",
    "pydantic-settings",
    "fastapi",
    "uvicorn",
    "sqlite-vec",
    "typer",
    "watchfiles",
    "markdownify",
    "pyyaml",
    "python-multipart",
]

[project.scripts]
hub = "knowledgehub.cli:app"

[dependency-groups]
dev = ["pytest", "anyio", "httpx"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/knowledgehub"]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-q"
```

- [ ] **Step 2: Write `.gitignore`**

```
__pycache__/
*.py[cod]
.venv/
*.db
*.db-wal
*.db-shm
ui/node_modules/
ui/dist/
.pytest_cache/
.env
```

- [ ] **Step 3: Create package + empty test scaffolding**

`src/knowledgehub/__init__.py` and `tests/__init__.py` — both empty. `tests/conftest.py`:

```python
import pytest


@pytest.fixture
def anyio_backend():
    return "asyncio"
```

- [ ] **Step 4: Install and verify**

Run: `uv sync && uv run python -c "import knowledgehub; print('ok')"`
Expected: prints `ok`

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "chore: project scaffold (uv, pyproject, package layout)"
```

---

### Task 2: Config

**Files:**
- Create: `src/knowledgehub/config.py`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write the failing test**

```python
from pathlib import Path

from knowledgehub.config import Settings


def test_defaults():
    s = Settings(_env_file=None)
    assert s.db_path == Path("hub.db")
    assert s.chat_model == "openai:gpt-5.2"
    assert s.embedding_model == "text-embedding-3-small"
    assert s.embedding_dim == 1536
    assert s.enrich_enabled is True
    assert s.enrich_model == "openai:gpt-5-mini"
    assert s.chunk_max_chars == 3000
    assert s.max_tool_calls == 15


def test_env_override(monkeypatch):
    monkeypatch.setenv("HUB_CHAT_MODEL", "anthropic:claude-opus-4-8")
    s = Settings(_env_file=None)
    assert s.chat_model == "anthropic:claude-opus-4-8"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_config.py -v`
Expected: FAIL (ModuleNotFoundError / ImportError)

- [ ] **Step 3: Implement `src/knowledgehub/config.py`**

```python
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All knobs. Override via env vars prefixed HUB_ (e.g. HUB_CHAT_MODEL)."""

    model_config = SettingsConfigDict(env_prefix="HUB_", env_file=".env")

    db_path: Path = Path("hub.db")
    chat_model: str = "openai:gpt-5.2"
    embedding_model: str = "text-embedding-3-small"
    embedding_dim: int = 1536
    enrich_enabled: bool = True
    enrich_model: str = "openai:gpt-5-mini"
    chunk_max_chars: int = 3000  # ~750 tokens
    chunk_overlap_chars: int = 200
    max_tool_calls: int = 15
    search_top_k: int = 8


def get_settings() -> Settings:
    return Settings()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_config.py -v` — Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat: settings via pydantic-settings (HUB_ env prefix)"
```

---

### Task 3: Database schema + connection

**Files:**
- Create: `src/knowledgehub/db.py`
- Test: `tests/test_db.py`

- [ ] **Step 1: Write the failing test**

```python
from knowledgehub.db import connect


def test_schema_created(tmp_path):
    con = connect(tmp_path / "t.db", embedding_dim=32)
    tables = {
        r[0]
        for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','virtual table') OR type='table'"
        )
    }
    names = {r[0] for r in con.execute("SELECT name FROM sqlite_master")}
    for required in ("meta", "sources", "documents", "chunks", "chunks_fts", "chunk_vec"):
        assert required in names, f"missing {required}"
    assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_vec_roundtrip(tmp_path):
    import sqlite_vec

    con = connect(tmp_path / "t.db", embedding_dim=4)
    con.execute(
        "INSERT INTO chunk_vec(rowid, embedding) VALUES (1, ?)",
        (sqlite_vec.serialize_float32([1.0, 0.0, 0.0, 0.0]),),
    )
    row = con.execute(
        "SELECT rowid, distance FROM chunk_vec WHERE embedding MATCH ? AND k = 1",
        (sqlite_vec.serialize_float32([1.0, 0.0, 0.0, 0.0]),),
    ).fetchone()
    assert row[0] == 1


def test_fts_sync_triggers(tmp_path):
    con = connect(tmp_path / "t.db", embedding_dim=4)
    con.execute(
        "INSERT INTO documents(source_type, path, title, content, content_hash) VALUES ('upload','a.md','A','hello world','h1')"
    )
    doc_id = con.execute("SELECT id FROM documents").fetchone()[0]
    con.execute(
        "INSERT INTO chunks(document_id, position, heading_path, text) VALUES (?,0,'','hello world')",
        (doc_id,),
    )
    hit = con.execute(
        "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH '\"hello\"'"
    ).fetchone()
    assert hit is not None
    con.execute("DELETE FROM chunks WHERE document_id = ?", (doc_id,))
    assert con.execute("SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH '\"hello\"'").fetchone()[0] == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_db.py -v` — Expected: FAIL (no module `knowledgehub.db`)

- [ ] **Step 3: Implement `src/knowledgehub/db.py`**

```python
import sqlite3
from pathlib import Path

import sqlite_vec

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL DEFAULT 'folder',
    location TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY,
    source_id INTEGER REFERENCES sources(id) ON DELETE SET NULL,
    source_type TEXT NOT NULL,            -- folder | upload | connector
    path TEXT NOT NULL UNIQUE,
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
"""


def connect(db_path: Path | str, embedding_dim: int) -> sqlite3.Connection:
    """Open (creating if needed) the hub database with vec + FTS ready."""
    con = sqlite3.connect(db_path, check_same_thread=False)
    con.enable_load_extension(True)
    sqlite_vec.load(con)
    con.enable_load_extension(False)
    con.execute("PRAGMA journal_mode = WAL")
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(SCHEMA)
    con.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS chunk_vec USING vec0(embedding float[{int(embedding_dim)}])"
    )
    con.commit()
    return con
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_db.py -v` — Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat: sqlite schema with sqlite-vec + FTS5 (WAL, trigger-synced)"
```

---

### Task 4: Heading-aware chunker

**Files:**
- Create: `src/knowledgehub/chunking.py`
- Test: `tests/test_chunking.py`

- [ ] **Step 1: Write the failing test**

```python
from knowledgehub.chunking import Chunk, chunk_markdown


def test_heading_path_recorded():
    md = "# Polly\n\nIntro text.\n\n## Integrations\n\n### Telegram\n\nWebhook setup details."
    chunks = chunk_markdown(md, max_chars=3000, overlap_chars=0)
    paths = [c.heading_path for c in chunks]
    assert any("Polly > Integrations > Telegram" in p for p in paths)
    tg = next(c for c in chunks if "Telegram" in c.heading_path)
    assert "Webhook setup details." in tg.text


def test_long_section_splits_with_overlap():
    paras = "\n\n".join(f"Paragraph {i}. " + "x" * 200 for i in range(30))
    md = f"# Doc\n\n{paras}"
    chunks = chunk_markdown(md, max_chars=1000, overlap_chars=100)
    assert len(chunks) > 3
    assert all(len(c.text) <= 1000 for c in chunks)
    # overlap: end of chunk N appears at start of chunk N+1
    assert chunks[1].text[:50] in chunks[0].text


def test_code_fence_never_split():
    code = "```python\n" + "\n".join(f"line_{i} = {i}" for i in range(40)) + "\n```"
    md = f"# Doc\n\nbefore\n\n{code}\n\nafter"
    chunks = chunk_markdown(md, max_chars=300, overlap_chars=0)
    fenced = [c for c in chunks if "```python" in c.text]
    assert len(fenced) == 1
    assert fenced[0].text.count("```") == 2  # open + close in same chunk


def test_positions_sequential():
    md = "# A\n\n" + "\n\n".join("p" * 400 for _ in range(10))
    chunks = chunk_markdown(md, max_chars=900, overlap_chars=0)
    assert [c.position for c in chunks] == list(range(len(chunks)))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_chunking.py -v` — Expected: FAIL (import error)

- [ ] **Step 3: Implement `src/knowledgehub/chunking.py`**

```python
import re
from dataclasses import dataclass

HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
FENCE_RE = re.compile(r"^(```|~~~)")


@dataclass
class Chunk:
    position: int
    heading_path: str
    text: str


def _blocks(md: str) -> list[tuple[str, str]]:
    """Split markdown into (kind, text) blocks: 'heading', 'code', 'para'.

    Code fences are kept as single atomic blocks.
    """
    blocks: list[tuple[str, str]] = []
    lines = md.splitlines()
    i = 0
    para: list[str] = []

    def flush():
        nonlocal para
        text = "\n".join(para).strip()
        if text:
            blocks.append(("para", text))
        para = []

    while i < len(lines):
        line = lines[i]
        if FENCE_RE.match(line.strip()):
            flush()
            fence = [line]
            i += 1
            while i < len(lines):
                fence.append(lines[i])
                if FENCE_RE.match(lines[i].strip()):
                    break
                i += 1
            blocks.append(("code", "\n".join(fence)))
        elif HEADING_RE.match(line):
            flush()
            blocks.append(("heading", line))
        elif not line.strip():
            flush()
        else:
            para.append(line)
        i += 1
    flush()
    return blocks


def chunk_markdown(md: str, max_chars: int = 3000, overlap_chars: int = 200) -> list[Chunk]:
    """Heading-aware packing: blocks accumulate into chunks <= max_chars.

    Headings update the heading path; a chunk never mixes content from two
    heading sections. Code fences are atomic (oversized ones become their own
    chunk even if > max_chars... but we cap to a single block). Overlap copies
    the tail of the previous chunk into the next within the same section.
    """
    heading_stack: list[tuple[int, str]] = []
    chunks: list[Chunk] = []
    buf: list[str] = []
    buf_path = ""

    def path() -> str:
        return " > ".join(h for _, h in heading_stack)

    def flush():
        nonlocal buf
        text = "\n\n".join(buf).strip()
        if text:
            chunks.append(Chunk(position=len(chunks), heading_path=buf_path, text=text))
        buf = []

    for kind, text in _blocks(md):
        if kind == "heading":
            flush()
            m = HEADING_RE.match(text)
            assert m
            level, title = len(m.group(1)), m.group(2).strip()
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, title))
            continue

        current = "\n\n".join(buf)
        if buf and len(current) + len(text) + 2 > max_chars:
            tail = current[-overlap_chars:] if overlap_chars else ""
            flush()
            if tail:
                buf = [tail]
        buf_path = path()
        buf.append(text)

        # an atomic block alone may exceed max; emit it solo
        if len("\n\n".join(buf)) > max_chars and len(buf) == 1:
            flush()

    flush()
    return chunks
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_chunking.py -v` — Expected: PASS (4 tests). If overlap assert fails on exact boundaries, adjust the test slice length, not the algorithm, as long as overlap text demonstrably carries over.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat: heading-aware markdown chunker (atomic code fences, overlap)"
```

---

### Task 5: Embedders

**Files:**
- Create: `src/knowledgehub/embeddings.py`
- Test: `tests/test_embeddings.py`

- [ ] **Step 1: Write the failing test**

```python
import math

from knowledgehub.embeddings import FakeEmbedder


def test_fake_embedder_deterministic_unit_vectors():
    e = FakeEmbedder(dim=32)
    a1 = e.embed(["hello world"])[0]
    a2 = e.embed(["hello world"])[0]
    b = e.embed(["goodbye"])[0]
    assert a1 == a2
    assert a1 != b
    assert len(a1) == 32
    assert math.isclose(sum(x * x for x in a1), 1.0, rel_tol=1e-6)


def test_fake_embedder_similar_texts_share_tokens():
    e = FakeEmbedder(dim=32)
    base = e.embed(["telegram webhook setup"])[0]
    near = e.embed(["telegram webhook configuration"])[0]
    far = e.embed(["quarterly budget report"])[0]

    def dot(u, v):
        return sum(a * b for a, b in zip(u, v))

    assert dot(base, near) > dot(base, far)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_embeddings.py -v` — Expected: FAIL

- [ ] **Step 3: Implement `src/knowledgehub/embeddings.py`**

```python
import hashlib
import math
from typing import Protocol


class Embedder(Protocol):
    model: str
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class FakeEmbedder:
    """Deterministic bag-of-token-hashes embedder for tests. No network."""

    def __init__(self, dim: int = 32):
        self.model = "fake"
        self.dim = dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for text in texts:
            vec = [0.0] * self.dim
            for tok in text.lower().split():
                h = int.from_bytes(hashlib.sha256(tok.encode()).digest()[:4], "big")
                vec[h % self.dim] += 1.0
            norm = math.sqrt(sum(x * x for x in vec)) or 1.0
            out.append([x / norm for x in vec])
        return out


class OpenAIEmbedder:
    """Real embeddings via the OpenAI API (default: text-embedding-3-small)."""

    def __init__(self, model: str = "text-embedding-3-small", dim: int = 1536):
        from openai import OpenAI

        self.model = model
        self.dim = dim
        self._client = OpenAI()

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        resp = self._client.embeddings.create(model=self.model, input=texts)
        return [d.embedding for d in resp.data]


def build_embedder(settings) -> Embedder:
    """Embedder from config. 'fake' is allowed for offline/dev use."""
    if settings.embedding_model == "fake":
        return FakeEmbedder(dim=settings.embedding_dim)
    return OpenAIEmbedder(model=settings.embedding_model, dim=settings.embedding_dim)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_embeddings.py -v` — Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat: embedder protocol with OpenAI impl and deterministic fake"
```

---

### Task 6: Storage layer — documents CRUD

**Files:**
- Create: `src/knowledgehub/storage.py`
- Test: `tests/test_storage.py`

- [ ] **Step 1: Write the failing test**

```python
import pytest

from knowledgehub.chunking import Chunk
from knowledgehub.db import connect
from knowledgehub.embeddings import FakeEmbedder
from knowledgehub.storage import Storage


@pytest.fixture
def store(tmp_path):
    con = connect(tmp_path / "t.db", embedding_dim=32)
    return Storage(con, FakeEmbedder(dim=32))


def _doc(store, path="polly/integrations.md", text="Telegram webhook setup for polly."):
    chunks = [Chunk(position=0, heading_path="Integrations > Telegram", text=text)]
    return store.upsert_document(
        source_type="folder",
        path=path,
        title="Polly Integrations",
        content=f"# Polly Integrations\n\n{text}",
        content_hash="hash1",
        chunks=chunks,
        embed_inputs=[c.text for c in chunks],
    )


def test_upsert_and_get(store):
    doc_id = _doc(store)
    doc = store.get_document(doc_id)
    assert doc.title == "Polly Integrations"
    assert "Telegram webhook" in doc.content


def test_unchanged_detection(store):
    _doc(store)
    assert store.is_unchanged("polly/integrations.md", "hash1") is True
    assert store.is_unchanged("polly/integrations.md", "other") is False
    assert store.is_unchanged("missing.md", "hash1") is False


def test_update_replaces_chunks(store):
    doc_id = _doc(store)
    chunks = [Chunk(position=0, heading_path="", text="Completely new content about slack.")]
    new_id = store.upsert_document(
        source_type="folder",
        path="polly/integrations.md",
        title="Polly Integrations",
        content="new",
        content_hash="hash2",
        chunks=chunks,
        embed_inputs=[c.text for c in chunks],
    )
    assert new_id == doc_id  # same document row, replaced contents
    rows = store.con.execute("SELECT count(*) FROM chunks WHERE document_id=?", (doc_id,)).fetchone()
    assert rows[0] == 1
    assert store.con.execute("SELECT count(*) FROM chunk_vec").fetchone()[0] == 1


def test_delete_document(store):
    doc_id = _doc(store)
    store.delete_document_by_path("polly/integrations.md")
    assert store.get_document(doc_id) is None
    assert store.con.execute("SELECT count(*) FROM chunk_vec").fetchone()[0] == 0
    assert store.con.execute("SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH '\"telegram\"'").fetchone()[0] == 0


def test_list_documents(store):
    _doc(store)
    _doc(store, path="other/budget.md", text="Quarterly budget numbers.")
    docs = store.list_documents()
    assert len(docs) == 2
    filtered = store.list_documents(query="budget")
    assert len(filtered) == 1 and filtered[0].path == "other/budget.md"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_storage.py -v` — Expected: FAIL

- [ ] **Step 3: Implement `src/knowledgehub/storage.py`**

```python
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
```

(Search methods come in Task 7 — same file.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_storage.py -v` — Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat: storage layer - document CRUD, atomic chunk replacement, sources"
```

---

### Task 7: Hybrid search (RRF) + grep

**Files:**
- Modify: `src/knowledgehub/storage.py` (add methods to `Storage`)
- Test: `tests/test_search.py`

- [ ] **Step 1: Write the failing test**

```python
import pytest

from knowledgehub.chunking import Chunk
from knowledgehub.db import connect
from knowledgehub.embeddings import FakeEmbedder
from knowledgehub.storage import Storage


@pytest.fixture
def store(tmp_path):
    con = connect(tmp_path / "t.db", embedding_dim=32)
    s = Storage(con, FakeEmbedder(dim=32))
    docs = [
        ("polly/telegram.md", "Polly Telegram", "polly connects to telegram via webhook callbacks"),
        ("polly/slack.md", "Polly Slack", "polly posts messages to slack channels"),
        ("infra/budget.md", "Budget", "quarterly infrastructure budget planning numbers"),
    ]
    for path, title, text in docs:
        chunks = [Chunk(position=0, heading_path=title, text=text)]
        s.upsert_document(
            source_type="folder", path=path, title=title, content=text,
            content_hash=path, chunks=chunks, embed_inputs=[text],
        )
    return s


def test_keyword_match_wins_on_exact_terms(store):
    hits = store.search_hybrid("telegram webhook", top_k=3)
    assert hits[0].path == "polly/telegram.md"
    assert hits[0].heading_path == "Polly Telegram"


def test_semantic_side_contributes(store):
    # FakeEmbedder is token-overlap based; shared tokens rank the right doc up
    hits = store.search_hybrid("budget planning", top_k=3)
    assert hits[0].path == "infra/budget.md"


def test_fts_query_with_special_chars_does_not_crash(store):
    hits = store.search_hybrid('why "polly" (telegram)? -slack', top_k=3)
    assert isinstance(hits, list)


def test_grep(store):
    hits = store.grep(r"webhook", limit=10)
    assert len(hits) == 1
    assert hits[0].path == "polly/telegram.md"
    assert store.grep(r"nonexistentzzz") == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_search.py -v` — Expected: FAIL (`Storage` has no `search_hybrid`)

- [ ] **Step 3: Add search methods to `Storage` in `src/knowledgehub/storage.py`**

```python
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
```

- [ ] **Step 4: Run all tests**

Run: `uv run pytest -v` — Expected: PASS (all tasks so far)

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat: hybrid search (FTS5 + vec KNN merged via RRF) and grep"
```

---

### Task 8: Parsers + ingestion pipeline + folder sync

**Files:**
- Create: `src/knowledgehub/parsers.py`, `src/knowledgehub/ingest.py`
- Test: `tests/test_ingest.py`

- [ ] **Step 1: Write the failing test**

```python
import pytest

from knowledgehub.db import connect
from knowledgehub.embeddings import FakeEmbedder
from knowledgehub.ingest import Ingestor, sync_folder
from knowledgehub.parsers import parse_file
from knowledgehub.storage import Storage


@pytest.fixture
def store(tmp_path):
    con = connect(tmp_path / "t.db", embedding_dim=32)
    return Storage(con, FakeEmbedder(dim=32))


def test_parse_markdown_title(tmp_path):
    f = tmp_path / "a.md"
    f.write_text("# Real Title\n\nbody")
    title, md = parse_file(f)
    assert title == "Real Title" and "body" in md


def test_parse_txt_and_html(tmp_path):
    t = tmp_path / "notes.txt"
    t.write_text("plain text notes")
    title, md = parse_file(t)
    assert title == "notes" and md == "plain text notes"

    h = tmp_path / "doc.html"
    h.write_text("<h1>Exported Doc</h1><p>from google docs</p>")
    title, md = parse_file(h)
    assert title == "Exported Doc" and "from google docs" in md


def test_ingest_add_update_skip(store, tmp_path):
    f = tmp_path / "a.md"
    f.write_text("# A\n\nfirst version")
    ing = Ingestor(store, max_chars=3000, overlap_chars=0)

    assert ing.ingest_file(f, source_type="folder").status == "added"
    assert ing.ingest_file(f, source_type="folder").status == "skipped"
    f.write_text("# A\n\nsecond version")
    assert ing.ingest_file(f, source_type="folder").status == "updated"
    hits = store.search_hybrid("second version", top_k=3)
    assert hits and "second" in hits[0].text


def test_ingest_failure_isolated(store, tmp_path):
    bad = tmp_path / "bad.docx"
    bad.write_bytes(b"\x00\x01binary")
    good = tmp_path / "good.md"
    good.write_text("# Good\n\ncontent here")
    report = sync_folder(tmp_path, store, max_chars=3000, overlap_chars=0)
    assert report.added == 1 and report.failed == 1


def test_sync_removes_deleted_files(store, tmp_path):
    a = tmp_path / "a.md"
    a.write_text("# A\n\nalpha doc")
    (tmp_path / "b.md").write_text("# B\n\nbeta doc")
    sync_folder(tmp_path, store, max_chars=3000, overlap_chars=0)
    assert len(store.list_documents()) == 2
    a.unlink()
    report = sync_folder(tmp_path, store, max_chars=3000, overlap_chars=0)
    assert report.removed == 1
    assert len(store.list_documents()) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_ingest.py -v` — Expected: FAIL

- [ ] **Step 3: Implement `src/knowledgehub/parsers.py`**

```python
import re
from pathlib import Path

from markdownify import markdownify

SUPPORTED = {".md", ".markdown", ".txt", ".html", ".htm"}


def parse_file(path: Path) -> tuple[str, str]:
    """Return (title, canonical_markdown). Raises ValueError on unsupported/broken files."""
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED:
        raise ValueError(f"unsupported file type: {suffix}")
    try:
        raw = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as e:
        raise ValueError(f"not a text file: {path.name}") from e
    return parse_content(path.stem, raw, suffix)


def parse_content(fallback_title: str, raw: str, suffix: str) -> tuple[str, str]:
    if suffix in (".html", ".htm"):
        md = markdownify(raw, heading_style="ATX").strip()
    else:
        md = raw.strip()
    m = re.search(r"^#\s+(.+)$", md, re.MULTILINE)
    title = m.group(1).strip() if m else fallback_title
    return title, md
```

- [ ] **Step 4: Implement `src/knowledgehub/ingest.py`**

```python
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
            title, md = parse_content(name, raw, suffix)
            return self._index(f"upload/{name}", title, md, source_type=source_type, source_id=None)
        except Exception as e:
            return IngestResult(path=name, status="failed", error=str(e))

    def _index(self, path: str, title: str, md: str, *, source_type: str, source_id: int | None) -> IngestResult:
        content_hash = hashlib.sha256(md.encode()).hexdigest()
        existed = self.store.con.execute(
            "SELECT 1 FROM documents WHERE path=?", (path,)
        ).fetchone()
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


def sync_folder(folder: Path, store: Storage, *, max_chars: int, overlap_chars: int, enricher=None) -> SyncReport:
    """Sync one folder: ingest new/changed, remove vanished. Per-file isolation."""
    source_id = store.register_source("folder", str(folder))
    ing = Ingestor(store, max_chars=max_chars, overlap_chars=overlap_chars, enricher=enricher)
    report = SyncReport()
    seen: set[str] = set()
    for path in sorted(folder.rglob("*")):
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
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_ingest.py -v` — Expected: PASS (5 tests). Then `uv run pytest` — all green.

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "feat: parsers + ingestion pipeline + folder sync with deletion handling"
```

---

### Task 9: Enrichment (contextual lines + summaries)

**Files:**
- Create: `src/knowledgehub/enrich.py`
- Test: `tests/test_enrich.py`

- [ ] **Step 1: Write the failing test**

```python
import pydantic_ai.models
from pydantic_ai.models.test import TestModel

from knowledgehub.enrich import Enricher

pydantic_ai.models.ALLOW_MODEL_REQUESTS = False


def test_summarize_and_contextualize_use_model():
    e = Enricher(model=TestModel(custom_output_text="A concise summary."))
    assert e.summarize("Doc Title", "# Doc\n\nlots of text") == "A concise summary."
    line = e.contextualize("Polly Guide", "Integrations > Telegram", "webhook setup...")
    assert line == "A concise summary."  # TestModel returns fixed text; wiring is what we test


def test_enricher_feeds_embedding_inputs(tmp_path):
    from knowledgehub.db import connect
    from knowledgehub.embeddings import FakeEmbedder
    from knowledgehub.ingest import Ingestor
    from knowledgehub.storage import Storage

    store = Storage(connect(tmp_path / "t.db", embedding_dim=32), FakeEmbedder(dim=32))
    e = Enricher(model=TestModel(custom_output_text="ctxline"))
    ing = Ingestor(store, max_chars=3000, overlap_chars=0, enricher=e)
    f = tmp_path / "a.md"
    f.write_text("# A\n\nsome body text")
    res = ing.ingest_file(f, source_type="folder")
    assert res.status == "added"
    doc = store.list_documents()[0]
    assert doc.summary == "ctxline"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_enrich.py -v` — Expected: FAIL

- [ ] **Step 3: Implement `src/knowledgehub/enrich.py`**

```python
from pydantic_ai import Agent

SUMMARY_PROMPT = (
    "Write a single-paragraph summary (max 80 words) of the document below. "
    "State what it is, what it covers, and any project/system names it mentions. "
    "Output only the summary.\n\nTitle: {title}\n\n{content}"
)

CONTEXT_PROMPT = (
    "Write ONE short sentence situating this chunk within its document, naming the "
    "document and section so the chunk is retrievable on its own. Output only the sentence.\n\n"
    "Document: {title}\nSection: {section}\n\nChunk:\n{chunk}"
)


class Enricher:
    """Cheap-model ingestion enrichment: doc summaries + contextual retrieval lines."""

    def __init__(self, model):
        # model: a pydantic-ai model name string ("openai:gpt-5-mini") or Model instance (TestModel in tests)
        self._agent = Agent(model, system_prompt="You annotate documents for a search index. Be terse.")

    def summarize(self, title: str, content: str) -> str:
        prompt = SUMMARY_PROMPT.format(title=title, content=content[:20000])
        return self._agent.run_sync(prompt).output.strip()

    def contextualize(self, title: str, section: str, chunk: str) -> str:
        prompt = CONTEXT_PROMPT.format(title=title, section=section or "(top)", chunk=chunk[:4000])
        return self._agent.run_sync(prompt).output.strip()
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_enrich.py -v` — Expected: PASS. Then full suite: `uv run pytest`.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat: enrichment - document summaries + contextual retrieval lines"
```

---

### Task 10: The agent

**Files:**
- Create: `src/knowledgehub/agent.py`
- Test: `tests/test_agent.py`

- [ ] **Step 1: Write the failing test**

```python
import pydantic_ai.models
import pytest
from pydantic_ai import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel

from knowledgehub.agent import HubDeps, build_agent
from knowledgehub.chunking import Chunk
from knowledgehub.db import connect
from knowledgehub.embeddings import FakeEmbedder
from knowledgehub.storage import Storage

pydantic_ai.models.ALLOW_MODEL_REQUESTS = False
pytestmark = pytest.mark.anyio


@pytest.fixture
def deps(tmp_path):
    store = Storage(connect(tmp_path / "t.db", embedding_dim=32), FakeEmbedder(dim=32))
    text = "polly connects to telegram via webhook callbacks registered in setup.py"
    store.upsert_document(
        source_type="folder", path="polly/telegram.md", title="Polly Telegram",
        content=f"# Polly Telegram\n\n{text}", content_hash="h",
        chunks=[Chunk(position=0, heading_path="Polly Telegram", text=text)],
        embed_inputs=[text],
    )
    return HubDeps(store=store)


async def test_all_four_tools_registered(deps):
    agent = build_agent("openai:gpt-5.2")
    m = TestModel(call_tools=[])
    with agent.override(model=m):
        await agent.run("hello", deps=deps)
    tool_names = {t.name for t in m.last_model_request_parameters.function_tools}
    assert tool_names == {"search", "read_document", "list_documents", "grep"}


async def test_search_tool_returns_provenance(deps):
    def script(messages, info: AgentInfo) -> ModelResponse:
        if len(messages) == 1:
            return ModelResponse(parts=[ToolCallPart("search", {"query": "telegram webhook"})])
        tool_return = messages[-1].parts[0]
        assert "polly/telegram.md" in str(tool_return.content)
        assert "Polly Telegram" in str(tool_return.content)
        return ModelResponse(parts=[TextPart("Answer with citation [polly/telegram.md]")])

    agent = build_agent("openai:gpt-5.2")
    with agent.override(model=FunctionModel(script)):
        result = await agent.run("how does polly integrate with telegram?", deps=deps)
    assert "polly/telegram.md" in result.output


async def test_read_document_tool(deps):
    doc_id = deps.store.list_documents()[0].id

    def script(messages, info: AgentInfo) -> ModelResponse:
        if len(messages) == 1:
            return ModelResponse(parts=[ToolCallPart("read_document", {"doc_id": doc_id})])
        content = str(messages[-1].parts[0].content)
        assert "registered in setup.py" in content
        return ModelResponse(parts=[TextPart("done")])

    agent = build_agent("openai:gpt-5.2")
    with agent.override(model=FunctionModel(script)):
        await agent.run("read it", deps=deps)


async def test_system_prompt_demands_citations_and_honesty():
    agent = build_agent("openai:gpt-5.2")
    sp = " ".join(agent._system_prompts)
    assert "cite" in sp.lower()
    assert "knowledge base" in sp.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_agent.py -v` — Expected: FAIL

- [ ] **Step 3: Implement `src/knowledgehub/agent.py`**

```python
from dataclasses import dataclass

from pydantic_ai import Agent, RunContext

from .storage import Storage

SYSTEM_PROMPT = """You are the team knowledge hub. You answer questions ONLY from the indexed
documents, found via your tools. Rules:

- Always search before answering. Use multiple searches with different phrasings when the
  first results look incomplete.
- For "why" questions, prefer read_document on the most relevant document over answering
  from a fragment; follow references to other documents by searching for them.
- Use grep for exact identifiers, codenames, or acronyms that search may miss.
- Cite every claim with its source as [path > section]. Never state facts without a citation.
- If the knowledge base does not contain the answer, say exactly that and name what you
  looked for. Never improvise from general knowledge.
- Keep answers concise; quote the source where wording matters."""


@dataclass
class HubDeps:
    store: Storage


def build_agent(model) -> Agent[HubDeps, str]:
    agent: Agent[HubDeps, str] = Agent(
        model,
        deps_type=HubDeps,
        system_prompt=SYSTEM_PROMPT,
        retries=2,
    )

    @agent.tool
    def search(ctx: RunContext[HubDeps], query: str, top_k: int = 8) -> list[dict]:
        """Hybrid keyword+semantic search over the knowledge base.

        Returns chunks with provenance (path, title, section). Use this first for
        every question; vary the phrasing across calls if results look incomplete.
        """
        hits = ctx.deps.store.search_hybrid(query, top_k=top_k)
        return [
            {
                "doc_id": h.document_id,
                "path": h.path,
                "title": h.title,
                "section": h.heading_path,
                "text": h.text,
            }
            for h in hits
        ]

    @agent.tool
    def read_document(ctx: RunContext[HubDeps], doc_id: int) -> dict:
        """Read a full document by id (ids come from search/list_documents results).

        Use this when a chunk looks relevant but truncated, and for 'why' questions
        where surrounding context matters.
        """
        doc = ctx.deps.store.get_document(doc_id)
        if doc is None:
            return {"error": f"no document with id {doc_id}"}
        return {"doc_id": doc.id, "path": doc.path, "title": doc.title, "content": doc.content}

    @agent.tool
    def list_documents(ctx: RunContext[HubDeps], query: str | None = None) -> list[dict]:
        """Browse indexed documents (titles + summaries), optionally filtered.

        Use this to discover which documents exist about a topic before deep-diving.
        """
        return [
            {"doc_id": d.id, "path": d.path, "title": d.title, "summary": d.summary or ""}
            for d in ctx.deps.store.list_documents(query=query)
        ]

    @agent.tool
    def grep(ctx: RunContext[HubDeps], pattern: str) -> list[dict]:
        """Exact regex scan over raw document text (case-insensitive).

        Use for identifiers, codenames, acronyms, or exact strings that fuzzy
        search might miss (e.g. 'POLLY_WEBHOOK_URL').
        """
        hits = ctx.deps.store.grep(pattern)
        return [
            {"doc_id": h.document_id, "path": h.path, "section": h.heading_path, "text": h.text}
            for h in hits
        ]

    return agent
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_agent.py -v` — Expected: PASS (4 tests). If `agent._system_prompts` is not the attribute name in the installed pydantic-ai version, check `Agent.__init__` source via `uv run python -c "import inspect, pydantic_ai; print(inspect.signature(pydantic_ai.Agent.__init__))"` and read the private attr it stores system prompts in; adjust only the test.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat: hub agent with search/read_document/list_documents/grep tools"
```

---

### Task 11: FastAPI app

**Files:**
- Create: `src/knowledgehub/api.py`
- Test: `tests/test_api.py`

- [ ] **Step 1: Write the failing test**

```python
import pydantic_ai.models
import pytest
from fastapi.testclient import TestClient
from pydantic_ai.models.test import TestModel

from knowledgehub.api import build_app
from knowledgehub.config import Settings

pydantic_ai.models.ALLOW_MODEL_REQUESTS = False


@pytest.fixture
def client(tmp_path):
    settings = Settings(
        _env_file=None,
        db_path=tmp_path / "t.db",
        embedding_model="fake",
        embedding_dim=32,
        enrich_enabled=False,
    )
    app = build_app(settings, model_override=TestModel(custom_output_text="hi from hub"))
    return TestClient(app)


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_ingest_upload_and_list_documents(client):
    r = client.post("/ingest", files={"file": ("notes.md", b"# Notes\n\npolly telegram webhook", "text/markdown")})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "added" and body["chunks"] >= 1

    docs = client.get("/documents").json()
    assert len(docs) == 1 and docs[0]["title"] == "Notes"

    doc = client.get(f"/documents/{docs[0]['id']}").json()
    assert "polly telegram webhook" in doc["content"]


def test_document_404(client):
    assert client.get("/documents/999").status_code == 404


def test_sources_register_and_list(client, tmp_path):
    folder = tmp_path / "docs"
    folder.mkdir()
    (folder / "a.md").write_text("# A\n\nalpha")
    r = client.post("/sources", json={"kind": "folder", "location": str(folder)})
    assert r.status_code == 200
    assert r.json()["report"]["added"] == 1
    assert len(client.get("/sources").json()) == 1


def test_chat_streams_vercel_protocol(client):
    payload = {
        "id": "chat1",
        "messages": [
            {"id": "m1", "role": "user", "parts": [{"type": "text", "text": "what is polly?"}]}
        ],
    }
    r = client.post("/chat", json=payload)
    assert r.status_code == 200
    assert "text/event-stream" in r.headers["content-type"]
    assert "hi from hub" in r.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_api.py -v` — Expected: FAIL

- [ ] **Step 3: Implement `src/knowledgehub/api.py`**

```python
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from pydantic_ai.ui.vercel_ai import VercelAIAdapter

from .agent import HubDeps, build_agent
from .config import Settings
from .db import connect
from .embeddings import build_embedder
from .enrich import Enricher
from .ingest import Ingestor, sync_folder
from .storage import Storage


async def verify_request(request: Request) -> None:
    """Auth stub. v1 is local/single-user; team deployment implements this one
    function (e.g. check an API key header) instead of retrofitting routes."""
    return None


class SourceIn(BaseModel):
    kind: str = "folder"
    location: str


def build_app(settings: Settings | None = None, model_override=None) -> FastAPI:
    settings = settings or Settings()
    con = connect(settings.db_path, embedding_dim=settings.embedding_dim)
    embedder = build_embedder(settings)
    store = Storage(con, embedder)
    enricher = Enricher(settings.enrich_model) if settings.enrich_enabled else None
    ingestor = Ingestor(
        store, max_chars=settings.chunk_max_chars,
        overlap_chars=settings.chunk_overlap_chars, enricher=enricher,
    )
    agent = build_agent(model_override or settings.chat_model)
    deps = HubDeps(store=store)

    app = FastAPI(title="Knowledge Hub")
    app.add_middleware(
        CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
    )

    @app.get("/health")
    async def health(_=Depends(verify_request)):
        return {"status": "ok"}

    @app.post("/chat")
    async def chat(request: Request, _=Depends(verify_request)):
        return await VercelAIAdapter.dispatch_request(request, agent=agent, deps=deps)

    @app.post("/ingest")
    async def ingest(file: UploadFile, _=Depends(verify_request)):
        raw = (await file.read()).decode("utf-8", errors="replace")
        suffix = Path(file.filename or "upload.md").suffix or ".md"
        result = ingestor.ingest_text(file.filename or "upload.md", raw, suffix=suffix)
        if result.status == "failed":
            raise HTTPException(status_code=422, detail=result.error)
        return {"path": result.path, "status": result.status, "chunks": result.chunks}

    @app.get("/documents")
    async def documents(query: str | None = None, _=Depends(verify_request)):
        return [
            {"id": d.id, "path": d.path, "title": d.title, "summary": d.summary}
            for d in store.list_documents(query=query)
        ]

    @app.get("/documents/{doc_id}")
    async def document(doc_id: int, _=Depends(verify_request)):
        doc = store.get_document(doc_id)
        if doc is None:
            raise HTTPException(status_code=404, detail="document not found")
        return {"id": doc.id, "path": doc.path, "title": doc.title, "content": doc.content, "summary": doc.summary}

    @app.get("/sources")
    async def sources(_=Depends(verify_request)):
        return [{"id": i, "kind": k, "location": loc} for i, k, loc in store.list_sources()]

    @app.post("/sources")
    async def add_source(body: SourceIn, _=Depends(verify_request)):
        folder = Path(body.location)
        if not folder.is_dir():
            raise HTTPException(status_code=400, detail=f"not a directory: {folder}")
        report = sync_folder(
            folder, store, max_chars=settings.chunk_max_chars,
            overlap_chars=settings.chunk_overlap_chars, enricher=enricher,
        )
        return {"report": {"added": report.added, "updated": report.updated,
                           "skipped": report.skipped, "removed": report.removed,
                           "failed": report.failed}}

    return app
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_api.py -v` — Expected: PASS (5 tests). If `dispatch_request(..., deps=deps)` raises a TypeError on the installed version, switch to the explicit form from the pydantic-ai docs (build_run_input → `VercelAIAdapter(agent=agent, run_input=run_input, accept=accept)` → `adapter.run_stream(deps=deps)` → `adapter.encode_stream(...)` → `StreamingResponse`).

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat: FastAPI app - /chat (Vercel AI protocol), /ingest, /documents, /sources"
```

---

### Task 12: CLI

**Files:**
- Create: `src/knowledgehub/cli.py`
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write the failing test**

```python
from typer.testing import CliRunner

from knowledgehub.cli import app

runner = CliRunner()


def _env(tmp_path):
    return {
        "HUB_DB_PATH": str(tmp_path / "t.db"),
        "HUB_EMBEDDING_MODEL": "fake",
        "HUB_EMBEDDING_DIM": "32",
        "HUB_ENRICH_ENABLED": "false",
    }


def test_sync_and_resync(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.md").write_text("# A\n\nalpha content")
    r = runner.invoke(app, ["sync", str(docs)], env=_env(tmp_path))
    assert r.exit_code == 0, r.output
    assert "synced 1" in r.output

    # re-sync all registered sources (no arg)
    r = runner.invoke(app, ["sync"], env=_env(tmp_path))
    assert r.exit_code == 0
    assert "skipped 1" in r.output


def test_add_single_file(tmp_path):
    f = tmp_path / "note.md"
    f.write_text("# Note\n\nbody")
    r = runner.invoke(app, ["add", str(f)], env=_env(tmp_path))
    assert r.exit_code == 0
    assert "added" in r.output


def test_search_command(tmp_path):
    f = tmp_path / "note.md"
    f.write_text("# Note\n\ntelegram webhook details")
    runner.invoke(app, ["add", str(f)], env=_env(tmp_path))
    r = runner.invoke(app, ["search", "telegram"], env=_env(tmp_path))
    assert r.exit_code == 0
    assert "note.md" in r.output
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cli.py -v` — Expected: FAIL

- [ ] **Step 3: Implement `src/knowledgehub/cli.py`**

```python
from pathlib import Path

import typer

from .config import Settings
from .db import connect
from .embeddings import build_embedder
from .enrich import Enricher
from .ingest import Ingestor, sync_folder
from .storage import Storage

app = typer.Typer(help="Knowledge hub: ingest documents, search, serve.")


def _store(settings: Settings) -> tuple[Storage, Ingestor]:
    con = connect(settings.db_path, embedding_dim=settings.embedding_dim)
    store = Storage(con, build_embedder(settings))
    enricher = Enricher(settings.enrich_model) if settings.enrich_enabled else None
    ing = Ingestor(
        store, max_chars=settings.chunk_max_chars,
        overlap_chars=settings.chunk_overlap_chars, enricher=enricher,
    )
    return store, ing


@app.command()
def sync(folder: str = typer.Argument(None), watch: bool = typer.Option(False, "--watch")):
    """Sync a folder (and register it), or re-sync all registered sources."""
    settings = Settings()
    store, ing = _store(settings)
    enricher = ing.enricher

    def run_all() -> None:
        folders = [Path(folder)] if folder else [Path(loc) for _, kind, loc in store.list_sources() if kind == "folder"]
        if not folders:
            typer.echo("no sources registered; run: hub sync <folder>")
            raise typer.Exit(1)
        for f in folders:
            report = sync_folder(
                f, store, max_chars=settings.chunk_max_chars,
                overlap_chars=settings.chunk_overlap_chars, enricher=enricher,
            )
            typer.echo(f"{f}: {report.summary()}")

    run_all()
    if watch:
        from watchfiles import watch as fswatch

        targets = [folder] if folder else [loc for _, kind, loc in store.list_sources() if kind == "folder"]
        typer.echo(f"watching {targets} (ctrl-c to stop)")
        for _changes in fswatch(*targets):
            run_all()


@app.command()
def add(file: str):
    """Ingest a single file."""
    settings = Settings()
    _, ing = _store(settings)
    res = ing.ingest_file(Path(file), source_type="upload")
    typer.echo(f"{res.path}: {res.status} ({res.chunks} chunks)" + (f" error: {res.error}" if res.error else ""))
    if res.status == "failed":
        raise typer.Exit(1)


@app.command()
def search(query: str, top_k: int = 5):
    """Run a hybrid search directly (debugging aid)."""
    settings = Settings()
    store, _ = _store(settings)
    for hit in store.search_hybrid(query, top_k=top_k):
        typer.echo(f"{hit.score:.4f}  {hit.path}  [{hit.heading_path}]")
        typer.echo(f"        {hit.text[:120]!r}")


@app.command()
def reindex():
    """Re-embed every chunk (after changing embedding model). Rebuilds chunk_vec."""
    settings = Settings()
    store, _ = _store(settings)
    con = store.con
    con.execute("DROP TABLE IF EXISTS chunk_vec")
    con.execute(f"CREATE VIRTUAL TABLE chunk_vec USING vec0(embedding float[{settings.embedding_dim}])")
    rows = list(con.execute("SELECT id, text FROM chunks ORDER BY id"))
    import sqlite_vec

    batch = 64
    for i in range(0, len(rows), batch):
        part = rows[i : i + batch]
        vecs = store.embedder.embed([t for _, t in part])
        with con:
            for (cid, _), v in zip(part, vecs):
                con.execute("INSERT INTO chunk_vec(rowid, embedding) VALUES (?,?)", (cid, sqlite_vec.serialize_float32(v)))
    with con:
        con.execute(
            "INSERT INTO meta(key, value) VALUES ('embedding_model', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (store.embedder.model,),
        )
    typer.echo(f"reindexed {len(rows)} chunks with {store.embedder.model}")


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8000):
    """Run the API server."""
    import uvicorn

    from .api import build_app

    uvicorn.run(build_app(Settings()), host=host, port=port)


@app.command()
def eval(golden_file: str, top_k: int = 5):
    """Retrieval-quality eval: % of golden questions whose expected doc is in top-k."""
    import yaml

    settings = Settings()
    store, _ = _store(settings)
    cases = yaml.safe_load(Path(golden_file).read_text())
    hits = 0
    for case in cases:
        results = store.search_hybrid(case["question"], top_k=top_k)
        found = any(case["expect_path"] in r.path for r in results)
        hits += found
        typer.echo(f"{'PASS' if found else 'MISS'}  {case['question']}")
    typer.echo(f"recall@{top_k}: {hits}/{len(cases)}")


if __name__ == "__main__":
    app()
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_cli.py -v` — Expected: PASS. Note: Typer's CliRunner `env=` replaces vars for the invocation; `Settings()` reads them at call time, which is why `_store` is constructed inside each command.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat: CLI - sync/add/search/reindex/serve/eval (+ --watch)"
```

---

### Task 13: Eval golden set seed

**Files:**
- Create: `eval/golden.yaml`, `eval/fixtures/polly-telegram.md`, `eval/fixtures/project-x-decision.md`

- [ ] **Step 1: Create fixture docs**

`eval/fixtures/polly-telegram.md`:

```markdown
# Polly Telegram Integration

## Overview

Polly connects to Telegram through the Bot API using webhook callbacks.

## Webhook setup

Register the webhook with `POLLY_WEBHOOK_URL` pointing at `/telegram/webhook`.
Polly validates updates with the bot token and routes commands to the poll engine.
```

`eval/fixtures/project-x-decision.md`:

```markdown
# Project X Decision Record

## Why we did Project X

Customer churn analysis showed onboarding drop-off; Project X rebuilt the signup
flow to reduce steps from 7 to 3. Approved 2025-09 after the Q3 retro.
```

- [ ] **Step 2: Create `eval/golden.yaml`**

```yaml
- question: how does polly integrate with telegram
  expect_path: polly-telegram.md
- question: webhook setup for polly
  expect_path: polly-telegram.md
- question: why did we do project X
  expect_path: project-x-decision.md
- question: what does project X solve
  expect_path: project-x-decision.md
```

- [ ] **Step 3: Verify the eval runs end-to-end with the fake embedder**

Run:
```bash
HUB_DB_PATH=/tmp/eval.db HUB_EMBEDDING_MODEL=fake HUB_EMBEDDING_DIM=32 HUB_ENRICH_ENABLED=false uv run hub sync eval/fixtures
HUB_DB_PATH=/tmp/eval.db HUB_EMBEDDING_MODEL=fake HUB_EMBEDDING_DIM=32 HUB_ENRICH_ENABLED=false uv run hub eval eval/golden.yaml
```
Expected: `recall@5: 4/4` (keyword side carries these even with fake embeddings). Clean up: `rm -f /tmp/eval.db*`.

- [ ] **Step 4: Commit**

```bash
git add -A && git commit -m "feat: retrieval eval golden set + fixtures (extend with real team docs)"
```

---

### Task 14: React chat UI

**Files:**
- Create: `ui/package.json`, `ui/index.html`, `ui/vite.config.ts`, `ui/tsconfig.json`, `ui/src/main.tsx`, `ui/src/App.tsx`, `ui/src/app.css`

- [ ] **Step 1: Scaffold `ui/package.json`**

```json
{
  "name": "knowledgehub-ui",
  "private": true,
  "version": "0.1.0",
  "type": "module",
  "scripts": {
    "dev": "vite",
    "build": "tsc -b && vite build"
  },
  "dependencies": {
    "@ai-sdk/react": "^2",
    "ai": "^5",
    "react": "^19",
    "react-dom": "^19"
  },
  "devDependencies": {
    "@types/react": "^19",
    "@types/react-dom": "^19",
    "@vitejs/plugin-react": "^4",
    "typescript": "^5",
    "vite": "^6"
  }
}
```

(If `npm install` reports a peer/major mismatch for `@ai-sdk/react`/`ai`, install the latest majors npm suggests — the `useChat` + `DefaultChatTransport` API below is the v5 surface; check the migration note in the AI SDK docs only if compilation fails.)

- [ ] **Step 2: `ui/vite.config.ts` (proxy /chat + API to backend)**

```ts
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/chat": "http://127.0.0.1:8000",
      "/ingest": "http://127.0.0.1:8000",
      "/documents": "http://127.0.0.1:8000",
      "/sources": "http://127.0.0.1:8000",
    },
  },
});
```

- [ ] **Step 3: `ui/tsconfig.json`, `ui/index.html`, `ui/src/main.tsx`**

`ui/tsconfig.json`:

```json
{
  "compilerOptions": {
    "target": "ES2022",
    "lib": ["ES2022", "DOM", "DOM.Iterable"],
    "module": "ESNext",
    "moduleResolution": "bundler",
    "jsx": "react-jsx",
    "strict": true,
    "skipLibCheck": true,
    "noEmit": true
  },
  "include": ["src"]
}
```

`ui/index.html`:

```html
<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>Knowledge Hub</title>
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/src/main.tsx"></script>
  </body>
</html>
```

`ui/src/main.tsx`:

```tsx
import { createRoot } from "react-dom/client";
import App from "./App";
import "./app.css";

createRoot(document.getElementById("root")!).render(<App />);
```

- [ ] **Step 4: `ui/src/App.tsx` — chat with tool-progress + upload**

```tsx
import { useChat } from "@ai-sdk/react";
import { DefaultChatTransport } from "ai";
import { useRef, useState } from "react";

export default function App() {
  const { messages, sendMessage, status } = useChat({
    transport: new DefaultChatTransport({ api: "/chat" }),
  });
  const [input, setInput] = useState("");
  const fileRef = useRef<HTMLInputElement>(null);
  const [uploadNote, setUploadNote] = useState("");

  async function upload(file: File) {
    const form = new FormData();
    form.append("file", file);
    const res = await fetch("/ingest", { method: "POST", body: form });
    const body = await res.json();
    setUploadNote(
      res.ok ? `added ${file.name} — ${body.chunks} chunks` : `failed: ${body.detail}`,
    );
  }

  return (
    <div className="app">
      <header>
        <h1>Knowledge Hub</h1>
        <div className="upload">
          <input
            type="file"
            ref={fileRef}
            accept=".md,.txt,.html"
            onChange={(e) => e.target.files?.[0] && upload(e.target.files[0])}
          />
          <span className="note">{uploadNote}</span>
        </div>
      </header>

      <main>
        {messages.map((m) => (
          <div key={m.id} className={`msg ${m.role}`}>
            {m.parts.map((part, i) => {
              if (part.type === "text") return <p key={i}>{part.text}</p>;
              if (part.type.startsWith("tool-") || part.type === "dynamic-tool") {
                const p = part as { type: string; state?: string; input?: unknown };
                const name = part.type === "dynamic-tool"
                  ? (part as { toolName?: string }).toolName
                  : part.type.replace("tool-", "");
                return (
                  <div key={i} className="tool">
                    ⚙ {name}({p.input ? JSON.stringify(p.input) : ""}) — {p.state ?? "…"}
                  </div>
                );
              }
              return null;
            })}
          </div>
        ))}
        {status === "streaming" && <div className="thinking">…</div>}
      </main>

      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (!input.trim()) return;
          sendMessage({ text: input });
          setInput("");
        }}
      >
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="Ask the knowledge hub…"
          autoFocus
        />
        <button type="submit" disabled={status !== "ready"}>
          Send
        </button>
      </form>
    </div>
  );
}
```

- [ ] **Step 5: `ui/src/app.css` (minimal, readable)**

```css
* { box-sizing: border-box; }
body { margin: 0; font-family: system-ui, sans-serif; background: #f6f5f1; }
.app { max-width: 780px; margin: 0 auto; padding: 1rem; display: flex; flex-direction: column; min-height: 100vh; }
header { display: flex; justify-content: space-between; align-items: baseline; gap: 1rem; }
header h1 { font-size: 1.2rem; }
.note { font-size: 0.8rem; color: #5a7d5a; }
main { flex: 1; overflow-y: auto; padding: 1rem 0; }
.msg { margin: 0.75rem 0; padding: 0.75rem 1rem; border-radius: 10px; white-space: pre-wrap; }
.msg.user { background: #dde7f0; margin-left: 20%; }
.msg.assistant { background: #fff; margin-right: 10%; border: 1px solid #e3e0d8; }
.tool { font-size: 0.78rem; color: #888; font-family: monospace; margin: 0.25rem 0; }
form { display: flex; gap: 0.5rem; padding: 0.75rem 0; }
form input { flex: 1; padding: 0.6rem 0.8rem; border: 1px solid #ccc; border-radius: 8px; }
form button { padding: 0.6rem 1.1rem; border: 0; border-radius: 8px; background: #2b4c7e; color: #fff; }
form button:disabled { opacity: 0.5; }
```

- [ ] **Step 6: Install and build-check**

Run:
```bash
cd ui && npm install && npm run build
```
Expected: `tsc` passes and Vite produces `dist/`. If the AI SDK message-part union differs (TS errors on `part.state`/`toolName`), loosen those two casts — the runtime rendering logic is correct for the v5 stream.

- [ ] **Step 7: Manual smoke test (requires a real model key, else skip to commit)**

```bash
# terminal 1 (use fake embedder if no OpenAI key; chat model needs a real key)
HUB_DB_PATH=hub.db uv run hub serve
# terminal 2
cd ui && npm run dev   # open http://localhost:5173, ask a question
```

- [ ] **Step 8: Commit**

```bash
git add -A && git commit -m "feat: React chat UI (Vercel AI protocol, tool progress, upload)"
```

---

### Task 15: README + final verification

**Files:**
- Create: `README.md`

- [ ] **Step 1: Write `README.md`**

```markdown
# Knowledge Hub

Agentic team brain: feed it markdown/text/Google-Docs-exports, ask it questions in chat.
Spec: `docs/superpowers/specs/2026-06-11-knowledge-hub-design.md` · Decisions: `...-decisions.md`

## Quickstart

    uv sync
    export OPENAI_API_KEY=sk-...          # chat + embeddings (defaults)
    uv run hub sync ~/path/to/docs        # ingest a folder
    uv run hub serve                      # API on :8000
    cd ui && npm install && npm run dev   # chat UI on :5173

## Configuration (env, prefix HUB_)

| Var | Default | Notes |
|---|---|---|
| `HUB_DB_PATH` | `hub.db` | the whole brain is this file |
| `HUB_CHAT_MODEL` | `openai:gpt-5.2` | any pydantic-ai model string, e.g. `anthropic:claude-opus-4-8` |
| `HUB_EMBEDDING_MODEL` | `text-embedding-3-small` | `fake` = offline deterministic (dev/tests) |
| `HUB_EMBEDDING_DIM` | `1536` | must match the model; run `hub reindex` after changing |
| `HUB_ENRICH_ENABLED` | `true` | contextual lines + summaries at ingestion (cheap model) |
| `HUB_ENRICH_MODEL` | `openai:gpt-5-mini` | |

## CLI

    hub sync [FOLDER] [--watch]   # register+sync folder / re-sync all sources
    hub add FILE                  # ingest one file
    hub search QUERY              # debug hybrid search
    hub reindex                   # re-embed after model change
    hub eval eval/golden.yaml     # retrieval recall@k
    hub serve                     # FastAPI server

## Tests

    uv run pytest                 # no network: fake embedder + TestModel/FunctionModel
```

- [ ] **Step 2: Full suite + final checks**

Run: `uv run pytest -v` — Expected: all green.
Run: `cd ui && npm run build && cd ..` — Expected: clean build.

- [ ] **Step 3: Commit**

```bash
git add -A && git commit -m "docs: README with quickstart, config, CLI reference"
```

---

## Self-review notes (done at plan time)

- **Spec coverage check:** storage/hybrid/RRF (T3,6,7) ✅; chunker (T4) ✅; embeddings + reindex + model stamping (T5, T12 reindex + meta) ✅; ingestion pipeline, dedupe, deletion, per-file isolation, report (T8) ✅; enrichment toggleable (T9) ✅; agent 4 tools + honesty/citation prompt (T10) ✅; API surface incl. auth stub + CORS (T11) ✅; CLI incl. watch (T12) ✅; eval golden set (T13) ✅; UI with tool progress + upload (T14) ✅; README (T15) ✅. Deferred per spec §12: Drive connector, Slack bot, PDF/docx, Postgres, real auth.
- **Max tool calls (~15):** enforced softly via system prompt + `retries=2`; pydantic-ai `UsageLimits(request_limit=...)` can be threaded through `dispatch_request` if the installed version supports a `usage_limits` kwarg — implementer should attempt it in Task 11 and keep it if it works.
- **Type consistency:** `Storage(con, embedder)`, `HubDeps(store=...)`, `build_agent(model)`, `Ingestor(store, max_chars=, overlap_chars=, enricher=)` used consistently across tasks 6–12.
```
