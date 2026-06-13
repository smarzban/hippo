# Settings UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Roadmap item 9 — a Settings view in the React app (Sources / Users & Roles / Tokens / Status) over new admin+token HTTP endpoints, with self-service tokens and admin-only management. Design: `../specs/2026-06-13-settings-ui-design.md`.

**Architecture:** New FastAPI endpoints (`/users`, `/tokens`, `/sources/{id}/resync`, `/settings/status`) reusing existing `Storage` methods plus two new ones (`list_all_tokens`, `revoke_token_any`); admin routes behind `require_admin`, token routes behind `verify_request` and scoped to the caller's email. Frontend adds a gear-toggle Settings view (`ui/src/Settings.tsx`) with role-gated tabs — no router. SQL stays in `storage.py`; tests zero-network via `TestClient` + bearer tokens.

**Tech Stack:** FastAPI, pydantic, sqlite (existing); React 19 + fetch (existing UI).

**Hard rules:** tests zero-network; no SQL outside `storage.py`; retrieval keeps keyword-only `role`; TDD; commit per green step.

---

### Task 1: Storage — `list_all_tokens` + `revoke_token_any`

**Files:**
- Modify: `src/hippo/storage.py` (near `list_tokens`/`revoke_token`, ~lines 299-308)
- Test: `tests/test_storage_tokens.py` (create) or append to the existing token test file if present

- [ ] **Step 1: Write the failing test**

```python
from hippo.db import connect
from hippo.embeddings import FakeEmbedder
from hippo.storage import Storage


def _store(tmp_path):
    return Storage(connect(tmp_path / "h.db", embedding_dim=8), FakeEmbedder(dim=8))


def test_list_all_tokens_spans_users(tmp_path):
    s = _store(tmp_path)
    s.create_token("a@x.com", "laptop")
    s.create_token("b@x.com", "ci")
    rows = s.list_all_tokens()
    emails = {r[1] for r in rows}              # (id, email, name, created_at, last_used_at)
    assert emails == {"a@x.com", "b@x.com"}
    assert all(len(r) == 5 for r in rows)


def test_revoke_token_any_ignores_owner(tmp_path):
    s = _store(tmp_path)
    s.create_token("a@x.com", "laptop")
    tok_id = s.list_all_tokens()[0][0]
    assert s.revoke_token_any(tok_id) is True       # admin revokes without owning it
    assert s.list_all_tokens() == []
    assert s.revoke_token_any(tok_id) is False      # already gone
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_storage_tokens.py -v`
Expected: FAIL — `AttributeError: 'Storage' object has no attribute 'list_all_tokens'`.

- [ ] **Step 3: Implement (study `list_tokens`/`revoke_token` first for the exact table/columns)**

Open `src/hippo/storage.py` lines ~299-308 to see the `tokens` table columns used by `list_tokens(email)` and `revoke_token(token_id, email)`. Mirror them. Add inside the `Storage` class, holding `self._lock` exactly as the neighbours do:

```python
    def list_all_tokens(self) -> list[tuple[int, str, str, str, str | None]]:
        """All users' tokens (admin view): (id, email, name, created_at, last_used_at).
        Never returns the token secret — only the stored hash exists."""
        with self._lock:
            return [
                (r[0], r[1], r[2], r[3], r[4])
                for r in self._con.execute(
                    "SELECT id, email, name, created_at, last_used_at "
                    "FROM tokens ORDER BY email, id"
                ).fetchall()
            ]

    def revoke_token_any(self, token_id: int) -> bool:
        """Delete a token by id without the owner-email scope (admin revoke).
        Returns True if a row was deleted."""
        with self._lock:
            cur = self._con.execute("DELETE FROM tokens WHERE id = ?", (token_id,))
            self._con.commit()
            return cur.rowcount > 0
```

> IMPLEMENTER: match the REAL column names and the REAL lock attribute (`self._lock`/`self._con` or whatever the file uses) and the commit pattern used by the adjacent `create_token`/`revoke_token`. The SQL shape above is the intent; align it to the file.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_storage_tokens.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/hippo/storage.py tests/test_storage_tokens.py
git commit -m "feat: Storage.list_all_tokens + revoke_token_any (admin token view)"
```

---

### Task 2: API — Users & Roles endpoints

**Files:**
- Modify: `src/hippo/api.py` (add routes near the sources routes, ~line 346; add a `RoleIn` model near `SourceIn`)
- Test: `tests/test_api_settings.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_api_settings.py` (mirror `tests/test_api_auth.py`'s `_settings` helper and bearer-token pattern — `app.state.store` holds the Storage):

```python
from fastapi.testclient import TestClient
from hippo.api import build_app
from hippo.config import Settings


def _settings(tmp_path, **kw):
    base = dict(db_path=tmp_path / "h.db", embedding_model="fake", embedding_dim=8,
                enrich_enabled=False, auth_mode="iap", iap_audience="aud",
                admin_emails="boss@x.com")
    base.update(kw)
    return Settings(_env_file=None, **base)


def _app(tmp_path, **kw):
    # iap mode with no IAP key_fetcher => only bearer tokens authenticate (zero-network).
    from hippo.auth import IapVerifier
    app = build_app(_settings(tmp_path, **kw), iap_verifier=IapVerifier("aud", key_fetcher=lambda: {}))
    return app, app.state.store


def _bearer(store, email):
    return {"Authorization": f"Bearer {store.create_token(email)}"}


def test_users_list_and_set_role_admin_only(tmp_path):
    app, store = _app(tmp_path)
    store.ensure_user("dev@x.com")
    admin, dev = _bearer(store, "boss@x.com"), _bearer(store, "dev@x.com")
    c = TestClient(app)
    # developer is forbidden
    assert c.get("/users", headers=dev).status_code == 403
    # admin lists + promotes
    assert c.get("/users", headers=admin).status_code == 200
    assert c.put("/users/dev@x.com/role", json={"role": "manager"}, headers=admin).status_code == 200
    assert any(u["email"] == "dev@x.com" and u["role"] == "manager"
               for u in c.get("/users", headers=admin).json())


def test_set_role_rejects_invalid_and_self_demotion(tmp_path):
    app, store = _app(tmp_path)
    admin = _bearer(store, "boss@x.com")
    c = TestClient(app)
    assert c.put("/users/dev@x.com/role", json={"role": "wizard"}, headers=admin).status_code == 400
    # anti-lockout: admin cannot demote their own account
    assert c.put("/users/boss@x.com/role", json={"role": "developer"}, headers=admin).status_code == 400
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_api_settings.py -v`
Expected: FAIL — 404 on `/users` (route doesn't exist) so assertions fail.

- [ ] **Step 3: Implement**

Add near `SourceIn` (top of `api.py`):

```python
class RoleIn(BaseModel):
    role: Literal["developer", "manager", "admin"]
```

Add routes inside `build_app` after the sources routes (~line 346), using the existing `require_admin`:

```python
    @app.get("/users")
    async def list_users(user: AuthenticatedUser = Depends(require_admin)):
        return [{"email": e, "role": r} for e, r in store.list_users()]

    @app.put("/users/{email}/role")
    async def set_user_role(email: str, body: RoleIn,
                            user: AuthenticatedUser = Depends(require_admin)):
        target = email.strip().lower()
        if target == user.email and body.role != "admin":
            raise HTTPException(status_code=400,
                detail="you can't remove your own admin role")
        store.set_role(target, body.role)
        return {"email": target, "role": body.role}
```

(`RoleIn`'s `Literal` makes FastAPI return 422 for unknown roles; the test asserts 400 — change the test to `in (400, 422)` OR validate manually and raise 400. Pick manual validation to keep a clean 400: declare `body: dict` and check `body.get("role") in {"developer","manager","admin"}` else 400. IMPLEMENTER: choose one and make the test match.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_api_settings.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/hippo/api.py tests/test_api_settings.py
git commit -m "feat: /users list + role endpoints (admin, anti-lockout)"
```

---

### Task 3: API — Tokens endpoints (self-service + admin)

**Files:**
- Modify: `src/hippo/api.py` (add routes; add a `TokenIn` model)
- Test: `tests/test_api_settings.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_api_settings.py`:

```python
def test_tokens_self_service_and_secret_once(tmp_path):
    app, store = _app(tmp_path)
    dev = _bearer(store, "dev@x.com")
    c = TestClient(app)
    created = c.post("/tokens", json={"name": "laptop"}, headers=dev)
    assert created.status_code == 200
    body = created.json()
    assert body["token"].startswith("hk_")          # secret returned once
    # listing shows metadata only — never the secret
    listed = c.get("/tokens", headers=dev).json()
    assert any(t["id"] == body["id"] and t["name"] == "laptop" for t in listed)
    assert all("token" not in t and "hk_" not in str(t.values()) for t in listed)


def test_tokens_cross_user_revoke_blocked_for_dev_allowed_for_admin(tmp_path):
    app, store = _app(tmp_path)
    dev, admin = _bearer(store, "dev@x.com"), _bearer(store, "boss@x.com")
    c = TestClient(app)
    other_id = int(c.post("/tokens", json={"name": "x"}, headers=admin).json()["id"])  # admin's token
    # developer cannot delete someone else's token
    assert c.delete(f"/tokens/{other_id}", headers=dev).status_code == 404
    # admin can
    assert c.delete(f"/tokens/{other_id}", headers=admin).status_code == 200


def test_tokens_all_view_is_admin_only(tmp_path):
    app, store = _app(tmp_path)
    dev, admin = _bearer(store, "dev@x.com"), _bearer(store, "boss@x.com")
    c = TestClient(app)
    c.post("/tokens", json={"name": "d"}, headers=dev)
    assert c.get("/tokens?all=true", headers=dev).status_code == 403
    all_rows = c.get("/tokens?all=true", headers=admin).json()
    assert any(t.get("email") == "dev@x.com" for t in all_rows)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_api_settings.py -k tokens -v`
Expected: FAIL — token routes don't exist.

- [ ] **Step 3: Implement**

Add `TokenIn` near `SourceIn`:

```python
class TokenIn(BaseModel):
    name: str = ""
```

Add routes inside `build_app` (after the users routes):

```python
    @app.get("/tokens")
    async def list_tokens(all: bool = False,
                          user: AuthenticatedUser = Depends(verify_request)):
        if all:
            if user.role != "admin":
                raise HTTPException(status_code=403, detail="admin only")
            return [{"id": i, "email": e, "name": n, "created_at": c, "last_used_at": lu}
                    for i, e, n, c, lu in store.list_all_tokens()]
        return [{"id": i, "name": n, "created_at": c, "last_used_at": lu}
                for i, n, c, lu in store.list_tokens(user.email)]

    @app.post("/tokens")
    async def create_token(body: TokenIn, user: AuthenticatedUser = Depends(verify_request)):
        secret = store.create_token(user.email, body.name)   # tied to caller -> caller's role
        tok = store.list_tokens(user.email)[-1]               # newest row for the id
        return {"id": tok[0], "token": secret}

    @app.delete("/tokens/{token_id}")
    async def delete_token(token_id: int, user: AuthenticatedUser = Depends(verify_request)):
        ok = (store.revoke_token_any(token_id) if user.role == "admin"
              else store.revoke_token(token_id, user.email))
        if not ok:
            raise HTTPException(status_code=404, detail="token not found")
        return {"revoked": token_id}
```

> IMPLEMENTER: confirm `create_token` returns the plaintext and that the id can be recovered (the last row for that email, or a return-value tweak). If `create_token` already returns enough to get the id, simplify. Keep the SQL in storage.py — don't query the table from api.py.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_api_settings.py -k tokens -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/hippo/api.py tests/test_api_settings.py
git commit -m "feat: /tokens self-service + admin endpoints (secret returned once)"
```

---

### Task 4: API — `/sources/{id}/resync`, `/settings/status`, SPA RESERVED

**Files:**
- Modify: `src/hippo/api.py`
- Test: `tests/test_api_settings.py`

- [ ] **Step 1: Write the failing test**

Append:

```python
def test_resync_known_and_unknown(tmp_path):
    app, store = _app(tmp_path)
    admin = _bearer(store, "boss@x.com")
    sid = store.register_source("folder", str(tmp_path), access="everyone")
    c = TestClient(app)
    r = c.post(f"/sources/{sid}/resync", headers=admin)
    assert r.status_code == 200 and "report" in r.json()
    assert c.post("/sources/99999/resync", headers=admin).status_code == 404
    assert c.post(f"/sources/{sid}/resync", headers=_bearer(store, "dev@x.com")).status_code == 403


def test_status_admin_only_and_no_secrets(tmp_path):
    app, store = _app(tmp_path, chat_model="openai:gpt-5.2")
    admin, dev = _bearer(store, "boss@x.com"), _bearer(store, "dev@x.com")
    c = TestClient(app)
    assert c.get("/settings/status", headers=dev).status_code == 403
    st = c.get("/settings/status", headers=admin).json()
    assert st["auth_mode"] == "iap" and st["chat_model"] == "openai:gpt-5.2"
    assert set(st["counts"]) == {"documents", "sources", "users"}
    assert "hk_" not in str(st) and "secret" not in str(st).lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_api_settings.py -k "resync or status" -v`
Expected: FAIL — routes don't exist.

- [ ] **Step 3: Implement**

```python
    @app.post("/sources/{source_id}/resync")
    async def resync_source(source_id: int, user: AuthenticatedUser = Depends(require_admin)):
        match = next((s for s in store.list_sources(role="admin") if s[0] == source_id), None)
        if match is None:
            raise HTTPException(status_code=404, detail="source not found")
        location = match[2]  # (id, kind, location, access)
        report = await run_in_threadpool(
            sync_folder, Path(location), store, max_chars=settings.chunk_max_chars,
            overlap_chars=settings.chunk_overlap_chars, enricher=enricher,
            max_doc_chars=settings.max_doc_chars,
        )
        return {"report": {"added": report.added, "updated": report.updated,
                           "skipped": report.skipped, "removed": report.removed,
                           "failed": report.failed}}

    @app.get("/settings/status")
    async def settings_status(user: AuthenticatedUser = Depends(require_admin)):
        return {
            "auth_mode": settings.auth_mode,
            "chat_model": settings.chat_model,
            "embedding_model": settings.embedding_model,
            "repos": {
                "team": bool(settings.github_token and settings.github_docs_repo),
                "managers": bool(settings.github_token and settings.github_managers_repo),
            },
            "mcp_enabled": settings.mcp_enabled,
            "slack_enabled": settings.slack_enabled,
            "counts": {
                "documents": len(store.list_documents(role="admin")),
                "sources": len(store.list_sources(role="admin")),
                "users": len(store.list_users()),
            },
        }
```

Update the SPA `RESERVED` tuple (~line 362) to add the new prefixes:

```python
            RESERVED = ("auth", "chat", "ingest", "documents", "sources", "me",
                        "users", "tokens", "settings",
                        "health", "openapi.json", "docs", "redoc", "assets", "mcp")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_api_settings.py -v`
Expected: PASS (all settings API tests).

- [ ] **Step 5: Commit**

```bash
git add src/hippo/api.py tests/test_api_settings.py
git commit -m "feat: /sources/{id}/resync + /settings/status + SPA RESERVED prefixes"
```

---

### Task 5: Frontend — Settings view (gear toggle + tabs)

No JS test harness exists; the gate is `npm run build`. Keep logic minimal and pull the only branching rule (role → tabs) into a tiny pure function for clarity.

**Files:**
- Create: `ui/src/Settings.tsx`
- Modify: `ui/src/App.tsx` (gear button + `view` state), `ui/vite.config.ts` (proxy), `ui/src/app.css` (a few classes)

- [ ] **Step 1: Vite proxy** — add the new paths so dev hits the API:

```ts
    proxy: {
      "/chat": "http://127.0.0.1:8000",
      "/ingest": "http://127.0.0.1:8000",
      "/documents": "http://127.0.0.1:8000",
      "/sources": "http://127.0.0.1:8000",
      "/users": "http://127.0.0.1:8000",
      "/tokens": "http://127.0.0.1:8000",
      "/settings": "http://127.0.0.1:8000",
    },
```

- [ ] **Step 2: Create `ui/src/Settings.tsx`**

A self-contained component. `role` decides tabs; everyone gets Tokens. Uses `fetch`. (Style with existing/added CSS; logic shown is complete.)

```tsx
import { useCallback, useEffect, useState } from "react";

type Role = "developer" | "manager" | "admin";

export function tabsForRole(role: string): string[] {
  return role === "admin"
    ? ["Sources", "Users", "Tokens", "Status"]
    : ["Tokens"];
}

async function getJSON(url: string) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${r.status}`);
  return r.json();
}

export default function Settings({ role, onClose }: { role: Role; onClose: () => void }) {
  const tabs = tabsForRole(role);
  const [tab, setTab] = useState(tabs[0]);
  return (
    <div className="settings">
      <div className="settings-head">
        <h2>Settings</h2>
        <button onClick={onClose}>← Back to chat</button>
      </div>
      <nav className="settings-tabs">
        {tabs.map((t) => (
          <button key={t} className={t === tab ? "active" : ""} onClick={() => setTab(t)}>{t}</button>
        ))}
      </nav>
      {tab === "Tokens" && <TokensPanel admin={role === "admin"} />}
      {tab === "Sources" && <SourcesPanel />}
      {tab === "Users" && <UsersPanel />}
      {tab === "Status" && <StatusPanel />}
    </div>
  );
}

function TokensPanel({ admin }: { admin: boolean }) {
  const [rows, setRows] = useState<any[]>([]);
  const [name, setName] = useState("");
  const [secret, setSecret] = useState<string | null>(null);
  const [showAll, setShowAll] = useState(false);
  const load = useCallback(() => {
    getJSON(showAll ? "/tokens?all=true" : "/tokens").then(setRows).catch(() => setRows([]));
  }, [showAll]);
  useEffect(() => { load(); }, [load]);
  const create = async () => {
    const r = await fetch("/tokens", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }) });
    if (r.ok) { const b = await r.json(); setSecret(b.token); setName(""); load(); }
  };
  const revoke = async (id: number) => { await fetch(`/tokens/${id}`, { method: "DELETE" }); load(); };
  return (
    <div className="panel">
      <p>Personal access tokens for MCP / Slack / CLI. Each token carries your own role.</p>
      <div className="row">
        <input placeholder="name (e.g. laptop)" value={name} onChange={(e) => setName(e.target.value)} />
        <button onClick={create}>Create token</button>
        {admin && <label><input type="checkbox" checked={showAll}
          onChange={(e) => setShowAll(e.target.checked)} /> show all users</label>}
      </div>
      {secret && (
        <div className="secret">
          <strong>Copy now — you won't see it again:</strong>
          <code>{secret}</code>
          <button onClick={() => navigator.clipboard?.writeText(secret)}>Copy</button>
          <button onClick={() => setSecret(null)}>Done</button>
        </div>
      )}
      <table><tbody>
        {rows.map((t) => (
          <tr key={t.id}>
            {t.email && <td>{t.email}</td>}
            <td>{t.name || "(unnamed)"}</td>
            <td>{t.last_used_at ? `used ${t.last_used_at}` : "never used"}</td>
            <td><button onClick={() => revoke(t.id)}>Revoke</button></td>
          </tr>
        ))}
      </tbody></table>
    </div>
  );
}

function SourcesPanel() {
  const [rows, setRows] = useState<any[]>([]);
  const [loc, setLoc] = useState("");
  const [access, setAccess] = useState("everyone");
  const [note, setNote] = useState("");
  const load = useCallback(() => { getJSON("/sources").then(setRows).catch(() => setRows([])); }, []);
  useEffect(() => { load(); }, [load]);
  const add = async () => {
    const r = await fetch("/sources", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind: "folder", location: loc, access }) });
    setNote(r.ok ? "added" : `error ${r.status}`); if (r.ok) { setLoc(""); load(); }
  };
  const resync = async (id: number) => { setNote("syncing…");
    const r = await fetch(`/sources/${id}/resync`, { method: "POST" }); setNote(r.ok ? "synced" : `error ${r.status}`); };
  const del = async (id: number) => { await fetch(`/sources/${id}`, { method: "DELETE" }); load(); };
  return (
    <div className="panel">
      <div className="row">
        <input placeholder="/path/to/docs (within HIPPO_SOURCE_ROOTS)" value={loc}
          onChange={(e) => setLoc(e.target.value)} />
        <select value={access} onChange={(e) => setAccess(e.target.value)}>
          <option value="everyone">everyone</option><option value="managers">managers</option>
        </select>
        <button onClick={add}>Add source</button><span className="note">{note}</span>
      </div>
      <table><tbody>
        {rows.map((s) => (
          <tr key={s.id}><td>{s.location}</td><td>{s.access}</td>
            <td><button onClick={() => resync(s.id)}>Re-sync</button>
                <button onClick={() => del(s.id)}>Delete</button></td></tr>
        ))}
      </tbody></table>
    </div>
  );
}

function UsersPanel() {
  const [rows, setRows] = useState<any[]>([]);
  const load = useCallback(() => { getJSON("/users").then(setRows).catch(() => setRows([])); }, []);
  useEffect(() => { load(); }, [load]);
  const setRole = async (email: string, role: string) => {
    await fetch(`/users/${encodeURIComponent(email)}/role`, { method: "PUT",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ role }) });
    load();
  };
  return (
    <div className="panel">
      <table><tbody>
        {rows.map((u) => (
          <tr key={u.email}><td>{u.email}</td>
            <td><select value={u.role} onChange={(e) => setRole(u.email, e.target.value)}>
              {["developer", "manager", "admin"].map((r) => <option key={r} value={r}>{r}</option>)}
            </select></td></tr>
        ))}
      </tbody></table>
    </div>
  );
}

function StatusPanel() {
  const [s, setS] = useState<any>(null);
  useEffect(() => { getJSON("/settings/status").then(setS).catch(() => setS(null)); }, []);
  if (!s) return <div className="panel">Loading…</div>;
  return (
    <div className="panel status">
      <dl>
        <dt>Auth mode</dt><dd>{s.auth_mode}</dd>
        <dt>Chat model</dt><dd>{s.chat_model}</dd>
        <dt>Embedding model</dt><dd>{s.embedding_model}</dd>
        <dt>Repos</dt><dd>team: {String(s.repos.team)} · managers: {String(s.repos.managers)}</dd>
        <dt>MCP</dt><dd>{String(s.mcp_enabled)}</dd>
        <dt>Slack</dt><dd>{String(s.slack_enabled)}</dd>
        <dt>Counts</dt><dd>{s.counts.documents} docs · {s.counts.sources} sources · {s.counts.users} users</dd>
      </dl>
    </div>
  );
}
```

- [ ] **Step 3: Wire the gear toggle into `App.tsx`**

Add `import Settings from "./Settings";`. Add `const [view, setView] = useState<"chat" | "settings">("chat");`. In the header `.upload` block (after the whoami span), add a gear button when signed in:

```tsx
{me && <button className="gear" title="Settings" onClick={() => setView("settings")}>⚙</button>}
```

Wrap the existing `<main>…</main>` so that when `view === "settings"` and `me` is set, it renders `<Settings role={me.role as any} onClose={() => setView("chat")} />` instead of the chat main. (Guard: if `view==="settings"` but `!me`, fall back to chat.)

- [ ] **Step 4: Minimal CSS** — add classes used above (`.settings`, `.settings-tabs`, `.panel`, `.secret`, `.gear`, `.status dl`) to `ui/src/app.css`, matching the existing visual style (reuse colors/spacing variables already there).

- [ ] **Step 5: Build gate**

Run: `cd ui && npm run build`
Expected: clean TypeScript build, no errors.

- [ ] **Step 6: Commit**

```bash
git add ui/src/Settings.tsx ui/src/App.tsx ui/vite.config.ts ui/src/app.css
git commit -m "feat: Settings UI view (gear toggle, role-gated tabs)"
```

---

### Task 6: Docs

**Files:** `README.md`, `CLAUDE.md`, `docs/superpowers/plans/2026-06-12-roadmap.md`

- [ ] **Step 1: README** — add a "Settings" subsection under the UI/usage area: admins manage sources, users/roles, and see status; every user self-serves personal access tokens from the gear menu (token shown once). Note the new endpoints exist.
- [ ] **Step 2: CLAUDE.md** — architecture: note the new endpoints in the `api.py` line (`/users`, `/tokens`, `/sources/{id}/resync`, `/settings/status`); add `Settings.tsx` to the `ui/` description; note `Storage.list_all_tokens`/`revoke_token_any`; bump the test count; add a State line "Roadmap item 9 (Settings UI) implemented on branch `build/settings-ui`."
- [ ] **Step 3: roadmap** — mark item 9 **built**, reference `../specs/2026-06-13-settings-ui-design.md`.
- [ ] **Step 4: Commit**

```bash
git add README.md CLAUDE.md docs/superpowers/plans/2026-06-12-roadmap.md
git commit -m "docs: Settings UI (README, CLAUDE.md, roadmap item 9)"
```

---

### Task 7: Final gate

- [ ] **Step 1:** `uv run pytest` — all pass, zero-network (202 existing + new settings/storage tests).
- [ ] **Step 2:** `cd ui && npm run build` — clean.
- [ ] **Step 3:** `uv run python -c "import hippo.api, hippo.storage; print('ok')"` — ok.
- [ ] **Step 4 (if Ollama up):** `uv run hippo eval eval/golden.yaml` — recall unchanged (settings don't touch retrieval).

## Self-review notes
- **Spec coverage:** §3 endpoints → Tasks 1-4; §4 frontend → Task 5; §2 access (require_admin / self-scope) → Tasks 2-4 + tests; §5 tests → each API task; storage methods → Task 1.
- **No SQL outside storage.py:** the two new methods live in `storage.py`; api.py only calls Storage.
- **Fail-closed:** admin routes use `require_admin`; token routes scope to `user.email`; `?all=true` re-checks admin; anti-lockout guard on self-demotion.
- **Secret hygiene:** `POST /tokens` returns the secret once; all list endpoints return metadata only; `/settings/status` returns bools, never secrets — asserted in tests.
- **Type consistency:** `RoleIn`/`TokenIn` models; tuple unpacking matches the new `list_all_tokens` shape `(id,email,name,created_at,last_used_at)` and existing `list_tokens` `(id,name,created_at,last_used_at)`.
