# Storage layer

`storage/` is where **all** of Hippo's SQL lives. Nothing else in the codebase
issues SQL — the agent, API, ingest, MCP, and Slack surfaces all call the
`Storage` interface. This is deliberate: it's the **Postgres exit ramp**.
Reimplement `Storage` against another backend and the rest of the app is
unchanged.

## Package shape

`storage.py` was a single ~770-line class; it's now a package decomposed by
persistence domain behind a thin facade (the `Storage` god-class was the
refactor target — see `CLAUDE.md` and the audit notes):

| File | Contents |
|---|---|
| `_facade.py` | `class Storage(...)` — owns `__init__` (the connection, the embedder, the one `_lock`); composes the mixins. |
| `documents.py` | `_DocumentsMixin` — upsert/delete/get/list docs, dedup checks, the embedding-model stamp, `_delete_chunks`. |
| `folders.py` | `_FoldersMixin` — the folder-tree CRUD, subtree walks, cascade delete. |
| `users.py` | `_UsersMixin` — users, roles, profile, password hashes, lockout state; the `LOCKOUT_*` constants. |
| `tokens.py` | `_TokensMixin` — personal access tokens (sha256-stored). |
| `config_store.py` | `_ConfigMixin` — the `config` key/value table, setup claim, counts; `SETUP_COMPLETE_KEY`. |
| `search.py` | `_SearchMixin` — hybrid search, grep, reindex, backup; the `RRF_K`/`GREP_*`/`VEC_OVERFETCH` constants. |
| `_common.py` | Shared dataclasses (`Document`, `DocumentMeta`, `SearchHit`, `Folder`), `_role_filter`, `_norm_email`, the logger. |
| `__init__.py` | Re-exports `Storage` + the dataclasses. **Public surface unchanged:** `from .storage import Storage`. |

The mixins all operate on `self.con` / `self._lock` / `self.embedder`, so
cross-domain calls resolve via the facade's MRO — e.g. `delete_folder`
(folders) reuses `_delete_chunks` (documents); `create_token_returning_id`
(tokens) reuses `_user_id_for` (users); `is_setup_complete` (config) calls
`get_config`. Class constants (`Storage.LOCKOUT_MINUTES`, `Storage.RRF_K`, …)
remain reachable on the facade through inheritance.

## The one-connection, one-lock model

```python
class Storage:
    def __init__(self, con, embedder):
        self.con = con
        self.embedder = embedder
        self._lock = threading.Lock()
```

A single SQLite connection is shared between the event loop and
`run_in_threadpool` workers (agent tools + ingest). SQLite's per-statement mutex
does **not** prevent interleaved statement-stepping across threads (that raises
`InterfaceError`), so **every DB critical section is serialized through
`self._lock`**. Network embedding is always done *outside* the lock so a slow
ingest can't block concurrent reads.

> **Invariant:** one `Storage` per connection. Two `Storage` instances on one
> connection would each have their own lock and defeat the serialization. The
> facade is constructed once in `build_context`.

This is the main scaling boundary: for a larger deployment, swap to
per-worker/pooled connections (or Postgres) behind the same interface.

## Schema (`db.py`)

`connect()` opens the database with WAL, loads the `sqlite-vec` extension, and
creates the schema if absent:

- **`folders`** — adjacency tree (`parent_id`), `min_role`, `origin`
  (`manual`|`folder`), `location`. Seeded with three roots on first open:
  `Default` (user), `Private` (admin), `Owner` (owner). An index on
  `documents.folder_id` speeds the tier joins.
- **`documents`** — `folder_id` FK, path, title, content, `content_hash`,
  summary.
- **`chunks`** — `document_id`, position, `heading_path`, text — mirrored into
  **`chunks_fts`** (FTS5) by sync triggers.
- **`chunk_vec`** — a `sqlite-vec` `vec0` virtual table; its dimension is fixed
  at creation.
- **`users`** (surrogate `id` PK) + **`tokens`** (`user_id` FK).
- **`config`** (key, value) and **`meta`** (the embedding stamp, etc.).

### Legacy-DB guard

A pre-folders database (one with `documents.source_id` and no `folders` table)
raises `RuntimeError("recreate the database")` on `connect()`. There is no
migration path — fail loud rather than silently misbehave.

## Role filtering lives here

`_role_filter(role)` returns an SQL fragment + params restricting documents to
folders the role may read, using `readable_min_roles()` from `roles.py` (the
single rank definition). The retrieval methods (`search_hybrid`, `grep`,
`list_documents`, `get_document`, `list_document_meta`) take `role` **keyword-only
with no default**, so a forgotten call site is a `TypeError`, never a silent
access-control leak. See [Auth & RBAC](auth-and-rbac.md).

## Notable methods

- `list_document_meta` — a **content-free** projection (`id/path/title/summary`)
  for browse/list surfaces, so listing the corpus doesn't read every document's
  full Markdown into memory.
- `create_user` — atomic insert-only (`ON CONFLICT DO NOTHING` + rowcount inside
  the lock), so concurrent creates can't both win (callers map `False` → 409).
- `claim_setup` — atomic first-run claim, so racing `/setup` requests can't both
  create an owner.
- `record_failed_login` / `is_locked` / `clear_lock_if_expired` — DB-clock-based
  lockout (testable; counter decays after the window).
- `token_owner` — lets the API tier-check a cross-user token revoke against the
  owner's role.

## Hard rules

- **No SQL outside this package.** Don't erode the exit ramp.
- **One `Storage` per connection / one lock.**
- **`password_hash` is never returned** by any method that feeds an API response
  beyond the login path's internal use.
