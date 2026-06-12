# Auth, Roles & Sources Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Roadmap items 1+2 — pluggable authentication (`none`/`oidc`/`iap` + personal access tokens), three-tier roles enforced at the retrieval layer, source-level access control, the `/sources` allowlist, and upload-to-repo via the GitHub Contents API.

**Architecture:** Identity is established per-request by `verify_request` (the existing seam) and converges on `AuthenticatedUser(email, role)` in all modes. Roles live in a `users` table; visibility is a per-*source* access level (`everyone`/`managers`) filtered inside `Storage` so chat, REST, and (later) MCP share one guarantee. Spec: `../specs/2026-06-12-team-readiness-design.md`.

**Tech Stack:** FastAPI + Starlette `SessionMiddleware`, `pyjwt[crypto]` (IAP ES256 + Google ID-token claims), `httpx` (Google token endpoint, GitHub Contents API; `MockTransport` in tests), Typer, React UI.

**Hard rules (unchanged):** tests zero-network; no SQL outside `storage.py`; `defer_model_check=True`; TDD — failing test first, commit per green step. Run `uv run pytest` from the repo root; the full suite must stay green after every task.

---

### Task 1: Settings — auth/source/GitHub knobs

**Files:**
- Modify: `src/hippo/config.py`
- Test: `tests/test_config.py` (create)

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_config.py
from pathlib import Path

from hippo.config import Settings


def test_auth_defaults_off():
    s = Settings(_env_file=None)
    assert s.auth_mode == "none"
    assert s.allowed_domain == ""
    assert s.admin_email_list == set()
    assert s.source_root_list == []


def test_admin_emails_parsed_and_lowercased():
    s = Settings(_env_file=None, admin_emails="A@x.com, b@x.com ,")
    assert s.admin_email_list == {"a@x.com", "b@x.com"}


def test_source_roots_colon_separated(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    s = Settings(_env_file=None, source_roots=f"{a}:{b}")
    assert s.source_root_list == [a.resolve(), b.resolve()]
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_config.py -v` → FAIL (`auth_mode` not defined).

- [ ] **Step 3: Implement** — add to `Settings` in `src/hippo/config.py` (after `search_top_k`):

```python
    # --- auth (spec §1) ---
    auth_mode: str = "none"  # none | oidc | iap
    allowed_domain: str = ""  # e.g. example.com; empty = any domain
    admin_emails: str = ""  # comma-separated bootstrap admins (always admin)
    secret_key: str = ""  # session-cookie signing; required in oidc mode
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    public_url: str = "http://localhost:8000"  # OIDC redirect URI base
    iap_audience: str = ""  # /projects/<n>/global/backendServices/<m>
    # --- sources / upload-to-repo (spec §1+2) ---
    source_roots: str = ""  # colon-separated dirs /sources may register
    github_token: str = ""
    github_docs_repo: str = ""  # e.g. example/hippo-docs
    github_managers_repo: str = ""
    github_branch: str = "main"

    @property
    def admin_email_list(self) -> set[str]:
        return {e.strip().lower() for e in self.admin_emails.split(",") if e.strip()}

    @property
    def source_root_list(self) -> list[Path]:
        return [Path(p).resolve() for p in self.source_roots.split(":") if p.strip()]
```

- [ ] **Step 4: Run** — `uv run pytest tests/test_config.py -v` → PASS; then full suite.

- [ ] **Step 5: Commit** — `git add src/hippo/config.py tests/test_config.py && git commit -m "feat: auth/source/github settings"`

---

### Task 2: Schema — `users`, `tokens`, `sources.access`

**Files:**
- Modify: `src/hippo/db.py`
- Test: `tests/test_db.py` (extend; create if absent)

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_db.py (append or create)
from hippo.db import connect


def _cols(con, table):
    return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}


def test_users_and_tokens_tables(tmp_path):
    con = connect(tmp_path / "t.db", embedding_dim=8)
    assert _cols(con, "users") >= {"email", "role", "created_at"}
    assert _cols(con, "tokens") >= {"token_hash", "email", "name"}


def test_sources_access_column_added_to_existing_db(tmp_path):
    """Pre-auth databases must gain sources.access on reopen (migration)."""
    db = tmp_path / "old.db"
    con = connect(db, embedding_dim=8)
    con.execute("ALTER TABLE sources DROP COLUMN access")  # simulate a v1 db
    con.commit()
    con.close()
    con2 = connect(db, embedding_dim=8)
    assert "access" in _cols(con2, "sources")
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_db.py -v` → FAIL (no `users` table).

- [ ] **Step 3: Implement** — in `src/hippo/db.py`, append to `SCHEMA` (before the FTS section):

```sql
CREATE TABLE IF NOT EXISTS users (
    email TEXT PRIMARY KEY,
    role TEXT NOT NULL DEFAULT 'developer'
        CHECK (role IN ('developer','manager','admin')),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS tokens (
    id INTEGER PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    email TEXT NOT NULL REFERENCES users(email) ON DELETE CASCADE,
    name TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
```

and in `connect()`, after `con.executescript(SCHEMA)`:

```python
    # migration: pre-auth dbs lack sources.access (ALTER can't add a CHECK;
    # Storage validates the value instead)
    cols = {r[1] for r in con.execute("PRAGMA table_info(sources)")}
    if "access" not in cols:
        con.execute("ALTER TABLE sources ADD COLUMN access TEXT NOT NULL DEFAULT 'everyone'")
```

Also add `access TEXT NOT NULL DEFAULT 'everyone'` to the `sources` CREATE TABLE in `SCHEMA` so new DBs get it directly.

- [ ] **Step 4: Run** — `uv run pytest tests/test_db.py -v` → PASS; full suite green.

- [ ] **Step 5: Commit** — `git commit -am "feat: users/tokens tables + sources.access migration"`

---

### Task 3: Storage — users & roles

**Files:**
- Modify: `src/hippo/storage.py`
- Test: `tests/test_storage.py`

- [ ] **Step 1: Write the failing tests** (append to `tests/test_storage.py`; reuse its existing `store` fixture):

```python
def test_ensure_user_defaults_developer(store):
    assert store.ensure_user("a@x.com") == "developer"
    assert store.ensure_user("a@x.com") == "developer"  # idempotent
    assert store.list_users() == [("a@x.com", "developer")]


def test_set_role_and_validation(store):
    store.set_role("a@x.com", "manager")
    assert store.ensure_user("a@x.com") == "manager"
    store.set_role("new@x.com", "admin")  # creates the row too
    assert ("new@x.com", "admin") in store.list_users()
    import pytest
    with pytest.raises(ValueError):
        store.set_role("a@x.com", "superuser")
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_storage.py -k user -v` → FAIL.

- [ ] **Step 3: Implement** — in `src/hippo/storage.py`, module level (under the dataclasses):

```python
VALID_ROLES = ("developer", "manager", "admin")
MANAGER_ROLES = ("manager", "admin")


def _visible(role: str, access: str | None) -> bool:
    """Source-level access check. access=None (uploads / no source) = everyone."""
    return role in MANAGER_ROLES or access != "managers"
```

New section in `Storage` (after `# -- sources --`):

```python
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
```

- [ ] **Step 4: Run** — targeted tests PASS, full suite green.

- [ ] **Step 5: Commit** — `git commit -am "feat: users table access via Storage (roles)"`

---

### Task 4: Storage — personal access tokens

**Files:**
- Modify: `src/hippo/storage.py`
- Test: `tests/test_storage.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_token_roundtrip_and_hashing(store):
    t = store.create_token("a@x.com", name="laptop")
    assert t.startswith("hk_") and len(t) > 30
    assert store.resolve_token(t) == "a@x.com"
    assert store.resolve_token("hk_wrong") is None
    # only the hash is stored — the raw token must not appear in the db
    raw = store.con.execute("SELECT token_hash FROM tokens").fetchone()[0]
    assert t not in raw and t[3:] not in raw
```

- [ ] **Step 2: Run to verify failure** — FAIL (`create_token` missing).

- [ ] **Step 3: Implement** — add `import hashlib` and `import secrets` to `storage.py` imports, then:

```python
    # -- personal access tokens ---------------------------------------------

    def create_token(self, email: str, name: str = "") -> str:
        """Mint a bearer token for MCP/CLI clients. Only its sha256 is stored."""
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
        return row[0] if row else None
```

- [ ] **Step 4: Run** — PASS; full suite green.

- [ ] **Step 5: Commit** — `git commit -am "feat: hashed personal access tokens in Storage"`

---

### Task 5: Storage — source access levels + `delete_source`

**Files:**
- Modify: `src/hippo/storage.py`, `src/hippo/ingest.py:114-117`, `src/hippo/cli.py:34,49`, `src/hippo/api.py:97-99`
- Test: `tests/test_storage.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_register_source_with_access_and_update(store):
    sid = store.register_source("folder", "/r/team")
    assert (sid, "folder", "/r/team", "everyone") in store.list_sources()
    sid2 = store.register_source("folder", "/r/mgr", access="managers")
    assert (sid2, "folder", "/r/mgr", "managers") in store.list_sources()
    # re-registering updates access in place
    store.register_source("folder", "/r/mgr", access="everyone")
    assert (sid2, "folder", "/r/mgr", "everyone") in store.list_sources()
    import pytest
    with pytest.raises(ValueError):
        store.register_source("folder", "/r/x", access="secret")


def test_delete_source_removes_documents(store):
    sid = store.register_source("folder", "/r/gone")
    _add_doc(store, "d.md", "manager budget text", source_id=sid)
    assert store.delete_source(sid) is True
    assert store.list_sources() == []
    assert store.list_documents(role="admin") == []
    assert store.delete_source(999) is False
```

Add this helper near the top of `tests/test_storage.py` (single chunk, used by Tasks 5–6):

```python
from hippo.chunking import Chunk


def _add_doc(store, path, text, source_id=None, title=None):
    return store.upsert_document(
        source_type="folder", path=path, title=title or path, content=text,
        content_hash=path + "h", chunks=[Chunk(position=0, heading_path=path, text=text)],
        embed_inputs=[text], source_id=source_id,
    )
```

(Check `Chunk`'s actual field names in `src/hippo/chunking.py` before writing the helper and match them exactly.)

- [ ] **Step 2: Run to verify failure** — FAIL (`register_source` rejects `access` kwarg).

- [ ] **Step 3: Implement** — replace `register_source` and `list_sources`; add `delete_source`:

```python
    def register_source(self, kind: str, location: str, access: str = "everyone") -> int:
        if access not in ("everyone", "managers"):
            raise ValueError(f"invalid access {access!r}; expected 'everyone' or 'managers'")
        with self._lock:
            with self.con:
                self.con.execute(
                    "INSERT INTO sources(kind, location, access) VALUES (?,?,?) "
                    "ON CONFLICT(location) DO UPDATE SET access=excluded.access",
                    (kind, location, access),
                )
            return self.con.execute(
                "SELECT id FROM sources WHERE location=?", (location,)
            ).fetchone()[0]

    def list_sources(self) -> list[tuple[int, str, str, str]]:
        with self._lock:
            return list(self.con.execute("SELECT id, kind, location, access FROM sources ORDER BY id"))

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
```

- [ ] **Step 4: Update the three call sites of the now-4-tuple `list_sources` and thread `access` through `sync_folder`:**

`src/hippo/ingest.py` — signature and first line of `sync_folder`:

```python
def sync_folder(folder: Path, store: Storage, *, max_chars: int, overlap_chars: int,
                enricher=None, access: str = "everyone") -> SyncReport:
    """Sync one folder: ingest new/changed, remove vanished. Per-file isolation."""
    source_id = store.register_source("folder", str(folder), access=access)
```

`src/hippo/cli.py` — both unpacking sites in `sync` become `for _, kind, loc, _access in store.list_sources()` (line 34) and `[loc for _, kind, loc, _access in store.list_sources() if kind == "folder"]` (line 49).

`src/hippo/api.py` GET `/sources` (line 99):

```python
        return [{"id": i, "kind": k, "location": loc, "access": acc}
                for i, k, loc, acc in store.list_sources()]
```

- [ ] **Step 5: Run** — `uv run pytest -v` → full suite PASS (note: `list_documents(role=...)` in the test exists only after Task 6 — if running tasks strictly in order, write `test_delete_source_removes_documents` with `store.document_exists("d.md") is False` instead, then strengthen it in Task 6).

- [ ] **Step 6: Commit** — `git commit -am "feat: source access levels + delete_source"`

---

### Task 6: Storage — role-filtered retrieval

**Files:**
- Modify: `src/hippo/storage.py` (`get_document`, `list_documents`, `search_hybrid`, `_hit`, `grep`)
- Modify call sites: `src/hippo/agent.py` (Task 7 does deps; here just keep compiling), `src/hippo/api.py`, `src/hippo/cli.py:71,107`, `tests/test_storage.py`, `tests/test_agent.py`, `tests/test_api.py`, `tests/test_concurrency.py`, `tests/test_ingest.py:45`
- Test: `tests/test_storage.py`

- [ ] **Step 1: Write the failing tests**

```python
import pytest


@pytest.fixture
def rbac_store(store):
    team = store.register_source("folder", "/r/team")
    mgr = store.register_source("folder", "/r/mgr", access="managers")
    _add_doc(store, "team/a.md", "public quarterly roadmap zebra", source_id=team)
    _add_doc(store, "mgr/comp.md", "manager compensation zebra", source_id=mgr)
    _add_doc(store, "upload/x.md", "uploaded note zebra", source_id=None)
    return store


def test_search_filters_manager_sources(rbac_store):
    dev_paths = {h.path for h in rbac_store.search_hybrid("zebra", top_k=10, role="developer")}
    assert "mgr/comp.md" not in dev_paths and "team/a.md" in dev_paths and "upload/x.md" in dev_paths
    mgr_paths = {h.path for h in rbac_store.search_hybrid("zebra", top_k=10, role="manager")}
    assert "mgr/comp.md" in mgr_paths


def test_list_get_and_grep_filter_by_role(rbac_store):
    assert {d.path for d in rbac_store.list_documents(role="developer")} == {"team/a.md", "upload/x.md"}
    assert {d.path for d in rbac_store.list_documents(role="admin")} >= {"mgr/comp.md"}
    mgr_id = next(d.id for d in rbac_store.list_documents(role="admin") if d.path == "mgr/comp.md")
    assert rbac_store.get_document(mgr_id, role="developer") is None
    assert rbac_store.get_document(mgr_id, role="manager") is not None
    assert all(h.path != "mgr/comp.md" for h in rbac_store.grep("compensation", role="developer"))
    assert any(h.path == "mgr/comp.md" for h in rbac_store.grep("compensation", role="admin"))
```

- [ ] **Step 2: Run to verify failure** — FAIL (`role` is an unexpected kwarg).

- [ ] **Step 3: Implement.** Role is **keyword-only with no default** — every caller states its authority explicitly (fail-closed by construction; a forgotten call site is a TypeError, not a leak).

```python
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
```

`search_hybrid` — same merge, but walk the full ranked candidate list and stop at `top_k` *visible* hits (so a developer's results don't shrink just because manager docs ranked high):

```python
    def search_hybrid(self, query: str, top_k: int = 8, *, role: str) -> list[SearchHit]:
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
            hits: list[SearchHit] = []
            for cid in sorted(scores, key=scores.__getitem__, reverse=True):
                hit = self._hit(cid, scores[cid], role)
                if hit is not None:
                    hits.append(hit)
                if len(hits) >= top_k:
                    break
        return hits

    def _hit(self, chunk_id: int, score: float, role: str) -> SearchHit | None:
        row = self.con.execute(
            """SELECT c.id, d.id, d.path, d.title, c.heading_path, c.text, s.access
               FROM chunks c JOIN documents d ON d.id = c.document_id
               LEFT JOIN sources s ON s.id = d.source_id WHERE c.id=?""",
            (chunk_id,),
        ).fetchone()
        # Orphan vec rowids yield None (see PR #2); invisible-to-role rows too.
        if row is None or not _visible(role, row[6]):
            return None
        return SearchHit(*row[:6], score=score)
```

`grep` — add `*, role: str`; SELECT gains `s.access` via the same LEFT JOIN; the scan loop becomes:

```python
        for row in rows:
            if not _visible(role, row[6]):
                continue
            if rx.search(row[5]):
                hits.append(SearchHit(*row[:6], score=1.0))
                if len(hits) >= limit:
                    break
```

- [ ] **Step 4: Update every call site** (mechanical; the suite enumerates them as TypeErrors):
  - `src/hippo/api.py`: `/documents`, `/documents/{id}` → `role="admin"` *temporarily* (Task 9 threads the real user role).
  - `src/hippo/agent.py`: tools → `role="admin"` temporarily (Task 7 makes it `ctx.deps.role`).
  - `src/hippo/cli.py`: `search` (line 71) and `eval` (line 107) → `role="admin"` (CLI = machine owner).
  - Tests: add `role="admin"` to every existing `search_hybrid`/`list_documents`/`get_document`/`grep` call in `tests/test_storage.py`, `tests/test_api.py`, `tests/test_agent.py`, `tests/test_concurrency.py`, `tests/test_ingest.py`.

- [ ] **Step 5: Run** — `uv run pytest -v` → full suite PASS.

- [ ] **Step 6: Commit** — `git commit -am "feat: role-filtered retrieval in Storage (search/grep/list/get)"`

---

### Task 7: Agent — role-aware `HubDeps`

**Files:**
- Modify: `src/hippo/agent.py`
- Test: `tests/test_agent.py`

- [ ] **Step 1: Write the failing test** (use the same fixtures style as the existing agent tests — `FunctionModel` driving a `search` call, `ALLOW_MODEL_REQUESTS = False`):

```python
def test_agent_search_respects_role(rbac_store_for_agent):
    """A developer's agent must not see manager-source chunks through any tool."""
    from pydantic_ai import models
    from pydantic_ai.models.function import FunctionModel
    models.ALLOW_MODEL_REQUESTS = False

    from hippo.agent import HubDeps, build_agent

    def call_search_then_answer(messages, info):
        from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
        if len(messages) == 1:
            return ModelResponse(parts=[ToolCallPart(tool_name="search", args={"query": "zebra", "top_k": 10})])
        return ModelResponse(parts=[TextPart(content="done")])

    agent = build_agent(FunctionModel(call_search_then_answer))
    result = agent.run_sync("q", deps=HubDeps(store=rbac_store_for_agent, role="developer"))
    tool_returns = [
        p.content for m in result.all_messages() for p in m.parts
        if getattr(p, "part_kind", "") == "tool-return"
    ]
    flat = str(tool_returns)
    assert "mgr/comp.md" not in flat and "team/a.md" in flat
```

Add an `rbac_store_for_agent` fixture mirroring Task 6's `rbac_store` (FakeEmbedder store + one team doc + one manager doc). Mirror the existing test file's fixture conventions exactly.

- [ ] **Step 2: Run to verify failure** — FAIL (`HubDeps` has no `role` field).

- [ ] **Step 3: Implement** — in `src/hippo/agent.py`:

```python
@dataclass
class HubDeps:
    store: Storage
    role: str  # developer | manager | admin — filters every tool's retrieval
```

and in the tools replace the Task-6 temporaries:
- `search`: `ctx.deps.store.search_hybrid(query, top_k=max(1, top_k), role=ctx.deps.role)`
- `read_document`: `ctx.deps.store.get_document(doc_id, role=ctx.deps.role)`
- `list_documents`: `ctx.deps.store.list_documents(query=query, role=ctx.deps.role)`
- `grep`: `ctx.deps.store.grep(pattern, role=ctx.deps.role)`

Update existing `HubDeps(store=...)` constructions in `tests/test_agent.py` and `src/hippo/api.py` (line 50) to pass `role="admin"` (api gets the real role in Task 9).

- [ ] **Step 4: Run** — full suite PASS.

- [ ] **Step 5: Commit** — `git commit -am "feat: role flows through agent deps into every tool"`

---

### Task 8: `auth.py` — domain gate, IAP verifier, Google ID-token validation

**Files:**
- Create: `src/hippo/auth.py`
- Modify: `pyproject.toml` (deps)
- Test: `tests/test_auth.py` (create)

- [ ] **Step 1: Add dependencies** — `uv add "pyjwt[crypto]" itsdangerous httpx` (httpx/itsdangerous may already be transitive; declare them — we import both directly).

- [ ] **Step 2: Write the failing tests**

```python
# tests/test_auth.py
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from hippo.auth import AuthError, IapVerifier, check_domain, validate_google_id_token

AUD = "/projects/1/global/backendServices/2"


def test_check_domain():
    check_domain("a@example.com", "example.com")  # ok
    check_domain("a@anywhere.com", "")  # empty = any domain
    with pytest.raises(AuthError):
        check_domain("a@gmail.com", "example.com")
    with pytest.raises(AuthError):
        check_domain("a@notexample.com.evil.com", "example.com")


@pytest.fixture
def ec_key():
    return ec.generate_private_key(ec.SECP256R1())


def _assertion(key, *, aud=AUD, email="a@x.com", kid="k1", exp_offset=600):
    claims = {"aud": aud, "iss": "https://cloud.google.com/iap",
              "exp": int(time.time()) + exp_offset, "email": email}
    return jwt.encode(claims, key, algorithm="ES256", headers={"kid": kid})


def test_iap_verifier_accepts_valid_assertion(ec_key):
    v = IapVerifier(AUD, key_fetcher=lambda: {"k1": ec_key.public_key()})
    assert v.verify(_assertion(ec_key)) == "a@x.com"


@pytest.mark.parametrize("kwargs", [
    {"aud": "/projects/9/global/backendServices/9"},  # wrong audience
    {"exp_offset": -600},                              # expired
    {"kid": "unknown"},                                # unknown signing key
    {"email": ""},                                     # no email claim
])
def test_iap_verifier_rejects(ec_key, kwargs):
    v = IapVerifier(AUD, key_fetcher=lambda: {"k1": ec_key.public_key()})
    with pytest.raises(AuthError):
        v.verify(_assertion(ec_key, **kwargs))


def _id_token(**over):
    claims = {"iss": "https://accounts.google.com", "aud": "client-1",
              "exp": int(time.time()) + 600, "email": "a@x.com", "email_verified": True}
    claims.update(over)
    return jwt.encode(claims, "irrelevant", algorithm="HS256")


def test_google_id_token_valid():
    assert validate_google_id_token(_id_token(), "client-1") == "a@x.com"


@pytest.mark.parametrize("over", [
    {"iss": "https://evil.example"},
    {"aud": "other-client"},
    {"exp": int(time.time()) - 10},
    {"email_verified": False},
    {"email": ""},
])
def test_google_id_token_rejects(over):
    with pytest.raises(AuthError):
        validate_google_id_token(_id_token(**over), "client-1")
```

- [ ] **Step 3: Run to verify failure** — `uv run pytest tests/test_auth.py -v` → FAIL (module missing).

- [ ] **Step 4: Implement `src/hippo/auth.py`**

```python
"""Identity layer. Every mode converges on AuthenticatedUser(email, role); the
rest of the codebase never knows how the email was established (spec §1)."""

import time
from dataclasses import dataclass

import jwt


class AuthError(Exception):
    """Identity could not be established or is not allowed (-> 401/403)."""


@dataclass
class AuthenticatedUser:
    email: str
    role: str  # developer | manager | admin


def check_domain(email: str, allowed_domain: str) -> None:
    if allowed_domain and not email.lower().endswith("@" + allowed_domain.lower()):
        raise AuthError(f"only {allowed_domain} accounts are allowed")


class IapVerifier:
    """Verifies GCP Identity-Aware Proxy assertions (ES256 JWTs signed by Google).

    key_fetcher is injectable so tests supply a local key; production lazily
    fetches Google's JWKS once per process and caches it."""

    KEYS_URL = "https://www.gstatic.com/iap/verify/public_key-jwk"

    def __init__(self, audience: str, key_fetcher=None):
        self.audience = audience
        self._fetch = key_fetcher or self._fetch_google_keys
        self._keys: dict | None = None

    def _fetch_google_keys(self) -> dict:
        import httpx

        jwks = httpx.get(self.KEYS_URL, timeout=10).json()
        return {k["kid"]: jwt.PyJWK(k).key for k in jwks["keys"]}

    def verify(self, assertion: str) -> str:
        if self._keys is None:
            self._keys = self._fetch()
        try:
            kid = jwt.get_unverified_header(assertion).get("kid")
        except jwt.PyJWTError as e:
            raise AuthError(f"malformed IAP assertion: {e}") from e
        key = self._keys.get(kid)
        if key is None:
            raise AuthError("unknown IAP signing key")
        try:
            claims = jwt.decode(
                assertion, key=key, algorithms=["ES256"],
                audience=self.audience, issuer="https://cloud.google.com/iap",
            )
        except jwt.PyJWTError as e:
            raise AuthError(f"invalid IAP assertion: {e}") from e
        email = claims.get("email", "")
        if not email:
            raise AuthError("IAP assertion has no email claim")
        return email


def validate_google_id_token(id_token: str, client_id: str) -> str:
    """Claims-validate a Google ID token received directly from Google's token
    endpoint over TLS (OIDC code flow). Signature verification is intentionally
    skipped — the spec permits it for tokens obtained straight from the issuer,
    and we never accept ID tokens from any other channel."""
    try:
        claims = jwt.decode(id_token, options={"verify_signature": False})
    except jwt.PyJWTError as e:
        raise AuthError(f"malformed ID token: {e}") from e
    if claims.get("iss") not in ("https://accounts.google.com", "accounts.google.com"):
        raise AuthError("ID token has the wrong issuer")
    if claims.get("aud") != client_id:
        raise AuthError("ID token has the wrong audience")
    if claims.get("exp", 0) < time.time():
        raise AuthError("ID token is expired")
    email = claims.get("email", "")
    if not email or not claims.get("email_verified", False):
        raise AuthError("ID token has no verified email")
    return email
```

- [ ] **Step 5: Run** — `uv run pytest tests/test_auth.py -v` → PASS (zero network: keys injected).

- [ ] **Step 6: Commit** — `git commit -am "feat: auth core — domain gate, IAP verifier, Google ID-token validation"`

---

### Task 9: API — real `verify_request` (none / bearer / iap), `require_admin`, `/me`

**Files:**
- Modify: `src/hippo/api.py`
- Test: `tests/test_api_auth.py` (create)

- [ ] **Step 1: Write the failing tests** (zero-network: `FakeEmbedder` via `HIPPO_EMBEDDING_MODEL=fake`-style settings construction, mirroring `tests/test_api.py`'s existing app fixture; IAP verifier injected):

```python
# tests/test_api_auth.py
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from hippo.api import build_app
from hippo.auth import IapVerifier
from hippo.config import Settings

AUD = "/projects/1/global/backendServices/2"


def _settings(tmp_path, **over):
    base = dict(_env_file=None, db_path=tmp_path / "t.db", embedding_model="fake",
                embedding_dim=32, enrich_enabled=False)
    base.update(over)
    return Settings(**base)


def test_none_mode_is_implicit_admin(tmp_path):
    app = build_app(_settings(tmp_path))
    c = TestClient(app)
    assert c.get("/health").status_code == 200
    me = c.get("/me").json()
    assert me["role"] == "admin" and me["auth_mode"] == "none"


def test_iap_mode_rejects_without_assertion(tmp_path):
    s = _settings(tmp_path, auth_mode="iap", iap_audience=AUD)
    key = ec.generate_private_key(ec.SECP256R1())
    verifier = IapVerifier(AUD, key_fetcher=lambda: {"k1": key.public_key()})
    app = build_app(s, iap_verifier=verifier)
    c = TestClient(app)
    assert c.get("/documents").status_code == 401
    assertion = jwt.encode(
        {"aud": AUD, "iss": "https://cloud.google.com/iap",
         "exp": int(time.time()) + 600, "email": "dev@x.com"},
        key, algorithm="ES256", headers={"kid": "k1"})
    r = c.get("/me", headers={"x-goog-iap-jwt-assertion": assertion})
    assert r.status_code == 200 and r.json() == {
        "email": "dev@x.com", "role": "developer", "auth_mode": "iap",
        "upload": {"team_repo": False, "managers_repo": False}}


def test_domain_gate_403(tmp_path):
    s = _settings(tmp_path, auth_mode="iap", iap_audience=AUD, allowed_domain="x.com")
    key = ec.generate_private_key(ec.SECP256R1())
    app = build_app(s, iap_verifier=IapVerifier(AUD, key_fetcher=lambda: {"k1": key.public_key()}))
    bad = jwt.encode({"aud": AUD, "iss": "https://cloud.google.com/iap",
                      "exp": int(time.time()) + 600, "email": "evil@gmail.com"},
                     key, algorithm="ES256", headers={"kid": "k1"})
    assert TestClient(app).get("/me", headers={"x-goog-iap-jwt-assertion": bad}).status_code == 403


def test_bearer_token_works_in_any_mode_and_env_admins_promoted(tmp_path):
    s = _settings(tmp_path, auth_mode="iap", iap_audience=AUD, admin_emails="boss@x.com")
    app = build_app(s, iap_verifier=IapVerifier(AUD, key_fetcher=lambda: {}))
    store = app.state.store
    t_dev = store.create_token("dev@x.com")
    t_boss = store.create_token("boss@x.com")
    c = TestClient(app)
    assert c.get("/me", headers={"Authorization": f"Bearer {t_dev}"}).json()["role"] == "developer"
    assert c.get("/me", headers={"Authorization": f"Bearer {t_boss}"}).json()["role"] == "admin"
    assert c.get("/me", headers={"Authorization": "Bearer hk_bogus"}).status_code == 401


def test_role_filtering_through_api(tmp_path):
    s = _settings(tmp_path, auth_mode="iap", iap_audience=AUD, admin_emails="boss@x.com")
    app = build_app(s, iap_verifier=IapVerifier(AUD, key_fetcher=lambda: {}))
    store = app.state.store
    mgr = store.register_source("folder", "/r/mgr", access="managers")
    from hippo.chunking import Chunk
    store.upsert_document(source_type="folder", path="mgr/comp.md", title="comp",
                          content="secret", content_hash="h", source_id=mgr,
                          chunks=[Chunk(position=0, heading_path="comp", text="secret")],
                          embed_inputs=["secret"])
    c = TestClient(app)
    dev = {"Authorization": f"Bearer {store.create_token('dev@x.com')}"}
    boss = {"Authorization": f"Bearer {store.create_token('boss@x.com')}"}
    assert all(d["path"] != "mgr/comp.md" for d in c.get("/documents", headers=dev).json())
    assert any(d["path"] == "mgr/comp.md" for d in c.get("/documents", headers=boss).json())
    doc_id = next(d["id"] for d in c.get("/documents", headers=boss).json() if d["path"] == "mgr/comp.md")
    assert c.get(f"/documents/{doc_id}", headers=dev).status_code == 404
    assert c.get(f"/documents/{doc_id}", headers=boss).status_code == 200
```

(Adapt `Chunk` field names to `src/hippo/chunking.py` reality, as in Task 5.)

- [ ] **Step 2: Run to verify failure** — FAIL (`/me` missing; `build_app` rejects `iap_verifier`; `app.state.store` unset).

- [ ] **Step 3: Implement in `src/hippo/api.py`.** New signature and wiring:

```python
def build_app(settings: Settings | None = None, model_override=None, *,
              iap_verifier=None, code_exchanger=None, github_factory=None) -> FastAPI:
```

Delete the module-level `verify_request` stub. Inside `build_app`, after `store` is built, add `app.state.store = store` (right after `app = FastAPI(...)`) and define the closures:

```python
    iap = iap_verifier or (IapVerifier(settings.iap_audience) if settings.auth_mode == "iap" else None)

    def _user_for(email: str) -> AuthenticatedUser:
        try:
            check_domain(email, settings.allowed_domain)
        except AuthError as e:
            raise HTTPException(status_code=403, detail=str(e))
        role = store.ensure_user(email)
        if email.lower() in settings.admin_email_list:
            role = "admin"  # env bootstrap always wins (spec §1)
        return AuthenticatedUser(email=email, role=role)

    async def verify_request(request: Request) -> AuthenticatedUser:
        # Bearer tokens are accepted in every mode (MCP/CLI clients, spec §1).
        authz = request.headers.get("authorization", "")
        if authz.lower().startswith("bearer "):
            email = store.resolve_token(authz[7:].strip())
            if email is None:
                raise HTTPException(status_code=401, detail="invalid token")
            return _user_for(email)
        if settings.auth_mode == "none":
            return AuthenticatedUser(email="local", role="admin")
        if settings.auth_mode == "iap":
            assertion = request.headers.get("x-goog-iap-jwt-assertion", "")
            if not assertion:
                raise HTTPException(status_code=401, detail="missing IAP assertion")
            try:
                return _user_for(iap.verify(assertion))
            except AuthError as e:
                raise HTTPException(status_code=401, detail=str(e))
        email = request.session.get("email", "")  # oidc: session cookie (Task 10)
        if not email:
            raise HTTPException(status_code=401, detail="not signed in")
        return _user_for(email)

    async def require_admin(user: AuthenticatedUser = Depends(verify_request)) -> AuthenticatedUser:
        if user.role != "admin":
            raise HTTPException(status_code=403, detail="admin only")
        return user
```

Imports: `from .auth import AuthError, AuthenticatedUser, IapVerifier, check_domain, validate_google_id_token`.

Update every route: `_=Depends(verify_request)` → `user: AuthenticatedUser = Depends(verify_request)`; thread the role:
- `/chat`: `deps = HubDeps(store=store, role=user.role)` per request (delete the startup-time `deps = HubDeps(store=store)`), pass `deps=deps`.
- `/documents`: `store.list_documents(query=query, role=user.role)`.
- `/documents/{doc_id}`: `store.get_document(doc_id, role=user.role)` (invisible == absent → existing 404 branch covers both).
- Add `/me`:

```python
    @app.get("/me")
    async def me(user: AuthenticatedUser = Depends(verify_request)):
        return {
            "email": user.email, "role": user.role, "auth_mode": settings.auth_mode,
            "upload": {
                "team_repo": bool(settings.github_token and settings.github_docs_repo),
                "managers_repo": bool(settings.github_token and settings.github_managers_repo)
                                 and user.role in ("manager", "admin"),
            },
        }
```

(`code_exchanger`/`github_factory` are accepted now, used in Tasks 10/13.)

- [ ] **Step 4: Run** — `uv run pytest tests/test_api_auth.py tests/test_api.py -v` → PASS; full suite green.

- [ ] **Step 5: Commit** — `git commit -am "feat: verify_request implemented — none/bearer/iap modes, require_admin, /me"`

---

### Task 10: API — OIDC login/callback/logout + session cookie

**Files:**
- Modify: `src/hippo/api.py`
- Test: `tests/test_api_auth.py`

- [ ] **Step 1: Write the failing tests**

```python
import jwt as pyjwt


def _fake_exchange(claims_over=None):
    claims = {"iss": "https://accounts.google.com", "aud": "cid",
              "exp": int(time.time()) + 600, "email": "u@x.com", "email_verified": True}
    claims.update(claims_over or {})
    def exchange(code, settings):
        assert code == "authcode"
        return {"id_token": pyjwt.encode(claims, "k", algorithm="HS256")}
    return exchange


def _oidc_app(tmp_path, **over):
    s = _settings(tmp_path, auth_mode="oidc", secret_key="s3cret",
                  oidc_client_id="cid", oidc_client_secret="cs", **over)
    return build_app(s, code_exchanger=_fake_exchange(over.pop("claims", None) if "claims" in over else None))


def test_oidc_full_flow_sets_session(tmp_path):
    c = TestClient(_oidc_app(tmp_path), follow_redirects=False)
    assert c.get("/documents").status_code == 401
    r = c.get("/auth/login")
    assert r.status_code == 307 and "accounts.google.com" in r.headers["location"]
    from urllib.parse import parse_qs, urlparse
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    r = c.get(f"/auth/callback?code=authcode&state={state}")
    assert r.status_code == 307 and r.headers["location"] == "/"
    assert c.get("/me").json()["email"] == "u@x.com"
    r = c.get("/auth/logout")
    assert c.get("/documents").status_code == 401


def test_oidc_state_mismatch_rejected(tmp_path):
    c = TestClient(_oidc_app(tmp_path), follow_redirects=False)
    c.get("/auth/login")
    assert c.get("/auth/callback?code=authcode&state=forged").status_code == 400


def test_oidc_requires_secret_key(tmp_path):
    import pytest
    with pytest.raises(ValueError):
        build_app(_settings(tmp_path, auth_mode="oidc", oidc_client_id="cid"))
```

- [ ] **Step 2: Run to verify failure** — FAIL (no `/auth/login` route).

- [ ] **Step 3: Implement.** Module-level in `api.py`:

```python
def _exchange_code_with_google(code: str, settings: Settings) -> dict:
    import httpx

    r = httpx.post("https://oauth2.googleapis.com/token", data={
        "code": code, "client_id": settings.oidc_client_id,
        "client_secret": settings.oidc_client_secret,
        "redirect_uri": f"{settings.public_url}/auth/callback",
        "grant_type": "authorization_code",
    }, timeout=10)
    r.raise_for_status()
    return r.json()
```

Inside `build_app`, after the `require_admin` definition:

```python
    if settings.auth_mode == "oidc":
        if not settings.secret_key:
            raise ValueError("HIPPO_SECRET_KEY is required when HIPPO_AUTH_MODE=oidc")
        app.add_middleware(SessionMiddleware, secret_key=settings.secret_key,
                           https_only=settings.public_url.startswith("https"))
        exchange = code_exchanger or _exchange_code_with_google

        @app.get("/auth/login")
        async def auth_login(request: Request):
            state = secrets.token_urlsafe(16)
            request.session["oauth_state"] = state
            params = {
                "client_id": settings.oidc_client_id,
                "redirect_uri": f"{settings.public_url}/auth/callback",
                "response_type": "code", "scope": "openid email", "state": state,
            }
            if settings.allowed_domain:
                params["hd"] = settings.allowed_domain  # UX hint; check_domain enforces
            return RedirectResponse("https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params))

        @app.get("/auth/callback")
        async def auth_callback(request: Request, code: str, state: str):
            if state != request.session.pop("oauth_state", None):
                raise HTTPException(status_code=400, detail="state mismatch")
            tokens = await run_in_threadpool(exchange, code, settings)
            try:
                email = validate_google_id_token(tokens.get("id_token", ""), settings.oidc_client_id)
                check_domain(email, settings.allowed_domain)
            except AuthError as e:
                raise HTTPException(status_code=403, detail=str(e))
            store.ensure_user(email)
            request.session["email"] = email
            return RedirectResponse("/")

        @app.get("/auth/logout")
        async def auth_logout(request: Request):
            request.session.clear()
            return RedirectResponse("/")
```

Imports to add: `import secrets`, `from urllib.parse import urlencode`, `from starlette.middleware.sessions import SessionMiddleware`, `from starlette.responses import RedirectResponse`.

- [ ] **Step 4: Run** — PASS; full suite green.

- [ ] **Step 5: Commit** — `git commit -am "feat: in-app Google OIDC login flow (oidc mode)"`

---

### Task 11: API — `/sources` allowlist, access level, admin-only, DELETE

**Files:**
- Modify: `src/hippo/api.py`
- Test: `tests/test_api_auth.py`

- [ ] **Step 1: Write the failing tests**

```python
def _iap_app_with_tokens(tmp_path, **settings_over):
    s = _settings(tmp_path, auth_mode="iap", iap_audience=AUD,
                  admin_emails="boss@x.com", **settings_over)
    app = build_app(s, iap_verifier=IapVerifier(AUD, key_fetcher=lambda: {}))
    store = app.state.store
    return (app, store,
            {"Authorization": f"Bearer {store.create_token('dev@x.com')}"},
            {"Authorization": f"Bearer {store.create_token('boss@x.com')}"})


def test_sources_admin_only_and_allowlisted(tmp_path):
    docs = tmp_path / "roots" / "team"
    docs.mkdir(parents=True)
    (docs / "a.md").write_text("# A\n\nalpha")
    app, store, dev, boss = _iap_app_with_tokens(tmp_path, source_roots=str(tmp_path / "roots"))
    c = TestClient(app)
    body = {"location": str(docs), "access": "everyone"}
    assert c.post("/sources", json=body, headers=dev).status_code == 403   # not admin
    outside = {"location": str(tmp_path), "access": "everyone"}            # parent of root
    assert c.post("/sources", json=outside, headers=boss).status_code == 403
    r = c.post("/sources", json=body, headers=boss)
    assert r.status_code == 200 and r.json()["report"]["added"] == 1
    listed = c.get("/sources", headers=dev).json()
    assert listed[0]["access"] == "everyone"


def test_sources_registration_refused_without_roots_when_auth_on(tmp_path):
    app, _, _, boss = _iap_app_with_tokens(tmp_path)  # no source_roots configured
    c = TestClient(app)
    r = c.post("/sources", json={"location": str(tmp_path)}, headers=boss)
    assert r.status_code == 403


def test_delete_source_admin_only(tmp_path):
    docs = tmp_path / "roots" / "m"
    docs.mkdir(parents=True)
    (docs / "s.md").write_text("# S\n\nsecret")
    app, store, dev, boss = _iap_app_with_tokens(tmp_path, source_roots=str(tmp_path / "roots"))
    c = TestClient(app)
    c.post("/sources", json={"location": str(docs), "access": "managers"}, headers=boss)
    sid = c.get("/sources", headers=boss).json()[0]["id"]
    assert c.delete(f"/sources/{sid}", headers=dev).status_code == 403
    assert c.delete(f"/sources/{sid}", headers=boss).status_code == 200
    assert c.get("/sources", headers=boss).json() == []
    assert c.delete(f"/sources/{sid}", headers=boss).status_code == 404
```

- [ ] **Step 2: Run to verify failure** — FAIL (no admin gate / no `access` field / no DELETE).

- [ ] **Step 3: Implement.** `SourceIn` gains the field (import `Literal` from `typing`):

```python
class SourceIn(BaseModel):
    kind: str = "folder"
    location: str
    access: Literal["everyone", "managers"] = "everyone"
```

Replace `POST /sources` and add DELETE:

```python
    @app.post("/sources")
    async def add_source(body: SourceIn, user: AuthenticatedUser = Depends(require_admin)):
        folder = Path(body.location).resolve()
        roots = settings.source_root_list
        if settings.auth_mode != "none" and not roots:
            raise HTTPException(status_code=403,
                detail="source registration is disabled: no HIPPO_SOURCE_ROOTS configured")
        if roots and not any(folder == r or r in folder.parents for r in roots):
            raise HTTPException(status_code=403, detail=f"{folder} is outside HIPPO_SOURCE_ROOTS")
        if not folder.is_dir():
            raise HTTPException(status_code=400, detail=f"not a directory: {folder}")
        report = await run_in_threadpool(
            sync_folder, folder, store, max_chars=settings.chunk_max_chars,
            overlap_chars=settings.chunk_overlap_chars, enricher=enricher, access=body.access,
        )
        return {"report": {"added": report.added, "updated": report.updated,
                           "skipped": report.skipped, "removed": report.removed,
                           "failed": report.failed}}

    @app.delete("/sources/{source_id}")
    async def remove_source(source_id: int, user: AuthenticatedUser = Depends(require_admin)):
        if not store.delete_source(source_id):
            raise HTTPException(status_code=404, detail="source not found")
        return {"deleted": source_id}
```

Note `folder.resolve()` before the root check — symlink/`..` tricks must not escape the allowlist.

- [ ] **Step 4: Run** — PASS; full suite green (existing `tests/test_api.py` source tests run in `none` mode with no roots configured → unchanged behavior).

- [ ] **Step 5: Commit** — `git commit -am "feat: /sources — admin-only, allowlisted roots, access level, DELETE"`

---

### Task 12: `github.py` — Contents API client

**Files:**
- Create: `src/hippo/github.py`
- Test: `tests/test_github.py` (create)

- [ ] **Step 1: Write the failing tests** (zero network via `httpx.MockTransport`):

```python
# tests/test_github.py
import base64
import json

import httpx
import pytest

from hippo.github import GitHubContentsClient, GitHubError


def _client(handler):
    http = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.github.com")
    return GitHubContentsClient("org/docs", "tok", branch="main", client=http)


def test_put_file_creates_new(monkeypatch):
    seen = {}
    def handler(request):
        if request.method == "GET":
            return httpx.Response(404)
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={"commit": {"sha": "abc123"}})
    sha = _client(handler).put_file("uploads/n.md", b"# N", "hippo upload: n.md (by a@x.com)")
    assert sha == "abc123"
    assert seen["url"].endswith("/repos/org/docs/contents/uploads/n.md")
    assert base64.b64decode(seen["body"]["content"]) == b"# N"
    assert seen["body"]["branch"] == "main" and "sha" not in seen["body"]


def test_put_file_updates_existing_with_sha():
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"sha": "oldsha"})
        assert json.loads(request.content)["sha"] == "oldsha"
        return httpx.Response(200, json={"commit": {"sha": "newsha"}})
    assert _client(handler).put_file("a.md", b"x", "m") == "newsha"


def test_put_file_error_raises():
    def handler(request):
        return httpx.Response(404) if request.method == "GET" else httpx.Response(422, text="nope")
    with pytest.raises(GitHubError):
        _client(handler).put_file("a.md", b"x", "m")
```

- [ ] **Step 2: Run to verify failure** — FAIL (module missing).

- [ ] **Step 3: Implement `src/hippo/github.py`**

```python
"""Upload-to-repo via the GitHub Contents API: one HTTP call commits one file —
no clone, no local git state (spec §1, 'version control as the default path')."""

import base64

import httpx


class GitHubError(Exception):
    pass


class GitHubContentsClient:
    def __init__(self, repo: str, token: str, branch: str = "main",
                 client: httpx.Client | None = None):
        self.repo = repo
        self.branch = branch
        self._http = client or httpx.Client(
            base_url="https://api.github.com",
            headers={"Authorization": f"Bearer {token}",
                     "Accept": "application/vnd.github+json"},
            timeout=15,
        )

    def put_file(self, path: str, content: bytes, message: str) -> str:
        """Create or update `path` on the branch; returns the commit sha."""
        url = f"/repos/{self.repo}/contents/{path}"
        body = {"message": message,
                "content": base64.b64encode(content).decode(),
                "branch": self.branch}
        existing = self._http.get(url, params={"ref": self.branch})
        if existing.status_code == 200:
            body["sha"] = existing.json()["sha"]  # update needs the current blob sha
        r = self._http.put(url, json=body)
        if r.status_code not in (200, 201):
            raise GitHubError(f"GitHub commit failed ({r.status_code}): {r.text[:200]}")
        return r.json()["commit"]["sha"]
```

- [ ] **Step 4: Run** — PASS.

- [ ] **Step 5: Commit** — `git commit -am "feat: GitHub Contents API client"`

---

### Task 13: API — `/ingest` upload-to-repo flow

**Files:**
- Modify: `src/hippo/api.py`
- Test: `tests/test_api_auth.py`

- [ ] **Step 1: Write the failing tests**

```python
class _FakeGH:
    def __init__(self):
        self.calls = []
    def put_file(self, path, content, message):
        self.calls.append((path, content, message))
        return "sha1"


def test_ingest_commits_to_repo_when_configured(tmp_path):
    fakes = {}
    def factory(repo):
        fakes[repo] = _FakeGH()
        return fakes[repo]
    s = _settings(tmp_path, auth_mode="iap", iap_audience=AUD, admin_emails="boss@x.com",
                  github_token="t", github_docs_repo="org/docs", github_managers_repo="org/mgr")
    app = build_app(s, iap_verifier=IapVerifier(AUD, key_fetcher=lambda: {}),
                    github_factory=factory)
    store = app.state.store
    dev = {"Authorization": f"Bearer {store.create_token('dev@x.com')}"}
    boss = {"Authorization": f"Bearer {store.create_token('boss@x.com')}"}
    c = TestClient(app)
    r = c.post("/ingest", files={"file": ("n.md", b"# N\n\nbody")}, headers=dev)
    assert r.status_code == 200 and r.json()["status"] == "committed"
    assert fakes["org/docs"].calls[0][0] == "uploads/n.md"
    assert "dev@x.com" in fakes["org/docs"].calls[0][2]
    # managers repo: developers refused, managers/admins allowed
    r = c.post("/ingest", files={"file": ("m.md", b"# M")}, data={"repo": "managers"}, headers=dev)
    assert r.status_code == 403
    r = c.post("/ingest", files={"file": ("m.md", b"# M")}, data={"repo": "managers"}, headers=boss)
    assert r.status_code == 200 and fakes["org/mgr"].calls[0][0] == "uploads/m.md"


def test_ingest_falls_back_to_unversioned_without_github(tmp_path):
    app = build_app(_settings(tmp_path))  # none mode, no github config
    c = TestClient(app)
    r = c.post("/ingest", files={"file": ("n.md", b"# N\n\nbody")})
    assert r.status_code == 200
    assert r.json()["status"] == "added" and r.json()["versioned"] is False
```

- [ ] **Step 2: Run to verify failure** — FAIL (`status` is `added`, no commit path).

- [ ] **Step 3: Implement.** In `build_app`, default the factory near the top:

```python
    github_factory = github_factory or (
        lambda repo: GitHubContentsClient(repo, settings.github_token, settings.github_branch))
```

Replace `/ingest` (imports: `from fastapi import Form`, `from .github import GitHubContentsClient, GitHubError`):

```python
    @app.post("/ingest")
    async def ingest(file: UploadFile, repo: str = Form("team"),
                     user: AuthenticatedUser = Depends(verify_request)):
        raw_bytes = await file.read()
        name = Path(file.filename or "upload.md").name  # basename only: no path tricks
        if repo == "managers" and user.role not in ("manager", "admin"):
            raise HTTPException(status_code=403, detail="managers repo requires the manager role")
        target = settings.github_managers_repo if repo == "managers" else settings.github_docs_repo
        if settings.github_token and target:
            gh = github_factory(target)
            try:
                sha = await run_in_threadpool(
                    gh.put_file, f"uploads/{name}", raw_bytes,
                    f"hippo upload: {name} (by {user.email})")
            except GitHubError as e:
                raise HTTPException(status_code=502, detail=str(e))
            # The doc is now versioned in git; the next repo sync ingests it (spec §1).
            return {"status": "committed", "repo": target, "path": f"uploads/{name}", "commit": sha}
        if repo == "managers":
            raise HTTPException(status_code=400, detail="managers repo is not configured")
        # No GitHub configured (personal mode): direct, unversioned ingestion.
        raw = raw_bytes.decode("utf-8", errors="replace")
        suffix = Path(name).suffix or ".md"
        result = await run_in_threadpool(ingestor.ingest_text, name, raw, suffix=suffix)
        if result.status == "failed":
            raise HTTPException(status_code=422, detail=result.error)
        return {"path": result.path, "status": result.status,
                "chunks": result.chunks, "versioned": False}
```

- [ ] **Step 4: Run** — PASS; check `tests/test_api.py`'s existing ingest tests still pass (they run with no GitHub config → fallback branch; add `versioned` to expected keys only if they assert exact dict equality).

- [ ] **Step 5: Commit** — `git commit -am "feat: UI uploads commit to the docs repo via GitHub Contents API"`

---

### Task 14: CLI — `hippo role` and `hippo token`

**Files:**
- Modify: `src/hippo/cli.py`
- Test: `tests/test_cli.py` (create; use `typer.testing.CliRunner`, env pointed at a tmp db with `HIPPO_EMBEDDING_MODEL=fake`)

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_cli.py
from typer.testing import CliRunner

from hippo.cli import app

runner = CliRunner()


def _env(tmp_path):
    return {"HIPPO_DB_PATH": str(tmp_path / "t.db"), "HIPPO_EMBEDDING_MODEL": "fake",
            "HIPPO_EMBEDDING_DIM": "32", "HIPPO_ENRICH_ENABLED": "false"}


def test_role_set_and_list(tmp_path):
    env = _env(tmp_path)
    r = runner.invoke(app, ["role", "set", "a@x.com", "manager"], env=env)
    assert r.exit_code == 0
    r = runner.invoke(app, ["role", "list"], env=env)
    assert "manager" in r.output and "a@x.com" in r.output
    r = runner.invoke(app, ["role", "set", "a@x.com", "superuser"], env=env)
    assert r.exit_code != 0


def test_token_create_prints_once(tmp_path):
    r = runner.invoke(app, ["token", "create", "a@x.com", "--name", "laptop"], env=env := _env(tmp_path))
    assert r.exit_code == 0 and "hk_" in r.output
```

- [ ] **Step 2: Run to verify failure** — FAIL (`role` command unknown).

- [ ] **Step 3: Implement** — in `src/hippo/cli.py` after the `app = typer.Typer(...)` line:

```python
role_app = typer.Typer(help="Manage user roles (developer | manager | admin).")
app.add_typer(role_app, name="role")
token_app = typer.Typer(help="Personal access tokens for MCP/API clients.")
app.add_typer(token_app, name="token")


@role_app.command("set")
def role_set(email: str, role: str):
    """Set a user's role (creates the user if new)."""
    store, _ = _store(Settings())
    try:
        store.set_role(email, role)
    except ValueError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1)
    typer.echo(f"{email}: {role}")


@role_app.command("list")
def role_list():
    store, _ = _store(Settings())
    for email, role in store.list_users():
        typer.echo(f"{role:10} {email}")


@token_app.command("create")
def token_create(email: str, name: str = typer.Option("", help="label, e.g. 'claude-code laptop'")):
    """Mint a bearer token tied to a user. Shown once; only its hash is stored."""
    store, _ = _store(Settings())
    typer.echo(store.create_token(email, name))
    typer.echo("save it now — it cannot be shown again", err=True)
```

- [ ] **Step 4: Run** — PASS; full suite green.

- [ ] **Step 5: Commit** — `git commit -am "feat: hippo role / hippo token CLI"`

---

### Task 15: UI — sign-in, identity header, repo picker, commit status

**Files:**
- Modify: `ui/src/App.tsx`, `ui/src/styles.css` (or wherever the existing styles live — check `ui/src/` first)

No UI test harness exists; the gate is `cd ui && npm run build` passing plus the API contract tests already written. Keep changes inside `App.tsx`.

- [ ] **Step 1: Add a `Me` type and state.** In `App.tsx`:

```tsx
type Me = {
  email: string;
  role: string;
  auth_mode: string;
  upload: { team_repo: boolean; managers_repo: boolean };
};
```

In `App()`: `const [me, setMe] = useState<Me | null>(null);` and `const [needsLogin, setNeedsLogin] = useState(false);` plus, with the existing mount effect:

```tsx
  useEffect(() => {
    fetch("/me").then((r) => {
      if (r.status === 401) setNeedsLogin(true);
      else if (r.ok) r.json().then(setMe);
    }).catch(() => {});
  }, []);
```

- [ ] **Step 2: Sign-in gate.** Immediately before the main `return`, render the gate when unauthenticated (only the `oidc` server has `/auth/login`; for `iap` the proxy redirects before the app loads, so this screen is effectively oidc-only):

```tsx
  if (needsLogin) {
    return (
      <div className="app">
        <div className="empty" style={{ marginTop: "20vh" }}>
          <span className="logo">{"\u{1F99B}"}</span>
          <h1>Hippo</h1>
          <p>Sign in with your Google account to continue.</p>
          <a className="upload-btn" href="/auth/login">Sign in with Google</a>
        </div>
      </div>
    );
  }
```

- [ ] **Step 3: Identity in the header.** Inside `<header>`, next to the upload control:

```tsx
          {me && me.auth_mode !== "none" && (
            <span className="whoami">
              {me.email} ({me.role})
              {me.auth_mode === "oidc" && <> · <a href="/auth/logout">sign out</a></>}
            </span>
          )}
```

- [ ] **Step 4: Repo picker + commit status.** Add `const [uploadRepo, setUploadRepo] = useState("team");`. In the upload `<div className="upload">`, before the label, show the picker only when the managers repo is available:

```tsx
          {me?.upload.managers_repo && (
            <select value={uploadRepo} onChange={(e) => setUploadRepo(e.target.value)}>
              <option value="team">team docs</option>
              <option value="managers">managers docs</option>
            </select>
          )}
```

Update `upload()` to send the field and read the new response shapes:

```tsx
  async function upload(file: File) {
    const form = new FormData();
    form.append("file", file);
    form.append("repo", uploadRepo);
    setUploadNote(`adding ${file.name}…`);
    const res = await fetch("/ingest", { method: "POST", body: form });
    const body = await res.json();
    if (!res.ok) {
      setUploadNote(`failed: ${body.detail}`);
    } else if (body.status === "committed") {
      setUploadNote(`committed ${file.name} to ${body.repo} — searchable after the next sync`);
    } else {
      setUploadNote(`added ${file.name} (unversioned) — ${body.chunks} chunks`);
      refreshDocs();
    }
  }
```

- [ ] **Step 5: Style crumbs** — add `.whoami { font-size: 12px; opacity: 0.7; margin-right: 12px; }` (match the existing stylesheet's conventions/units).

- [ ] **Step 6: Verify** — `cd ui && npm run build` → clean build. Manually eyeball with `npm run dev` against a `none`-mode server (header hidden, upload unchanged).

- [ ] **Step 7: Commit** — `git commit -am "feat(ui): sign-in gate, identity header, repo picker, commit status"`

---

### Task 16: Docs + final gate

**Files:**
- Modify: `CLAUDE.md` (architecture block: add `auth.py`, `github.py`; update api.py/cli.py lines; bump test count), `README.md` (config table: the new `HIPPO_*` vars; auth modes section; `hippo role`/`hippo token`), `docs/superpowers/plans/2026-06-12-roadmap.md` (items 1+2 → done)

- [ ] **Step 1: Update CLAUDE.md** — architecture entries:

```
auth.py        AuthenticatedUser, check_domain, IapVerifier (ES256, injectable keys),
               validate_google_id_token. Modes wired in api.py: none | oidc | iap (+ bearer
               tokens in any mode). Role filtering lives in storage.py, NOT here.
github.py      GitHubContentsClient.put_file: upload-to-repo via Contents API (1 call/file)
```

and under Hard rules add: "**Retrieval methods take `role` keyword-only with no default** — a forgotten call site must be a TypeError, never an access-control leak."

- [ ] **Step 2: Update README** — document `HIPPO_AUTH_MODE`, `HIPPO_ALLOWED_DOMAIN`, `HIPPO_ADMIN_EMAILS`, `HIPPO_SECRET_KEY`, `HIPPO_OIDC_CLIENT_ID/SECRET`, `HIPPO_PUBLIC_URL`, `HIPPO_IAP_AUDIENCE`, `HIPPO_SOURCE_ROOTS`, `HIPPO_GITHUB_TOKEN/DOCS_REPO/MANAGERS_REPO/BRANCH` in the existing config table style.

- [ ] **Step 3: Full gate** — `uv run pytest -v` (all green), `cd ui && npm run build` (clean), `uv run hippo eval eval/golden.yaml` (4/4).

- [ ] **Step 4: Commit** — `git commit -am "docs: auth/sources round — CLAUDE.md, README, roadmap"`

---

## Self-review notes

- **Spec coverage:** identity modes (T8/9/10), bearer tokens (T4/9), domain gate (T8/9), roles + bootstrap + CLI (T3/9/14), retrieval-layer enforcement incl. agent tools (T6/7), source access levels (T5), allowlist + admin-only + removal (T11), upload-to-repo + fallback (T12/13), UI (T15), settings (T1), schema/migration (T2), docs (T16). Git-sync itself needs no code (existing `sync_folder`; sidecar is the deferred Deploy item).
- **Known sequencing nit:** Task 5's `delete_source` test references `list_documents(role=...)` (a Task 6 signature) — the step text gives the in-order alternative assertion.
- **Out of scope here:** ingestion limits, grounding, grep timeout, backup, CI, Docker (= item 3 plan), `.docx` (item 5), `/mcp` (item 6) — each gets its own plan, written just-in-time.
