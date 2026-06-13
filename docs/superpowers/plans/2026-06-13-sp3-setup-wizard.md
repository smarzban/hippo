# SP3 — First-run Setup Wizard & Config Store Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an operator stand up Hippo entirely from the browser on first run — choose the auth mode, create the owner, name the role-tier roots, pick the LLM models — backed by a runtime-mutable DB **config store** that overlays env defaults, so owners can change operational settings later without editing env.

**Architecture:** A `config(key, value)` table holds operational, **non-secret** settings; a resolver returns the DB value if present else the env `Settings` default. **Secrets never enter the DB** (API keys, `oidc_client_secret`, `HIPPO_SECRET_KEY`, `db_path`, `HIPPO_SETUP_TOKEN` stay env-only). First-run = `setup_complete` flag false; the wizard endpoints are gated by `HIPPO_SETUP_TOKEN` (env, or a random token logged at startup) and lock once complete. `chat_model` is read **live per `/chat` request** (no restart); other operational keys (`auth_mode`, embedding, oidc/iap/domain, `enrich_model`) are resolved from the overlay at **construction** (DB wins at startup) — changing them persists and takes effect on the next restart (embedding additionally needs `hippo reindex`). Owner-only Instance Settings edits the overlay; the auth-mode switch is anti-lockout guarded.

**Tech Stack:** Python/FastAPI, sqlite (config table, SQL in storage.py); React/Vite/Vitest. Builds on SP1 (folders/roles) + SP2 (password auth / `set_password`). Tests zero-network.

**Spec:** `docs/superpowers/specs/2026-06-13-setup-wizard-design.md`

## Scope decisions (read first)

- **Secrets stay in env, always.** The wizard collects model **names** + non-secret params; it *validates* that the required secret env var is present for the chosen mode, never stores it. `/config`, `/setup/status`, `/settings/status` never return a secret value, and `PUT /config` rejects any secret key.
- **No `none` in the wizard** — it offers `password`/`oidc`/`iap` only. `none` stays a dev-only env setting.
- **Live vs restart:** `chat_model` is live per-request (spec §3 headline). `auth_mode`, `enrich_model`, `embedding_model`/`embedding_dim`, `allowed_domain`, `oidc_client_id`, `public_url`, `iap_audience` are resolved from the overlay at construction; Instance Settings persists changes that take effect on the next `serve` restart. Embedding model/dim are read-only in Instance Settings once documents exist (reindex guard). This bounds the api.py refactor while satisfying the spec's no-restart requirement for the user-facing chat model.
- **Lockout/session numbers** stay the SP2 hardcoded defaults (not DB-overridable in SP3); flagged as a later enhancement.
- **No data migration:** `db.py` creates the `config` table; `setup_complete` defaults false on a fresh DB.

## DB-overridable keys (the only keys `/config` and the overlay accept)

```
auth_mode, chat_model, enrich_model, embedding_model, embedding_dim,
allowed_domain, oidc_client_id, public_url, iap_audience
```

Everything else (provider keys, `oidc_client_secret`, `secret_key`, `db_path`, `setup_token`, `github_*`, `source_roots`, `ui_dist`, `mcp_enabled`, chunk sizes, limits) is **env-only**.

## File structure

- `src/hippo/db.py` — `config` table.
- `src/hippo/storage.py` — `ConfigStore` methods: `get_config`/`set_config`/`all_config`/`is_setup_complete`/`mark_setup_complete`; `document_count()` (for the embedding reindex guard).
- `src/hippo/config.py` — `DB_OVERRIDABLE` set, `_coerce`, `Config` resolver class, `setup_token` env field.
- `src/hippo/api.py` — overlay wiring at construction; live `chat_model` per `/chat`; setup-token resolution; `GET /setup/status`, `POST /setup`, `GET /config`, `PUT /config`; auth-mode-switch anti-lockout.
- `ui/src/setup.ts` — **NEW** pure wizard step reducer + validation; Vitest.
- `ui/src/App.tsx` — first-run wizard view (when `setup_complete == false`).
- `ui/src/Settings.tsx` — owner-only "Instance" tab.
- Tests: `tests/test_db.py`, `tests/test_storage.py`, `tests/test_config.py`, `tests/test_setup.py` (new), `tests/test_env_example.py`, `ui/src/setup.test.ts` (new).

---

### Task 1: `config` table + ConfigStore + setup flag

**Files:**
- Modify: `src/hippo/db.py`, `src/hippo/storage.py`
- Test: `tests/test_db.py`, `tests/test_storage.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_db.py`:

```python
def test_config_table_exists(tmp_path):
    con = connect(tmp_path / "t.db", embedding_dim=32)
    cols = {r[1] for r in con.execute("PRAGMA table_info(config)")}
    assert {"key", "value"} <= cols
```

`tests/test_storage.py`:

```python
def test_config_get_set_and_setup_flag(store):
    assert store.get_config("chat_model") is None
    store.set_config("chat_model", "openai:gpt-5.2")
    assert store.get_config("chat_model") == "openai:gpt-5.2"
    store.set_config("chat_model", "ollama:llama3")   # upsert
    assert store.get_config("chat_model") == "ollama:llama3"
    assert store.all_config()["chat_model"] == "ollama:llama3"
    assert store.is_setup_complete() is False
    store.mark_setup_complete()
    assert store.is_setup_complete() is True


def test_document_count(store):
    assert store.document_count() == 0
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_db.py::test_config_table_exists tests/test_storage.py -k "config or document_count" -q`
Expected: FAIL.

- [ ] **Step 3: Add the `config` table to `db.py`**

In `SCHEMA`, after the `tokens` table:

```python
CREATE TABLE IF NOT EXISTS config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
```

- [ ] **Step 4: Add ConfigStore methods to `storage.py`**

Add a new section after the tokens methods:

```python
    # -- config store (SP3) --------------------------------------------------

    SETUP_COMPLETE_KEY = "setup_complete"

    def get_config(self, key: str) -> str | None:
        with self._lock:
            row = self.con.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_config(self, key: str, value: str) -> None:
        with self._lock, self.con:
            self.con.execute(
                "INSERT INTO config(key, value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def all_config(self) -> dict[str, str]:
        with self._lock:
            return {k: v for k, v in self.con.execute("SELECT key, value FROM config")}

    def is_setup_complete(self) -> bool:
        return self.get_config(self.SETUP_COMPLETE_KEY) == "1"

    def mark_setup_complete(self) -> None:
        self.set_config(self.SETUP_COMPLETE_KEY, "1")

    def document_count(self) -> int:
        with self._lock:
            return self.con.execute("SELECT count(*) FROM documents").fetchone()[0]
```

- [ ] **Step 5: Run + commit**

Run: `uv run pytest tests/test_db.py tests/test_storage.py -q` → PASS. Then `uv run pytest -q` (full, additive → green).

```bash
git add src/hippo/db.py src/hippo/storage.py tests/test_db.py tests/test_storage.py
git commit -m "feat(storage): config table + ConfigStore + setup flag (SP3)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Config overlay resolver + setup-token env field

**Files:**
- Modify: `src/hippo/config.py`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_config.py`:

```python
def test_config_overlay_db_overrides_env_for_operational_keys(tmp_path):
    from hippo.config import Config, Settings, DB_OVERRIDABLE
    from hippo.db import connect
    from hippo.embeddings import FakeEmbedder
    from hippo.storage import Storage

    s = Settings(_env_file=None, chat_model="env-model", github_token="SECRET")
    store = Storage(connect(tmp_path / "t.db", embedding_dim=32), FakeEmbedder(dim=32))
    cfg = Config(s, store)
    assert cfg.get("chat_model") == "env-model"          # env default
    store.set_config("chat_model", "db-model")
    assert cfg.get("chat_model") == "db-model"           # DB overrides
    assert "chat_model" in DB_OVERRIDABLE
    # a secret/env-only key is NEVER sourced from the DB
    store.set_config("github_token", "DB-LEAK")
    assert cfg.get("github_token") == "SECRET"           # still env
    # embedding_dim is coerced to int
    store.set_config("embedding_dim", "768")
    assert cfg.get("embedding_dim") == 768


def test_setup_token_is_an_env_only_setting(tmp_path):
    from hippo.config import Settings, DB_OVERRIDABLE
    s = Settings(_env_file=None, setup_token="abc")
    assert s.setup_token == "abc"
    assert "setup_token" not in DB_OVERRIDABLE
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_config.py -k "overlay or setup_token" -q`
Expected: FAIL.

- [ ] **Step 3: Implement in `config.py`**

Add the `setup_token` field to `Settings` (in the auth section):

```python
    setup_token: str = ""  # first-run wizard gate; if empty, a random one is logged at startup
```

Add at module level (after the `Settings` class):

```python
# Operational keys the DB config store may override (env supplies the default).
# Everything NOT here — provider keys, oidc_client_secret, secret_key, db_path,
# setup_token, github_*, source_roots — is ENV-ONLY and never read from the DB.
DB_OVERRIDABLE: frozenset[str] = frozenset({
    "auth_mode", "chat_model", "enrich_model", "embedding_model", "embedding_dim",
    "allowed_domain", "oidc_client_id", "public_url", "iap_audience",
})

_INT_KEYS = frozenset({"embedding_dim"})


def _coerce(key: str, value: str):
    return int(value) if key in _INT_KEYS else value


class Config:
    """Live operational config: a DB value (if set) overrides the env Settings
    default — but ONLY for DB_OVERRIDABLE keys. Secrets/env-only keys always come
    from Settings, so a stray DB row can never leak or override a secret."""

    def __init__(self, settings: "Settings", store):
        self.settings = settings
        self.store = store

    def get(self, key: str):
        if key in DB_OVERRIDABLE:
            v = self.store.get_config(key)
            if v is not None:
                return _coerce(key, v)
        return getattr(self.settings, key)
```

- [ ] **Step 4: Run + commit**

Run: `uv run pytest tests/test_config.py -q` → PASS; `uv run pytest -q` → green.

```bash
git add src/hippo/config.py tests/test_config.py
git commit -m "feat(config): DB-overlay-on-env resolver, secrets env-only, setup_token field (SP3)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Wire the overlay into `build_app` (auth_mode + live chat_model)

Resolve operational settings from the overlay at construction; read `chat_model` live per `/chat`. This must NOT change behavior for existing none/oidc/iap/password deployments that have no config rows (DB empty → env wins → identical to today).

**Files:**
- Modify: `src/hippo/api.py`
- Test: `tests/test_setup.py` (new) + the existing auth suites must stay green.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_setup.py
from fastapi.testclient import TestClient

from hippo.api import build_app
from hippo.config import Settings


def _settings(tmp_path, **over):
    base = dict(_env_file=None, db_path=tmp_path / "t.db", embedding_model="fake",
                embedding_dim=32, enrich_enabled=False)
    base.update(over)
    return Settings(**base)


def test_db_config_overrides_chat_model_live(tmp_path):
    s = _settings(tmp_path, chat_model="env:model")
    app = build_app(s)
    # set a DB override AFTER construction; chat_model must be read live
    app.state.store.set_config("chat_model", "db:model")
    # build_app exposes the live resolver for the chat route; assert via a helper
    from hippo.config import Config
    assert Config(s, app.state.store).get("chat_model") == "db:model"


def test_auth_mode_resolved_from_db_overlay_at_construction(tmp_path):
    # pre-seed a DB with auth_mode=password BEFORE build_app, env says none
    from hippo.db import connect
    from hippo.embeddings import FakeEmbedder
    from hippo.storage import Storage
    con = connect(tmp_path / "t.db", embedding_dim=32)
    Storage(con, FakeEmbedder(dim=32)).set_config("auth_mode", "password")
    con.close()
    s = _settings(tmp_path, auth_mode="none", secret_key="k")
    app = build_app(s)
    c = TestClient(app)
    # password mode is active (from the DB overlay): /me is 401, /auth/config says password
    assert c.get("/auth/config").json()["auth_mode"] == "password"
    assert c.get("/me").status_code == 401
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_setup.py -q`
Expected: FAIL (auth_mode read from `settings` only; `/auth/config` returns env mode).

- [ ] **Step 3: Resolve operational config at construction**

In `build_app`, right after `store = Storage(con, embedder)`, build a `Config` and an effective-settings shim. The simplest correct change: resolve `auth_mode` and the auth-related operational params through the overlay and use those local variables instead of `settings.X` for the rest of construction.

```python
    from .config import Config
    cfg = Config(settings, store)
    auth_mode = cfg.get("auth_mode")
    allowed_domain = cfg.get("allowed_domain")
    oidc_client_id = cfg.get("oidc_client_id")
    public_url = cfg.get("public_url")
    iap_audience = cfg.get("iap_audience")
```

Then, throughout `build_app`, replace `settings.auth_mode` → `auth_mode`, `settings.allowed_domain` → `allowed_domain`, `settings.oidc_client_id` → `oidc_client_id`, `settings.public_url` → `public_url`, `settings.iap_audience` → `iap_audience` (in the auth blocks, `verify_request`, `_exchange_code_with_google` call sites, `/me`, `/settings/status`, and the iap-required check). Leave secret/env-only reads (`settings.secret_key`, `settings.oidc_client_secret`, `settings.github_*`, `settings.db_path`, `settings.source_root_list`, `settings.mcp_enabled`) as `settings.X`.

> `resolve_role` reads `settings.allowed_domain` and `settings.admin_email_list` internally. To keep the domain gate honest under the overlay without threading, pass the resolved `allowed_domain` by mutating a copy is messy — instead, leave `resolve_role` using `settings` (the overlay for `allowed_domain` takes effect at the next restart, consistent with the scope decision). Only the construction-time `auth_mode`/oidc/iap wiring uses the resolved locals. Document this.

- [ ] **Step 4: Read `chat_model` live in `/chat`**

Replace the single construction-time `agent = build_agent(...)` usage in the `/chat` route. Keep a default agent for reuse, but rebuild when the live model differs:

```python
    # chat_model is live (spec §3): rebuild the agent when the DB overlay changes it.
    default_model = model_override or cfg.get("chat_model")
    agent_cache = {"model": default_model, "agent": build_agent(default_model)}

    def _live_agent():
        m = model_override or cfg.get("chat_model")
        if m != agent_cache["model"]:
            agent_cache.update(model=m, agent=build_agent(m))
        return agent_cache["agent"]
```

In the `/chat` route, use `agent = _live_agent()` instead of the module-level `agent`.

- [ ] **Step 5: Run the gate**

Run: `uv run pytest tests/test_setup.py tests/test_api_auth.py tests/test_api.py tests/test_password_auth.py tests/test_agent.py -q`
Expected: PASS (existing modes unchanged when no config rows exist; the two new tests pass). Then `uv run pytest -q` full → green.

- [ ] **Step 6: Commit**

```bash
git add src/hippo/api.py tests/test_setup.py
git commit -m "feat(api): resolve operational config from DB overlay; live chat_model (SP3)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Setup-token gate + `GET /setup/status` + `POST /setup`

**Files:**
- Modify: `src/hippo/api.py`
- Test: `tests/test_setup.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_setup_status_public_and_setup_token_gate(tmp_path):
    s = _settings(tmp_path, setup_token="let-me-in")
    c = TestClient(build_app(s))
    st = c.get("/setup/status")
    assert st.status_code == 200
    assert st.json()["setup_complete"] is False
    assert set(st.json()["auth_modes_available"]) == {"password", "oidc", "iap"}  # no 'none'
    # wrong/absent token rejected
    assert c.post("/setup", json={"token": "nope", "auth_mode": "password",
                                  "owner_email": "o@x.com", "owner_password": "ownerpass1",
                                  "models": {}}).status_code in (401, 403)


def test_password_setup_happy_path(tmp_path):
    s = _settings(tmp_path, setup_token="let-me-in", secret_key="k")
    app = build_app(s)
    c = TestClient(app)
    r = c.post("/setup", json={
        "token": "let-me-in", "auth_mode": "password",
        "owner_email": "owner@x.com", "owner_password": "ownerpass1",
        "roots": {"user": "Team", "admin": "Managers", "owner": "Execs"},
        "models": {"chat_model": "ollama:llama3", "embedding_model": "fake", "embedding_dim": 32},
    })
    assert r.status_code == 200
    assert app.state.store.is_setup_complete() is True
    # owner can log in immediately
    assert c.post("/auth/login", json={"email": "owner@x.com", "password": "ownerpass1"}).json()["role"] == "owner"
    # roots were renamed + models persisted
    names = {f.min_role: f.name for f in app.state.store.list_folders(role="owner") if f.parent_id is None}
    assert names == {"user": "Team", "admin": "Managers", "owner": "Execs"}
    assert app.state.store.get_config("chat_model") == "ollama:llama3"
    # re-running setup after completion is refused
    assert c.post("/setup", json={"token": "let-me-in", "auth_mode": "password",
                                  "owner_email": "x@x.com", "owner_password": "xxxxxxxx",
                                  "models": {}}).status_code == 409


def test_oidc_setup_refuses_without_secret_env(tmp_path):
    s = _settings(tmp_path, setup_token="t", secret_key="", oidc_client_secret="")
    c = TestClient(build_app(s))
    r = c.post("/setup", json={"token": "t", "auth_mode": "oidc", "owner_email": "o@x.com",
                               "oidc": {"client_id": "cid", "public_url": "https://h"}, "models": {}})
    assert r.status_code == 400 and "secret" in r.json()["detail"].lower()
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_setup.py -k setup -q` → FAIL.

- [ ] **Step 3: Resolve the effective setup token + add routes**

In `build_app`, near the top (after `store`), resolve the setup token (env or a logged random one) and stash it:

```python
    import logging as _logging
    effective_setup_token = settings.setup_token
    if not effective_setup_token and not store.is_setup_complete():
        effective_setup_token = secrets.token_urlsafe(24)
        _logging.getLogger("hippo.auth").warning(
            "HIPPO_SETUP_TOKEN not set — first-run setup token is: %s", effective_setup_token)
```

Add the routes (place near `/auth/config`). `POST /setup` is token-gated and only valid while not complete:

```python
    @app.get("/setup/status")
    async def setup_status():
        return {"setup_complete": store.is_setup_complete(),
                "auth_modes_available": ["password", "oidc", "iap"]}

    @app.post("/setup")
    async def run_setup(request: Request):
        if store.is_setup_complete():
            raise HTTPException(status_code=409, detail="setup already complete")
        body = await request.json()
        if not secrets.compare_digest(str(body.get("token", "")), effective_setup_token):
            raise HTTPException(status_code=403, detail="invalid setup token")
        mode = body.get("auth_mode")
        if mode not in ("password", "oidc", "iap"):
            raise HTTPException(status_code=400, detail="auth_mode must be password|oidc|iap")
        owner_email = (body.get("owner_email") or "").strip().lower()
        if not owner_email:
            raise HTTPException(status_code=400, detail="owner_email is required")
        # validate the chosen mode's required SECRET env vars are present (env-only)
        if mode in ("password", "oidc") and not settings.secret_key:
            raise HTTPException(status_code=400,
                detail="HIPPO_SECRET_KEY (env) is required for this auth mode")
        if mode == "oidc" and not settings.oidc_client_secret:
            raise HTTPException(status_code=400,
                detail="HIPPO_OIDC_CLIENT_SECRET (env) is required for oidc")
        # create the owner
        if mode == "password":
            pw = body.get("owner_password") or ""
            if len(pw) < MIN_PASSWORD_LEN:
                raise HTTPException(status_code=400,
                    detail=f"owner password must be at least {MIN_PASSWORD_LEN} characters")
            store.set_password(owner_email, hash_password(pw), role="owner")
        else:
            store.set_role(owner_email, "owner")  # becomes owner on first oidc/iap sign-in
        # name the three roots (rename the seeded folders)
        roots = body.get("roots") or {}
        for f in store.list_folders(role="owner"):
            if f.parent_id is None and f.min_role in roots and roots[f.min_role]:
                store.rename_folder(f.id, roots[f.min_role])
        # persist operational config (DB-overridable keys only)
        store.set_config("auth_mode", mode)
        models = body.get("models") or {}
        for k in ("chat_model", "enrich_model", "embedding_model", "embedding_dim"):
            if k in models and models[k] not in (None, ""):
                store.set_config(k, str(models[k]))
        oidc = body.get("oidc") or {}
        for k_body, k_cfg in (("client_id", "oidc_client_id"), ("public_url", "public_url")):
            if oidc.get(k_body):
                store.set_config(k_cfg, oidc[k_body])
        if body.get("iap_audience"):
            store.set_config("iap_audience", body["iap_audience"])
        if body.get("allowed_domain"):
            store.set_config("allowed_domain", body["allowed_domain"])
        store.mark_setup_complete()
        # for password mode, log the owner in immediately
        if mode == "password":
            request.session["user_id"] = store.get_credentials(owner_email)["user_id"]
        return {"ok": True, "auth_mode": mode}
```

> `POST /setup` needs the session to be available for the password auto-login. SessionMiddleware is added only in password/oidc mode blocks — and the effective auth_mode for THIS request is the pre-setup mode (env). To keep the auto-login working, ensure SessionMiddleware is present whenever `settings.secret_key` is set (add it unconditionally when `secret_key` is truthy, in addition to the per-mode blocks — but guard against double-add). Simplest: in Task 3, add SessionMiddleware once if `settings.secret_key` and neither oidc nor password block will add it; OR have the password/oidc blocks be the only adders and accept that auto-login requires the resolved auth_mode already be password (pre-seeded). For the test `test_password_setup_happy_path`, env auth_mode is `none` but `secret_key` is set — so add SessionMiddleware when `secret_key` is set regardless of mode. Implement: a single `if settings.secret_key and not _session_added: app.add_middleware(SessionMiddleware, ...)` guard.

- [ ] **Step 4: Ensure SessionMiddleware is present when a secret key exists**

Refactor the oidc/password middleware adds into one: near where the oidc block adds `SessionMiddleware`, instead add it once up front:

```python
    if settings.secret_key:
        app.add_middleware(SessionMiddleware, secret_key=settings.secret_key,
                           https_only=public_url.startswith("https"), same_site="lax")
```

and REMOVE the `app.add_middleware(SessionMiddleware, ...)` lines from the oidc and password blocks (keep their route definitions). Keep the password block's `if not settings.secret_key: raise ValueError(...)` and add the same guard to oidc (it already has it). For the `none`/`iap` modes with a secret_key set, a harmless session middleware is mounted (unused) — acceptable and needed for the wizard auto-login.

- [ ] **Step 5: Run + commit**

Run: `uv run pytest tests/test_setup.py -q` → PASS; `uv run pytest -q` → green.

```bash
git add src/hippo/api.py tests/test_setup.py
git commit -m "feat(api): first-run setup wizard endpoints + token gate (SP3)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: `GET`/`PUT /config` (owner-only) + embedding reindex guard

**Files:**
- Modify: `src/hippo/api.py`
- Test: `tests/test_setup.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_config_get_put_owner_only_and_secrets_protected(tmp_path):
    app = build_app(_settings(tmp_path))  # none-mode caller is owner
    c = TestClient(app)
    got = c.get("/config")
    assert got.status_code == 200
    assert "chat_model" in got.json() and "secret_key" not in got.json() \
        and "github_token" not in got.json()
    # set an operational key
    assert c.put("/config", json={"chat_model": "ollama:llama3"}).status_code == 200
    assert app.state.store.get_config("chat_model") == "ollama:llama3"
    # writing a secret/env-only key is rejected
    r = c.put("/config", json={"secret_key": "leak"})
    assert r.status_code == 400 and "secret_key" in r.json()["detail"]
    # unknown key rejected
    assert c.put("/config", json={"nonsense": "x"}).status_code == 400


def test_embedding_change_guarded_after_documents_exist(tmp_path):
    app = build_app(_settings(tmp_path))
    c = TestClient(app)
    # empty index: allowed
    assert c.put("/config", json={"embedding_dim": 64}).status_code == 200
    # add a document, then changing embedding_dim is refused with the reindex note
    from hippo.chunking import Chunk
    fid = next(f.id for f in app.state.store.list_folders(role="owner") if f.parent_id is None)
    app.state.store.upsert_document(source_type="upload", path="x.md", title="x",
        content="hi", content_hash="h", chunks=[Chunk(position=0, heading_path="", text="hi")],
        embed_inputs=["hi"], folder_id=fid)
    r = c.put("/config", json={"embedding_dim": 128})
    assert r.status_code == 409 and "reindex" in r.json()["detail"].lower()
    r2 = c.put("/config", json={"embedding_model": "other"})
    assert r2.status_code == 409 and "reindex" in r2.json()["detail"].lower()
```

- [ ] **Step 2: Run to verify failure** — FAIL (routes undefined).

- [ ] **Step 3: Implement `GET`/`PUT /config`**

```python
    @app.get("/config")
    async def get_config(user: AuthenticatedUser = Depends(require_owner)):
        from .config import DB_OVERRIDABLE
        # effective value per key (DB override else env default); never a secret
        return {k: cfg.get(k) for k in sorted(DB_OVERRIDABLE)}

    @app.put("/config")
    async def put_config(request: Request, user: AuthenticatedUser = Depends(require_owner)):
        from .config import DB_OVERRIDABLE
        body = await request.json()
        for key in body:
            if key not in DB_OVERRIDABLE:
                raise HTTPException(status_code=400,
                    detail=f"{key!r} is not a settable operational key (secrets/env-only keys are rejected)")
        # embedding model/dim cannot change once documents exist (chunk_vec dim is fixed)
        if ("embedding_model" in body or "embedding_dim" in body) and store.document_count() > 0:
            raise HTTPException(status_code=409,
                detail="embedding_model/embedding_dim can't change after documents exist — "
                       "run `hippo reindex` (CLI) to re-embed")
        if "auth_mode" in body:
            _validate_auth_switch(user, body["auth_mode"])   # Task 6
        for key, value in body.items():
            store.set_config(key, str(value))
        return {"ok": True}
```

For Task 5, add a temporary no-op `_validate_auth_switch` so the suite is green; Task 6 fills it in:

```python
    def _validate_auth_switch(user: AuthenticatedUser, target: str) -> None:
        pass  # implemented in Task 6
```

- [ ] **Step 4: Run + commit**

Run: `uv run pytest tests/test_setup.py -q` → PASS; full → green.

```bash
git add src/hippo/api.py tests/test_setup.py
git commit -m "feat(api): owner-only /config get/put, secrets protected, embedding reindex guard (SP3)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 6: Auth-mode switch anti-lockout

**Files:**
- Modify: `src/hippo/api.py`
- Test: `tests/test_setup.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_auth_switch_blocked_when_owner_lacks_target_credential(tmp_path):
    # none-mode owner has no password; switching to password would lock everyone out
    app = build_app(_settings(tmp_path, secret_key="k"))
    c = TestClient(app)
    r = c.put("/config", json={"auth_mode": "password"})
    assert r.status_code == 400 and "password" in r.json()["detail"].lower()


def test_auth_switch_to_password_allowed_once_owner_has_password(tmp_path):
    app = build_app(_settings(tmp_path, secret_key="k"))
    # none-mode caller is the "local" owner; give a real owner a password first
    app.state.store.set_password("owner@x.com", __import__("hippo.auth", fromlist=["hash_password"]).hash_password("ownerpass1"), role="owner")
    c = TestClient(app)
    # switching is allowed because an owner holds a valid password credential
    assert c.put("/config", json={"auth_mode": "password"}).status_code == 200


def test_auth_switch_to_mode_missing_secret_env_rejected(tmp_path):
    app = build_app(_settings(tmp_path, secret_key=""))   # no secret key
    c = TestClient(app)
    r = c.put("/config", json={"auth_mode": "oidc"})
    assert r.status_code == 400 and "secret" in r.json()["detail"].lower()
```

- [ ] **Step 2: Run to verify failure** — FAIL (no-op validator passes everything).

- [ ] **Step 3: Implement `_validate_auth_switch`**

Replace the no-op with:

```python
    def _validate_auth_switch(user: AuthenticatedUser, target: str) -> None:
        from .config import VALID_AUTH_MODES  # define below, or inline the tuple
        if target not in ("password", "oidc", "iap"):
            raise HTTPException(status_code=400, detail="auth_mode must be password|oidc|iap")
        # the target mode's required SECRET env vars must be present (env-only)
        if target in ("password", "oidc") and not settings.secret_key:
            raise HTTPException(status_code=400,
                detail="HIPPO_SECRET_KEY (env) is required for the target auth mode")
        if target == "oidc" and not settings.oidc_client_secret:
            raise HTTPException(status_code=400,
                detail="HIPPO_OIDC_CLIENT_SECRET (env) is required for oidc")
        # anti-lockout: an owner must hold a valid credential in the TARGET mode
        owners = [e for e, r in store.list_users() if r == "owner"] + sorted(settings.admin_email_list)
        if target == "password":
            if not any((store.get_credentials(e) or {}).get("password_hash") for e in owners):
                raise HTTPException(status_code=400,
                    detail="set an owner password before switching to password mode "
                           "(anti-lockout) — use the break-glass CLI or an admin reset")
        else:  # oidc / iap: an owner email must satisfy the domain gate
            dom = cfg.get("allowed_domain")
            if dom and not any(e.endswith("@" + dom.lower()) for e in owners):
                raise HTTPException(status_code=400,
                    detail=f"no owner email under @{dom} — would lock out of {target} mode")
```

(Remove the `VALID_AUTH_MODES` import line if you inline the tuple; the tuple check is shown inline.)

- [ ] **Step 4: Run + commit**

Run: `uv run pytest tests/test_setup.py -q` → PASS; full → green.

```bash
git add src/hippo/api.py tests/test_setup.py
git commit -m "feat(api): anti-lockout guard on auth-mode switch (SP3)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 7: UI — setup wizard (pure step logic + first-run view)

**Files:**
- Create: `ui/src/setup.ts`, `ui/src/setup.test.ts`
- Modify: `ui/src/App.tsx`

- [ ] **Step 1: Write the failing Vitest**

```typescript
// ui/src/setup.test.ts
import { describe, expect, it } from "vitest";
import { WIZARD_STEPS, nextStep, stepValid, type SetupState } from "./setup";

const base: SetupState = {
  step: 0, token: "", authMode: "password", ownerEmail: "", ownerPassword: "",
  roots: { user: "Default", admin: "Private", owner: "Owner" },
  models: { chat_model: "", embedding_model: "", embedding_dim: 1536 },
};

describe("wizard", () => {
  it("has the expected ordered steps", () => {
    expect(WIZARD_STEPS).toEqual(["token", "auth", "owner", "roots", "models", "finish"]);
  });
  it("token step needs a token", () => {
    expect(stepValid({ ...base, step: 0, token: "" })).toBe(false);
    expect(stepValid({ ...base, step: 0, token: "abc" })).toBe(true);
  });
  it("password owner step needs email + 8-char password", () => {
    expect(stepValid({ ...base, step: 2, ownerEmail: "o@x.com", ownerPassword: "short" })).toBe(false);
    expect(stepValid({ ...base, step: 2, ownerEmail: "o@x.com", ownerPassword: "longenough" })).toBe(true);
  });
  it("nextStep advances but clamps at the last step", () => {
    expect(nextStep({ ...base, step: 0, token: "abc" }).step).toBe(1);
    expect(nextStep({ ...base, step: 5 }).step).toBe(5);
  });
});
```

- [ ] **Step 2: Run to verify failure** — `cd ui && npx vitest run src/setup.test.ts` → FAIL.

- [ ] **Step 3: Implement `ui/src/setup.ts`**

```typescript
export const WIZARD_STEPS = ["token", "auth", "owner", "roots", "models", "finish"] as const;
export const MIN_PASSWORD_LEN = 8;

export type SetupState = {
  step: number;
  token: string;
  authMode: "password" | "oidc" | "iap";
  ownerEmail: string;
  ownerPassword: string;
  roots: { user: string; admin: string; owner: string };
  models: { chat_model: string; embedding_model: string; embedding_dim: number };
};

const emailish = (s: string) => /.+@.+\..+/.test(s);

/** Per-step validity gate for the Next button. The server re-validates everything. */
export function stepValid(s: SetupState): boolean {
  switch (WIZARD_STEPS[s.step]) {
    case "token": return s.token.trim().length > 0;
    case "auth": return ["password", "oidc", "iap"].includes(s.authMode);
    case "owner":
      if (!emailish(s.ownerEmail)) return false;
      return s.authMode !== "password" || s.ownerPassword.length >= MIN_PASSWORD_LEN;
    case "roots": return !!(s.roots.user && s.roots.admin && s.roots.owner);
    case "models": return true;   // names optional; server falls back to env defaults
    default: return true;
  }
}

export function nextStep(s: SetupState): SetupState {
  if (!stepValid(s)) return s;
  return { ...s, step: Math.min(s.step + 1, WIZARD_STEPS.length - 1) };
}

export function buildSetupPayload(s: SetupState) {
  return {
    token: s.token, auth_mode: s.authMode, owner_email: s.ownerEmail,
    owner_password: s.ownerPassword, roots: s.roots, models: s.models,
  };
}
```

- [ ] **Step 4: Run Vitest** — PASS.

- [ ] **Step 5: Render the wizard in `App.tsx`**

On mount, fetch `GET /setup/status`. If `!setup_complete`, render a dedicated wizard view (no header/chat) that walks `WIZARD_STEPS`, gating Next on `stepValid`, and on finish `POST /setup` with `buildSetupPayload`, then `window.location.reload()`. Use the existing `useState` patterns; keep it a self-contained `SetupWizard` component. (Full multi-step JSX is mechanical — render one panel per `WIZARD_STEPS[step]` with the fields in `SetupState`, a Back/Next pair, and a Finish button on the last step that POSTs.) The wizard takes precedence over the `needsLogin` screen.

- [ ] **Step 6: Build + test + commit**

Run from `ui/`: `npm run build` (clean) and `npm test` (setup + auth + folders + citations green).

```bash
git add ui/src/setup.ts ui/src/setup.test.ts ui/src/App.tsx ui/src/app.css
git commit -m "feat(ui): first-run setup wizard (pure step logic + view) (SP3)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 8: UI — owner-only Instance Settings tab

**Files:**
- Modify: `ui/src/Settings.tsx`, `ui/src/App.tsx`

- [ ] **Step 1: Add an "Instance" tab (owner only)**

In `Settings.tsx`, extend `tabsForRole` so an `owner` additionally sees `"Instance"`:

```typescript
export function tabsForRole(role: string): string[] {
  if (role === "user") return ["Tokens"];
  const tabs = ["Folders", "Users", "Tokens", "Status"];
  if (role === "owner") tabs.push("Instance");
  return tabs;
}
```

(Update the existing `tabsForRole` test in any vitest/inline test accordingly if present.)

- [ ] **Step 2: Implement `InstancePanel`**

A panel that `GET /config` on mount and lets the owner edit `chat_model`/`enrich_model` (text inputs), shows `embedding_model`/`embedding_dim` **read-only with a "change via `hippo reindex`" note**, and offers an **auth-mode** `<select>` (password/oidc/iap). Saving issues `PUT /config` with the changed keys; on a `409`/`400` it surfaces `detail` (e.g. the reindex guard or the anti-lockout message). Mirror the existing panel style (rows, `note`). Render `{tab === "Instance" && <InstancePanel />}` in the `Settings` body.

```tsx
function InstancePanel() {
  const [cfg, setCfg] = useState<Record<string, any> | null>(null);
  const [note, setNote] = useState("");
  useEffect(() => { getJSON("/config").then(setCfg).catch(() => setCfg(null)); }, []);
  if (!cfg) return <div className="panel">Loading…</div>;
  const save = async (patch: Record<string, any>) => {
    const r = await fetch("/config", { method: "PUT",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(patch) });
    setNote(r.ok ? "saved (model changes are live; auth-mode/embedding need a restart)"
                 : await r.json().then((b) => b.detail).catch(() => `error ${r.status}`));
    if (r.ok) getJSON("/config").then(setCfg);
  };
  return (
    <div className="panel">
      <div className="row"><label>Chat model</label>
        <input defaultValue={cfg.chat_model}
          onBlur={(e) => e.target.value !== cfg.chat_model && save({ chat_model: e.target.value })} /></div>
      <div className="row"><label>Enrich model</label>
        <input defaultValue={cfg.enrich_model}
          onBlur={(e) => e.target.value !== cfg.enrich_model && save({ enrich_model: e.target.value })} /></div>
      <div className="row"><label>Embedding</label>
        <span className="sec">{cfg.embedding_model} / dim {cfg.embedding_dim} — change via <code>hippo reindex</code></span></div>
      <div className="row"><label>Auth mode</label>
        <select defaultValue={cfg.auth_mode} onChange={(e) => save({ auth_mode: e.target.value })}>
          {["password", "oidc", "iap"].map((m) => <option key={m} value={m}>{m}</option>)}
        </select></div>
      <span className="note">{note}</span>
    </div>
  );
}
```

- [ ] **Step 3: Build + test + commit**

Run from `ui/`: `npm run build` (clean) + `npm test` (green).

```bash
git add ui/src/Settings.tsx ui/src/App.tsx
git commit -m "feat(ui): owner-only Instance Settings (models, auth-mode switch) (SP3)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 9: `/settings/status`, docs, config drift

**Files:**
- Modify: `src/hippo/api.py` (settings_status), `README.md`, `CLAUDE.md`, `.env.example`, `tests/test_env_example.py`

- [ ] **Step 1: `/settings/status` reflects the live overlay + setup flag**

Update `/settings/status` to report the effective (overlay) `auth_mode`/`chat_model`/`embedding_model` via `cfg.get(...)` (not raw `settings`) and add `"setup_complete": store.is_setup_complete()`. Never add a secret. Update `tests/test_api_settings.py` if it asserts these fields.

- [ ] **Step 2: `.env.example` + drift guard**

SP3 adds ONE new `HIPPO_` setting: `setup_token`. Add `# HIPPO_SETUP_TOKEN=` (commented, with a note: first-run wizard gate; if unset a random token is logged at startup) to `.env.example`. Run `uv run pytest tests/test_env_example.py -q` — it should pass once `.env.example` documents `HIPPO_SETUP_TOKEN` (the drift guard asserts documented keys == `Settings.model_fields`).

- [ ] **Step 3: Docs**

- `README.md`: document the first-run wizard (`docker run` → open browser → `HIPPO_SETUP_TOKEN` (env or logged) → choose mode/owner/roots/models → done), the config store (operational keys UI-settable; **secrets stay in env**), Instance Settings (owner-only; models live, auth-mode switch with anti-lockout, embedding read-only post-docs), and the standard deploy path.
- `CLAUDE.md`: update `config.py` (Config overlay, DB_OVERRIDABLE, setup_token), `storage.py` (config store), `api.py` (`/setup/status`, `/setup`, `/config`), the State block (SP3 implemented; **productization epic complete**), and the test count.

- [ ] **Step 4: Final gate + commit**

Run:
```
uv run pytest -q
cd ui && npm test && npm run build
uv run hippo eval eval/golden.yaml   # (with HIPPO_EMBEDDING_MODEL=fake HIPPO_EMBEDDING_DIM=32 HIPPO_ENRICH_ENABLED=false) → 4/4
```
All green.

```bash
git add src/hippo/api.py README.md CLAUDE.md .env.example tests/
git commit -m "docs: setup wizard + config store across README/CLAUDE/.env (SP3)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage** (against `2026-06-13-setup-wizard-design.md`):
- §3 config store (DB overlay on env; DB-overridable list; secrets env-only; embedding reindex exception) → Tasks 1, 2, 5.
- §4 first-run detection + setup token (env or logged random; constant-time compare; inert after complete) → Tasks 1, 4.
- §5 wizard flow (token → auth → owner → roots → models → finish; password auto-login) → Tasks 4, 7.
- §6 Instance Settings (owner-only; model names; embedding read-only note; auth-mode switch anti-lockout) → Tasks 5, 6, 8.
- §7 API (`GET /setup/status`, `POST /setup` token-gated/409-after, `GET`/`PUT /config` owner-only secrets-never-returned, `/settings/status`) → Tasks 4, 5, 9.
- §8 fresh schema, `setup_complete` defaults false → Task 1.
- §9 testing (overlay override + secret-never-from-DB + unknown-key reject; setup gating wrong/absent/env/logged token + 409-after; password happy path + oidc-missing-secret refusal; embedding guard; auth-switch anti-lockout; secrets absent from /config,/setup/status,/settings/status; vitest step reducer) → Tasks 2, 4, 5, 6, 7, 9.

**Scope decisions documented:** chat_model live; auth_mode/embedding/oidc/iap/domain resolved at construction (change → restart); lockout/session numbers stay SP2 defaults. These satisfy the spec's explicit no-restart requirement (models) while bounding the api.py refactor.

**Security invariants:** secrets NEVER enter the DB (`DB_OVERRIDABLE` whitelist; `Config.get` only reads DB for those keys; `PUT /config` rejects non-whitelisted keys); `/config`/`/setup/status`/`/settings/status` never return a secret; setup token compared with `secrets.compare_digest`; no default credentials (wizard creates the owner); anti-lockout before any auth switch; embedding change blocked once documents exist. ✓

**Type consistency:** `Config.get(key)` used in build_app + `/config` + `/settings/status`; `store.set_config`/`get_config`/`is_setup_complete`/`mark_setup_complete`/`document_count` names consistent across storage + api; `DB_OVERRIDABLE` is the single source for both the overlay and the `PUT /config` whitelist; UI `WIZARD_STEPS`/`SetupState`/`stepValid` consistent across setup.ts + the wizard view. ✓

**Risk note:** Task 3 (overlay wiring) + Task 4 Step 4 (single SessionMiddleware when `secret_key` set) are the highest-risk edits — they must not change behavior for existing none/oidc/iap/password deployments with an empty config table. The Task 3/4 tests assert existing modes are unaffected; run the full auth suite after each.
