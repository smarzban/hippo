# SP2 — Built-in Password Auth Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a fourth auth mode, `password`, so Hippo can be self-hosted without Google/GCP: users sign in with email + password, credentials are argon2id-hashed in the existing surrogate-keyed `users` table, lockout protects against brute force, and bearer tokens / roles keep working unchanged.

**Architecture:** Builds directly on SP1's `users(id PK, email UNIQUE, role)`. Adds a nullable `password_hash` plus lockout columns. `auth.py` gains `hash_password`/`verify_password` (argon2id, reduced-cost in tests). `api.py` gains a `password` branch in `verify_request` (session keyed by `user_id`, reusing the same `SessionMiddleware` oidc uses), `POST /auth/login` + `/auth/logout`, a public `GET /auth/config`, self-service `POST /me/password`, and admin reset `POST /users/{email}/password`. A break-glass `hippo user set-password` CLI is the headless bootstrap. The React SPA shows a password login screen when `auth_mode == "password"`.

**Tech Stack:** Python/FastAPI, `argon2-cffi` (new dep), starlette `SessionMiddleware`; React/Vite/Vitest. Tests zero-network; argon2 runs at reduced cost via a test profile.

**Spec:** `docs/superpowers/specs/2026-06-13-password-auth-design.md`

**No data migration (spec §8):** `db.py` creates the new columns in the fresh schema; existing dev DBs are recreated. `password` is added to the `auth_mode` Literal.

## Security invariants (must hold)

- **No default credentials, ever** — the owner is created by the SP3 wizard or the break-glass CLI; SP2 ships only the `set_password` primitive.
- **Generic login errors** — never leak "no such user" vs "wrong password" (no account enumeration).
- **Never log or return a password hash** — not in `/me`, `/users`, `/settings/status`, or logs.
- **argon2id**, per-hash salt (argon2-cffi handles it); password mode **requires `HIPPO_SECRET_KEY`** (session signing) — `serve`/`build_app` refuse to start without it.
- Bearer tokens remain the headless path in every mode (checked before the password branch).
- Tests never hit the network; argon2 at reduced cost (local CPU only).

## File structure

- `src/hippo/config.py` — add `password` to the `auth_mode` Literal.
- `src/hippo/db.py` — `users.password_hash TEXT NULL`, `users.failed_logins INTEGER NOT NULL DEFAULT 0`, `users.locked_until TEXT`.
- `src/hippo/auth.py` — `hash_password`, `verify_password`, `set_password_hasher` (test hook).
- `src/hippo/storage.py` — `set_password`, `get_credentials`, `get_user_by_id`, `record_failed_login`, `reset_login_state`.
- `src/hippo/api.py` — password branch in `verify_request`; `SessionMiddleware` for password mode; `GET /auth/config`, `POST /auth/login`, `POST /auth/logout`, `POST /me/password`, `POST /users/{email}/password`.
- `src/hippo/cli.py` — `hippo user set-password <email>`.
- `ui/src/auth.ts` — **NEW** pure helper(s) (`passwordChangeError`) + Vitest.
- `ui/src/App.tsx` — password login screen (when `auth_mode == "password"`), logout control, fetch `/auth/config`.
- `ui/src/Settings.tsx` — self-service password change; admin "Reset password" in Users tab.
- `pyproject.toml` — add `argon2-cffi`.
- Tests: `tests/test_auth.py`, `tests/test_password_auth.py` (new), `tests/test_storage.py`, `tests/test_cli.py`, `tests/conftest.py` (fast-argon2 fixture), `tests/test_env_example.py`/`test_config.py` (drift), `ui/src/auth.test.ts` (new).

---

### Task 1: Dependency, config Literal, schema columns, fast-argon2 test fixture

Lay the foundation: the argon2 dep, the `password` mode value, the three new `users` columns, and a session-scoped fixture so every later test hashes at reduced cost.

**Files:**
- Modify: `pyproject.toml`, `src/hippo/config.py`, `src/hippo/db.py`, `tests/conftest.py`
- Test: `tests/test_db.py`, `tests/test_config.py`

- [ ] **Step 1: Add the dependency**

In `pyproject.toml`, add to `dependencies` (after `"aiohttp>=3.9",`):

```toml
    "argon2-cffi>=23.1.0",
```

Run: `uv sync` (installs argon2-cffi). Expected: resolves and installs.

- [ ] **Step 2: Write the failing schema/config tests**

Add to `tests/test_db.py`:

```python
def test_users_has_password_and_lockout_columns(tmp_path):
    con = connect(tmp_path / "t.db", embedding_dim=32)
    cols = {r[1] for r in con.execute("PRAGMA table_info(users)")}
    assert {"password_hash", "failed_logins", "locked_until"} <= cols
```

Add to `tests/test_config.py` (a test asserting the new mode is accepted):

```python
def test_password_is_a_valid_auth_mode():
    from hippo.config import Settings
    s = Settings(_env_file=None, auth_mode="password")
    assert s.auth_mode == "password"
```

- [ ] **Step 3: Run to verify failure**

Run: `uv run pytest tests/test_db.py::test_users_has_password_and_lockout_columns tests/test_config.py::test_password_is_a_valid_auth_mode -q`
Expected: FAIL (columns absent; `password` not in the Literal → pydantic ValidationError).

- [ ] **Step 4: Add `password` to the auth_mode Literal**

In `src/hippo/config.py`, change:

```python
    auth_mode: Literal["none", "oidc", "iap"] = "none"
```

to:

```python
    auth_mode: Literal["none", "oidc", "iap", "password"] = "none"
```

- [ ] **Step 5: Add the columns to the users table**

In `src/hippo/db.py`, change the `users` table definition to:

```python
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    role TEXT NOT NULL DEFAULT 'user'
        CHECK (role IN ('user','admin','owner')),
    password_hash TEXT,                                  -- NULL for oidc/iap users
    failed_logins INTEGER NOT NULL DEFAULT 0,
    locked_until TEXT,                                   -- ISO ts; NULL = not locked
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
```

- [ ] **Step 6: Add the fast-argon2 fixture**

In `tests/conftest.py`, append:

```python
@pytest.fixture(autouse=True, scope="session")
def _fast_argon2():
    """Hash at minimal cost so the suite stays fast (argon2 is local CPU, never
    network). Production keeps the library defaults."""
    from argon2 import PasswordHasher

    from hippo.auth import set_password_hasher

    set_password_hasher(PasswordHasher(time_cost=1, memory_cost=8, parallelism=1))
    yield
```

> `set_password_hasher` is defined in Task 2; this fixture imports it lazily so the file still imports before Task 2 runs (the fixture body only executes when tests run, by which point Task 2 is done — within subagent execution Task 1's gate runs before Task 2, so temporarily this import would fail). To keep Task 1's gate green, add a minimal stub now in `auth.py`: see Step 6b.

- [ ] **Step 6b: Add a minimal hasher stub to `auth.py` (completed in Task 2)**

To keep Task 1 green before Task 2 lands the real hashing, add this to the END of `src/hippo/auth.py` now (Task 2 replaces/expands it):

```python
# --- password hashing (expanded in SP2 Task 2) ---
from argon2 import PasswordHasher as _PasswordHasher

_HASHER = _PasswordHasher()


def set_password_hasher(hasher) -> None:
    """Swap the argon2 hasher (tests use a reduced-cost profile)."""
    global _HASHER
    _HASHER = hasher
```

- [ ] **Step 7: Run the gate**

Run: `uv run pytest tests/test_db.py tests/test_config.py -q`
Expected: PASS. (Run the FULL suite too — `uv run pytest -q` — it should stay green: the new columns are additive and nothing reads them yet.)

- [ ] **Step 8: Commit**

```bash
git add pyproject.toml uv.lock src/hippo/config.py src/hippo/db.py src/hippo/auth.py tests/conftest.py tests/test_db.py tests/test_config.py
git commit -m "feat(auth): add password mode literal, users credential/lockout columns, argon2 dep (SP2)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: argon2 hashing in `auth.py`

A single hash/verify pair, the only place argon2 is touched.

**Files:**
- Modify: `src/hippo/auth.py`
- Test: `tests/test_auth.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_auth.py`:

```python
def test_hash_and_verify_password_roundtrip():
    from hippo.auth import hash_password, verify_password

    h = hash_password("correct horse battery staple")
    assert h != "correct horse battery staple"   # never plaintext
    assert h.startswith("$argon2")               # argon2id encoded hash
    assert verify_password(h, "correct horse battery staple") is True
    assert verify_password(h, "wrong password") is False


def test_verify_password_handles_garbage_hash():
    from hippo.auth import verify_password
    assert verify_password("not-a-real-hash", "whatever") is False
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_auth.py -k password -q`
Expected: FAIL (`hash_password`/`verify_password` not defined).

- [ ] **Step 3: Implement the hash/verify pair**

Replace the stub block added in Task 1 Step 6b at the end of `src/hippo/auth.py` with:

```python
# --- password hashing (SP2) ---
# argon2id (argon2-cffi default type). One module-level hasher; tests swap it for
# a reduced-cost profile via set_password_hasher. Never log or return a hash.
from argon2 import PasswordHasher as _PasswordHasher
from argon2.exceptions import Argon2Error

_HASHER = _PasswordHasher()


def set_password_hasher(hasher) -> None:
    """Swap the argon2 hasher (tests use a reduced-cost profile)."""
    global _HASHER
    _HASHER = hasher


def hash_password(password: str) -> str:
    """Return an argon2id encoded hash (includes the per-hash salt + params)."""
    return _HASHER.hash(password)


def verify_password(hashed: str, password: str) -> bool:
    """Constant-time verify. False on mismatch OR a malformed/foreign hash —
    never raises, so callers get a clean boolean and no enumeration signal."""
    try:
        return _HASHER.verify(hashed, password)
    except Argon2Error:
        return False
```

> `PasswordHasher.verify` returns `True` on success and raises `VerifyMismatchError` (an `Argon2Error` subclass) on mismatch; `InvalidHashError` (also `Argon2Error`) covers a garbage/foreign hash. Catching `Argon2Error` covers both.

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_auth.py -k password -q`
Expected: PASS (fast, thanks to the reduced-cost fixture).

- [ ] **Step 5: Commit**

```bash
git add src/hippo/auth.py tests/test_auth.py
git commit -m "feat(auth): argon2id hash_password/verify_password (SP2)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Credentials & lockout in `storage.py`

All credential SQL lives in Storage. Email is the public identifier; rows are surrogate-keyed.

**Files:**
- Modify: `src/hippo/storage.py`
- Test: `tests/test_storage.py` (or a focused `tests/test_password_auth.py`)

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_storage.py`:

```python
LOCKOUT_MAX = 5


def test_set_password_creates_user_and_stores_hash(store):
    store.set_password("alice@x.com", "hashed-1", role="admin")
    creds = store.get_credentials("alice@x.com")
    assert creds is not None
    assert creds["password_hash"] == "hashed-1" and creds["role"] == "admin"
    # set_password on an existing user updates the hash, keeps the role
    store.set_password("alice@x.com", "hashed-2")
    assert store.get_credentials("alice@x.com")["password_hash"] == "hashed-2"
    assert store.get_credentials("alice@x.com")["role"] == "admin"


def test_get_credentials_unknown_email_is_none(store):
    assert store.get_credentials("nobody@x.com") is None


def test_get_user_by_id_roundtrip(store):
    store.set_password("bob@x.com", "h", role="owner")
    uid = store.get_credentials("bob@x.com")["user_id"]
    assert store.get_user_by_id(uid) == ("bob@x.com", "owner")
    assert store.get_user_by_id(999999) is None


def test_lockout_after_max_failures_then_reset(store):
    store.set_password("eve@x.com", "h")
    for _ in range(LOCKOUT_MAX):
        store.record_failed_login("eve@x.com")
    creds = store.get_credentials("eve@x.com")
    assert creds["failed_logins"] >= LOCKOUT_MAX
    assert creds["locked_until"] is not None       # lock set
    assert store.is_locked("eve@x.com") is True
    store.reset_login_state("eve@x.com")           # successful login clears it
    creds = store.get_credentials("eve@x.com")
    assert creds["failed_logins"] == 0 and creds["locked_until"] is None
    assert store.is_locked("eve@x.com") is False
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_storage.py -k "password or lockout or credentials or user_by_id" -q`
Expected: FAIL (methods undefined).

- [ ] **Step 3: Implement the storage methods**

Add to the `# -- users / roles --` section of `src/hippo/storage.py` (after `list_users`):

```python
    LOCKOUT_MAX_FAILURES = 5
    LOCKOUT_MINUTES = 15

    def set_password(self, email: str, password_hash: str, *, role: str | None = None) -> None:
        """Create-or-update a local credential. Creates the user (with `role` or
        the default) if absent; on an existing user updates the hash and (only if
        `role` is given) the role. Clears any lockout state. The caller hashes."""
        email = _norm_email(email)
        if role is not None and role not in VALID_ROLES:
            raise ValueError(f"invalid role {role!r}; expected one of {VALID_ROLES}")
        with self._lock, self.con:
            row = self.con.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
            if row is None:
                self.con.execute(
                    "INSERT INTO users(email, role, password_hash) VALUES (?,?,?)",
                    (email, role or DEFAULT_ROLE, password_hash),
                )
            elif role is not None:
                self.con.execute(
                    "UPDATE users SET password_hash=?, role=?, failed_logins=0, "
                    "locked_until=NULL WHERE id=?",
                    (password_hash, role, row[0]),
                )
            else:
                self.con.execute(
                    "UPDATE users SET password_hash=?, failed_logins=0, "
                    "locked_until=NULL WHERE id=?",
                    (password_hash, row[0]),
                )

    def get_credentials(self, email: str) -> dict | None:
        """Return {user_id, email, role, password_hash, failed_logins, locked_until}
        for an email, or None if no such user. Used only by the login path."""
        email = _norm_email(email)
        with self._lock:
            row = self.con.execute(
                "SELECT id, email, role, password_hash, failed_logins, locked_until "
                "FROM users WHERE email=?", (email,),
            ).fetchone()
        if row is None:
            return None
        return {"user_id": row[0], "email": row[1], "role": row[2],
                "password_hash": row[3], "failed_logins": row[4], "locked_until": row[5]}

    def get_user_by_id(self, user_id: int) -> tuple[str, str] | None:
        """(email, role) for a surrogate id, or None. Used by the session auth path."""
        with self._lock:
            row = self.con.execute(
                "SELECT email, role FROM users WHERE id=?", (user_id,)).fetchone()
        return (row[0], row[1]) if row else None

    def record_failed_login(self, email: str) -> None:
        """Increment the failure counter; lock for LOCKOUT_MINUTES once it reaches
        LOCKOUT_MAX_FAILURES. Lock timestamp is DB-clock based for testability."""
        email = _norm_email(email)
        with self._lock, self.con:
            self.con.execute(
                "UPDATE users SET failed_logins = failed_logins + 1 WHERE email=?", (email,))
            self.con.execute(
                f"UPDATE users SET locked_until = datetime('now', '+{self.LOCKOUT_MINUTES} minutes') "
                "WHERE email=? AND failed_logins >= ?",
                (email, self.LOCKOUT_MAX_FAILURES),
            )

    def reset_login_state(self, email: str) -> None:
        """Clear the failure counter + lock (called on a successful login)."""
        email = _norm_email(email)
        with self._lock, self.con:
            self.con.execute(
                "UPDATE users SET failed_logins=0, locked_until=NULL WHERE email=?", (email,))

    def is_locked(self, email: str) -> bool:
        """True iff the account is currently within its lockout window."""
        email = _norm_email(email)
        with self._lock:
            row = self.con.execute(
                "SELECT locked_until > datetime('now') FROM users WHERE email=?", (email,)
            ).fetchone()
        return bool(row and row[0])
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_storage.py -k "password or lockout or credentials or user_by_id" -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/hippo/storage.py tests/test_storage.py
git commit -m "feat(storage): local credentials + lockout state (SP2)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Auth flow in `api.py` — login/logout/session + `verify_request`

Wire the `password` mode into request auth.

**Files:**
- Modify: `src/hippo/api.py`
- Test: `tests/test_password_auth.py` (new)

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_password_auth.py
import pytest
from fastapi.testclient import TestClient

from hippo.api import build_app
from hippo.auth import hash_password
from hippo.config import Settings


def _settings(tmp_path, **over):
    base = dict(_env_file=None, db_path=tmp_path / "t.db", embedding_model="fake",
                embedding_dim=32, enrich_enabled=False, auth_mode="password",
                secret_key="test-secret")
    base.update(over)
    return Settings(**base)


def _app_with_owner(tmp_path, email="owner@x.com", pw="s3cret-pass"):
    app = build_app(_settings(tmp_path))
    app.state.store.set_password(email, hash_password(pw), role="owner")
    return app


def test_password_mode_requires_secret_key(tmp_path):
    with pytest.raises(ValueError, match="HIPPO_SECRET_KEY"):
        build_app(_settings(tmp_path, secret_key=""))


def test_unauthenticated_is_401_and_auth_config_is_public(tmp_path):
    c = TestClient(_app_with_owner(tmp_path))
    assert c.get("/me").status_code == 401
    cfg = c.get("/auth/config")          # public, no secrets
    assert cfg.status_code == 200 and cfg.json() == {"auth_mode": "password"}


def test_login_success_sets_session_and_me_works(tmp_path):
    c = TestClient(_app_with_owner(tmp_path))
    r = c.post("/auth/login", json={"email": "owner@x.com", "password": "s3cret-pass"})
    assert r.status_code == 200 and r.json()["role"] == "owner"
    me = c.get("/me")                    # session cookie carried by the client
    assert me.status_code == 200 and me.json()["email"] == "owner@x.com"
    c.post("/auth/logout")
    assert c.get("/me").status_code == 401


def test_wrong_password_is_generic_401(tmp_path):
    c = TestClient(_app_with_owner(tmp_path))
    r = c.post("/auth/login", json={"email": "owner@x.com", "password": "nope"})
    assert r.status_code == 401
    assert "invalid" in r.json()["detail"].lower()
    # unknown user is the SAME generic error (no account enumeration)
    r2 = c.post("/auth/login", json={"email": "ghost@x.com", "password": "x"})
    assert r2.status_code == 401 and r2.json()["detail"] == r.json()["detail"]


def test_lockout_blocks_even_correct_password(tmp_path):
    c = TestClient(_app_with_owner(tmp_path))
    for _ in range(5):
        c.post("/auth/login", json={"email": "owner@x.com", "password": "wrong"})
    r = c.post("/auth/login", json={"email": "owner@x.com", "password": "s3cret-pass"})
    assert r.status_code == 401 and "locked" in r.json()["detail"].lower()


def test_bearer_token_still_works_in_password_mode(tmp_path):
    app = _app_with_owner(tmp_path)
    tok = app.state.store.create_token("owner@x.com")
    c = TestClient(app)
    assert c.get("/me", headers={"Authorization": f"Bearer {tok}"}).json()["role"] == "owner"
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_password_auth.py -q`
Expected: FAIL (no password branch / endpoints; `build_app` doesn't require secret_key for password).

- [ ] **Step 3: Add the password branch to `verify_request`**

In `src/hippo/api.py`, in `verify_request`, add a `password` branch BEFORE the final oidc session block. After the `iap` branch and before `email = request.session.get("email", "")`:

```python
        if settings.auth_mode == "password":
            uid = request.session.get("user_id")
            if not uid:
                raise HTTPException(status_code=401, detail="not signed in")
            found = store.get_user_by_id(uid)
            if found is None:
                request.session.clear()
                raise HTTPException(status_code=401, detail="not signed in")
            email, _role = found
            return _user_for(email)   # re-resolves role (bootstrap/admin_emails honored)
```

- [ ] **Step 4: Require SECRET_KEY + add SessionMiddleware + endpoints for password mode**

In `build_app`, alongside the existing `if settings.auth_mode == "oidc":` block, add a sibling block. Place it right AFTER the oidc block:

```python
    if settings.auth_mode == "password":
        if not settings.secret_key:
            raise ValueError("HIPPO_SECRET_KEY is required when HIPPO_AUTH_MODE=password")
        app.add_middleware(SessionMiddleware, secret_key=settings.secret_key,
                           https_only=settings.public_url.startswith("https"),
                           same_site="lax")

        @app.post("/auth/login")
        async def auth_login_password(request: Request):
            body = await request.json()
            email = (body.get("email") or "").strip().lower()
            password = body.get("password") or ""
            creds = store.get_credentials(email)
            # Generic failure for missing user / no local password / bad password.
            generic = HTTPException(status_code=401, detail="invalid email or password")
            if store.is_locked(email):
                raise HTTPException(status_code=401,
                    detail=f"account locked — try again in up to {store.LOCKOUT_MINUTES} minutes")
            if creds is None or not creds["password_hash"]:
                # still do nothing leak-y; no counter to bump on a non-user
                raise generic
            if not verify_password(creds["password_hash"], password):
                store.record_failed_login(email)
                raise generic
            store.reset_login_state(email)
            request.session["user_id"] = creds["user_id"]
            user = _user_for(email)
            return {"email": user.email, "role": user.role}

        @app.post("/auth/logout")
        async def auth_logout_password(request: Request):
            request.session.clear()
            return {"ok": True}
```

Add `verify_password` to the auth import at the top of `api.py`:

```python
from .auth import (AuthError, AuthenticatedUser, IapVerifier, check_domain,
                   hash_password, resolve_role, validate_google_id_token, verify_password)
```

(`hash_password` is used in Task 5.)

- [ ] **Step 5: Add the public `GET /auth/config`**

Add near `/me` (unauthenticated — reveals only the mode, never a secret):

```python
    @app.get("/auth/config")
    async def auth_config():
        return {"auth_mode": settings.auth_mode}
```

- [ ] **Step 6: Run to verify pass**

Run: `uv run pytest tests/test_password_auth.py -q`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add src/hippo/api.py tests/test_password_auth.py
git commit -m "feat(api): password auth mode — login/logout/session, lockout, /auth/config (SP2)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: Self-service password change + admin reset

**Files:**
- Modify: `src/hippo/api.py`
- Test: `tests/test_password_auth.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_password_auth.py`:

```python
MIN_PW_LEN = 8


def test_self_service_password_change(tmp_path):
    c = TestClient(_app_with_owner(tmp_path))
    c.post("/auth/login", json={"email": "owner@x.com", "password": "s3cret-pass"})
    # wrong current → 403
    assert c.post("/me/password", json={"current": "nope", "new": "brandnew-pass"}).status_code == 403
    # too short → 400
    assert c.post("/me/password", json={"current": "s3cret-pass", "new": "short"}).status_code == 400
    # ok
    assert c.post("/me/password", json={"current": "s3cret-pass", "new": "brandnew-pass"}).status_code == 200
    c.post("/auth/logout")
    assert c.post("/auth/login", json={"email": "owner@x.com", "password": "brandnew-pass"}).status_code == 200


def test_admin_reset_returns_secret_once_and_is_gated(tmp_path):
    app = _app_with_owner(tmp_path)
    app.state.store.set_password("dev@x.com", hash_password("old-pass-dev"), role="user")
    c = TestClient(app)
    c.post("/auth/login", json={"email": "owner@x.com", "password": "s3cret-pass"})
    r = c.post("/users/dev@x.com/password", json={})
    assert r.status_code == 200 and len(r.json()["password"]) >= MIN_PW_LEN
    new_pw = r.json()["password"]
    # the reset password actually works
    c.post("/auth/logout")
    assert c.post("/auth/login", json={"email": "dev@x.com", "password": new_pw}).status_code == 200


def test_admin_reset_requires_admin(tmp_path):
    app = _app_with_owner(tmp_path)
    app.state.store.set_password("dev@x.com", hash_password("p"), role="user")
    c = TestClient(app)
    c.post("/auth/login", json={"email": "dev@x.com", "password": "p"})  # rank user
    assert c.post("/users/owner@x.com/password", json={}).status_code == 403
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_password_auth.py -k "self_service or admin_reset" -q`
Expected: FAIL (routes undefined).

- [ ] **Step 3: Implement the endpoints**

Add a module-level constant near the other models in `api.py`:

```python
MIN_PASSWORD_LEN = 8
```

Add these routes (after the `/users/{email}/role` route). Both are only meaningful in `password` mode but are harmless otherwise; gate the admin reset with `require_admin` + tier authority:

```python
    @app.post("/me/password")
    async def change_own_password(request: Request,
                                  user: AuthenticatedUser = Depends(verify_request)):
        body = await request.json()
        current = body.get("current") or ""
        new = body.get("new") or ""
        creds = store.get_credentials(user.email)
        if creds is None or not creds["password_hash"] or not verify_password(
                creds["password_hash"], current):
            raise HTTPException(status_code=403, detail="current password is incorrect")
        if len(new) < MIN_PASSWORD_LEN:
            raise HTTPException(status_code=400,
                detail=f"new password must be at least {MIN_PASSWORD_LEN} characters")
        store.set_password(user.email, hash_password(new))
        return {"ok": True}

    @app.post("/users/{email}/password")
    async def admin_reset_password(email: str,
                                   user: AuthenticatedUser = Depends(require_admin)):
        target = email.strip().lower()
        creds = store.get_credentials(target)
        if creds is None:
            raise HTTPException(status_code=404, detail="user not found")
        if rank(creds["role"]) > rank(user.role):
            raise HTTPException(status_code=403, detail="cannot reset a user above your tier")
        new_pw = secrets.token_urlsafe(12)   # >= MIN_PASSWORD_LEN; shown once
        store.set_password(target, hash_password(new_pw))
        return {"email": target, "password": new_pw}
```

(`secrets` is already imported in `api.py`.)

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_password_auth.py -q`
Expected: PASS (whole file).

- [ ] **Step 5: Commit**

```bash
git add src/hippo/api.py tests/test_password_auth.py
git commit -m "feat(api): self-service password change + admin reset (SP2)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 6: Break-glass CLI `hippo user set-password`

The headless bootstrap / locked-out-owner escape hatch.

**Files:**
- Modify: `src/hippo/cli.py`
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_cli.py` (uses Typer's `CliRunner`; the existing file already imports `app` and a runner — match its pattern):

```python
def test_user_set_password_creates_working_credential(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from hippo.cli import app
    from hippo.config import Settings
    from hippo.db import connect
    from hippo.embeddings import FakeEmbedder
    from hippo.storage import Storage

    db = tmp_path / "t.db"
    monkeypatch.setenv("HIPPO_DB_PATH", str(db))
    monkeypatch.setenv("HIPPO_EMBEDDING_MODEL", "fake")
    monkeypatch.setenv("HIPPO_EMBEDDING_DIM", "32")
    runner = CliRunner()
    # password entered twice on the prompt
    result = runner.invoke(app, ["user", "set-password", "boss@x.com", "--role", "owner"],
                           input="hunter2-strong\nhunter2-strong\n")
    assert result.exit_code == 0, result.output

    store = Storage(connect(db, embedding_dim=32), FakeEmbedder(dim=32))
    from hippo.auth import verify_password
    creds = store.get_credentials("boss@x.com")
    assert creds["role"] == "owner"
    assert verify_password(creds["password_hash"], "hunter2-strong") is True
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_cli.py -k set_password -q`
Expected: FAIL (no `user set-password` command).

- [ ] **Step 3: Implement the CLI command**

In `src/hippo/cli.py`, add a `user` Typer group near the `role`/`token` groups:

```python
user_app = typer.Typer(help="Local user accounts (password auth).")
app.add_typer(user_app, name="user")


@user_app.command("set-password")
def user_set_password(
    email: str,
    role: str = typer.Option(None, help="role for a NEW user: user | admin | owner"),
):
    """Set (or reset) a local password for EMAIL. Prompts twice, no echo. Creates
    the user if absent. Break-glass bootstrap / locked-out-owner recovery."""
    from .auth import hash_password

    pw = typer.prompt("New password", hide_input=True, confirmation_prompt=True)
    if len(pw) < 8:
        typer.echo("password must be at least 8 characters", err=True)
        raise typer.Exit(1)
    store, _ = _store(Settings())
    try:
        store.set_password(email, hash_password(pw), role=role)
    except ValueError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1)
    typer.echo(f"password set for {email}" + (f" (role {role})" if role else ""))
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_cli.py -k set_password -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/hippo/cli.py tests/test_cli.py
git commit -m "feat(cli): break-glass 'hippo user set-password' (SP2)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 7: UI — login screen, logout, password change, admin reset

**Files:**
- Create: `ui/src/auth.ts`, `ui/src/auth.test.ts`
- Modify: `ui/src/App.tsx`, `ui/src/Settings.tsx`

- [ ] **Step 1: Write the failing Vitest**

```typescript
// ui/src/auth.test.ts
import { describe, expect, it } from "vitest";
import { passwordChangeError, MIN_PASSWORD_LEN } from "./auth";

describe("passwordChangeError", () => {
  it("requires all fields", () => {
    expect(passwordChangeError("", "newlongpass", "newlongpass")).toMatch(/current/i);
  });
  it("enforces minimum length", () => {
    expect(passwordChangeError("cur", "short", "short")).toMatch(new RegExp(`${MIN_PASSWORD_LEN}`));
  });
  it("requires confirmation match", () => {
    expect(passwordChangeError("cur", "newlongpass", "different")).toMatch(/match/i);
  });
  it("returns null when valid", () => {
    expect(passwordChangeError("cur", "newlongpass", "newlongpass")).toBeNull();
  });
});
```

- [ ] **Step 2: Run to verify failure**

Run: `cd ui && npx vitest run src/auth.test.ts`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement `ui/src/auth.ts`**

```typescript
export const MIN_PASSWORD_LEN = 8;

/** Client-side pre-check for the password-change form. Returns an error string
 *  or null when valid. The server re-validates (this is UX, not the gate). */
export function passwordChangeError(
  current: string,
  next: string,
  confirm: string,
): string | null {
  if (!current) return "Enter your current password.";
  if (next.length < MIN_PASSWORD_LEN) return `New password must be at least ${MIN_PASSWORD_LEN} characters.`;
  if (next !== confirm) return "New password and confirmation do not match.";
  return null;
}
```

- [ ] **Step 4: Run to verify pass**

Run: `cd ui && npx vitest run src/auth.test.ts`
Expected: PASS

- [ ] **Step 5: Add the password login screen in `App.tsx`**

Fetch `/auth/config` on mount and key the login screen off it. Add state + effect near the other `useState`/`useEffect` calls:

```tsx
  const [authMode, setAuthMode] = useState<string>("none");
  const [loginEmail, setLoginEmail] = useState("");
  const [loginPw, setLoginPw] = useState("");
  const [loginErr, setLoginErr] = useState("");

  useEffect(() => {
    fetch("/auth/config").then((r) => r.json()).then((c) => setAuthMode(c.auth_mode)).catch(() => {});
  }, []);

  async function passwordLogin(e: React.FormEvent) {
    e.preventDefault();
    setLoginErr("");
    const r = await fetch("/auth/login", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email: loginEmail, password: loginPw }),
    });
    if (r.ok) window.location.reload();
    else setLoginErr((await r.json().catch(() => ({}))).detail || "Sign-in failed.");
  }
```

Replace the existing `if (needsLogin) { ... Google ... }` block with a mode-aware one:

```tsx
  if (needsLogin) {
    return (
      <div className="app">
        <div className="empty signin">
          <span className="logo">{"\u{1F99B}"}</span>
          <h1>Hippo</h1>
          {authMode === "password" ? (
            <form className="login-form" onSubmit={passwordLogin}>
              <input type="email" placeholder="email" value={loginEmail} autoFocus
                onChange={(e) => setLoginEmail(e.target.value)} />
              <input type="password" placeholder="password" value={loginPw}
                onChange={(e) => setLoginPw(e.target.value)} />
              <button className="upload-btn" type="submit">Sign in</button>
              {loginErr && <p className="error">{loginErr}</p>}
            </form>
          ) : (
            <>
              <p>Sign in with your Google account to continue.</p>
              <a className="upload-btn" href="/auth/login">Sign in with Google</a>
            </>
          )}
        </div>
      </div>
    );
  }
```

In the header whoami area, extend the logout control to password mode (it currently shows a sign-out link only for oidc). Change the whoami block so logout appears for both `oidc` and `password`:

```tsx
            <span className="whoami">
              {me.email} ({me.role})
              {me.auth_mode === "oidc" && <> · <a href="/auth/logout">sign out</a></>}
              {me.auth_mode === "password" && <> · <button className="linklike"
                onClick={async () => { await fetch("/auth/logout", { method: "POST" }); window.location.reload(); }}>sign out</button></>}
            </span>
```

- [ ] **Step 6: Add self-service password change + admin reset in `Settings.tsx`**

Add a "Password" control to the Tokens panel area (or a small section visible in `password` mode). Keep it simple — a `PasswordPanel` rendered as part of the Tokens tab (always available to a logged-in user):

```tsx
import { passwordChangeError } from "./auth";

function PasswordPanel() {
  const [cur, setCur] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [note, setNote] = useState("");
  const submit = async () => {
    const err = passwordChangeError(cur, next, confirm);
    if (err) { setNote(err); return; }
    const r = await fetch("/me/password", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ current: cur, new: next }) });
    if (r.ok) { setNote("password changed"); setCur(""); setNext(""); setConfirm(""); }
    else setNote(await r.json().then((b) => b.detail).catch(() => `error ${r.status}`));
  };
  return (
    <div className="panel">
      <p>Change your password</p>
      <div className="row">
        <input type="password" placeholder="current" value={cur} onChange={(e) => setCur(e.target.value)} />
        <input type="password" placeholder="new" value={next} onChange={(e) => setNext(e.target.value)} />
        <input type="password" placeholder="confirm" value={confirm} onChange={(e) => setConfirm(e.target.value)} />
        <button onClick={submit}>Update</button>
        <span className="note">{note}</span>
      </div>
    </div>
  );
}
```

Render `<PasswordPanel />` under the Tokens tab (below `TokensPanel`), shown only when the user authenticated with a password. Pass `authMode` into `Settings` (add it to the `Settings` props and the `<Settings .../>` call in App.tsx) and render the panel when `authMode === "password"`.

In `UsersPanel`, add a "Reset password" action that calls `POST /users/{email}/password` and shows the returned secret once:

```tsx
  const [resetPw, setResetPw] = useState<{ email: string; pw: string } | null>(null);
  const reset = async (email: string) => {
    const r = await fetch(`/users/${encodeURIComponent(email)}/password`, { method: "POST",
      headers: { "Content-Type": "application/json" }, body: "{}" });
    if (r.ok) { const b = await r.json(); setResetPw({ email, pw: b.password }); }
    else setNote(await r.json().then((b) => b.detail).catch(() => `error ${r.status}`));
  };
```

Add a "Reset password" `<button onClick={() => reset(u.email)}>` in each user row, and render `resetPw` once (email + the new password + a Done button), mirroring the token-secret reveal pattern.

- [ ] **Step 7: Build + Vitest**

Run from `ui/`:
```
npm run build
npm test
```
Expected: build clean; vitest green (auth + folders + citations).

- [ ] **Step 8: Commit**

```bash
git add ui/src/auth.ts ui/src/auth.test.ts ui/src/App.tsx ui/src/Settings.tsx ui/src/app.css
git commit -m "feat(ui): password login screen, logout, password change + admin reset (SP2)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 8: Docs + config drift

**Files:**
- Modify: `README.md`, `CLAUDE.md`, `.env.example`, `tests/test_env_example.py` (only if it fails), `tests/test_config.py`

- [ ] **Step 1: Confirm `.env.example` drift guard**

SP2 adds **no new `HIPPO_` settings** (lockout/session numbers are hardcoded defaults; the spec marks them "configurable later"). So `Settings.model_fields` is unchanged except the `auth_mode` Literal value, which the drift guard does not inspect. Update the `auth_mode` comment in `.env.example` to list `none|oidc|iap|password` and add a short note that `password` mode requires `HIPPO_SECRET_KEY`.

Run: `uv run pytest tests/test_env_example.py -q`
Expected: PASS.

- [ ] **Step 2: Update docs**

- `README.md`: document the `password` auth mode (email + password, argon2id, lockout 5/15min, 7-day session, requires `HIPPO_SECRET_KEY`, no default credentials), the login screen, self-service change + admin reset, and the break-glass `hippo user set-password <email>` CLI. Note that the SP3 wizard (not yet built) will be the normal bootstrap; for now the CLI bootstraps the first owner.
- `CLAUDE.md`: update `auth.py` (hash/verify), `api.py` (password mode + `/auth/login`/`/auth/logout`/`/auth/config`/`/me/password`/`/users/{email}/password`), `storage.py` (credentials + lockout), `cli.py` (`user set-password`), and the config line (`auth_mode` now includes `password`). Update the test-count and the State block to record SP2.

- [ ] **Step 3: Full gate + commit**

Run:
```
uv run pytest -q
cd ui && npm test && npm run build
```
Expected: all green.

```bash
git add README.md CLAUDE.md .env.example tests/
git commit -m "docs: password auth mode across README/CLAUDE/.env (SP2)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage** (against `2026-06-13-password-auth-design.md`):
- §1/§2 password mode, nullable `password_hash`, argon2id, login/logout/session, lockout, self-service change, admin reset, break-glass CLI → Tasks 1–7.
- §3 identity & storage (password_hash NULL for oidc/iap, lockout columns, argon2 reduced-cost test profile) → Tasks 1, 2, 3.
- §4 auth flow (bearer first, then password session by `user_id`, 401 → login screen; `serve` refuses without SECRET_KEY) → Task 4.
- §5 security (no default creds, generic errors, lockout 5/15, session via SessionMiddleware, bearer unchanged) → Tasks 4, 5, 6.
- §6 bootstrap = SP3 wizard (out of scope) + break-glass CLI → Task 6.
- §7 API + UI (login screen, logout, self-service change, admin reset shown once) → Tasks 4, 5, 7.
- §8 fresh schema, no migration → Task 1.
- §9 testing (roundtrip, login success/lockout/correct-pw-blocked, bearer-still-works, self-service requires current, admin reset gated + once, no-secret-key refusal, CLI) → Tasks 2–6.

**Invariants:** no default credentials; generic 401s (verified by `test_wrong_password_is_generic_401` comparing unknown-user vs wrong-password details); hash never returned (no endpoint returns `password_hash`; `/me` unchanged); SECRET_KEY required (`test_password_mode_requires_secret_key`); bearer works in password mode; argon2 reduced-cost in tests; all SQL in storage.py; retrieval `role` untouched. ✓

**Type consistency:** `get_credentials` dict keys (`user_id`, `password_hash`, `role`, `failed_logins`, `locked_until`) used consistently in api.py login; `set_password(email, password_hash, *, role=None)` signature matches all call sites (login bootstrap via CLI, admin reset, self-service); `LOCKOUT_MAX_FAILURES`/`LOCKOUT_MINUTES`/`MIN_PASSWORD_LEN` referenced consistently; UI `MIN_PASSWORD_LEN` mirrors the server's 8. ✓

**Note:** lockout uses the DB clock (`datetime('now','+15 minutes')`) so the lockout test needs no time-mocking — five failures set the lock and the sixth (correct-password) attempt is blocked immediately.
