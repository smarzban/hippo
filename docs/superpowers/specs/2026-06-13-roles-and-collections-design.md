# SP1 — Roles & content-folder model (design)

> First of three sub-projects turning Hippo into a self-hosted, UI-administered product.
> SP2 = built-in password auth; SP3 = first-run setup wizard. This spec is the foundation
> both build on. Brainstormed 2026-06-13. See `memory: hippo-productization-roadmap`.

## 1. Goal

Replace Hippo's flat, source-level access model with a **role-tiered folder tree** that admins
manage entirely from the UI:

- Three **hierarchical roles** — `user` < `admin` < `owner`.
- A **folder tree** rooted at three top folders (default names `Default`, `Private`, `Owner`)
  whose tier is `user` / `admin` / `owner` respectively. Folders nest arbitrarily; documents
  live in folders.
- A folder's tier is inherited from its top-level ancestor and gates **both read (retrieval)
  and write (upload)**: you may access a folder iff `your_role_rank >= folder_tier_rank`.
- Admins/owners create and organise folders in the UI (no server `mkdir`); a **role-scoped
  upload modal** lets anyone add docs into the folders their role may write to.

This also closes the upload-access gap (today uploads have no source → default everyone).

## 2. Scope

**In scope:** the role redesign; the folder-tree data model + fresh schema (the three seeded
roots); folder CRUD API + UI; the upload modal (multi-destination, progress); role-filtered
retrieval by folder tier; mounting existing synced folders / GitHub repos as **pull-only**
nodes under a chosen tier.

**No data migration.** All current data is disposable dummy/dev data, so SP1 does **not**
preserve or back-fill existing rows. `db.py` creates the new schema for fresh databases; an
existing dev DB is simply recreated (`rm hippo.db && uv run hippo sync <folder>`). See §8.

**Explicit non-goals (deferred):**
- **No per-subfolder tier overrides** — tier is fixed at the three roots and strictly inherited.
- **No bidirectional GitHub sync** — synced/repo folders are pull-only and lock out manual
  upload ("content comes from the origin only"). The toggle to *mix* manual uploads into a
  synced folder, merge pre-existing content up into the repo, and push later uploads back is a
  separate follow-up ("GitHub two-way sync").
- **Auth is unchanged** (still `none`/`oidc`/`iap`) — password auth is SP2.
- **Root-folder names are seeded defaults** here; making them user-chosen at first run is SP3.
  (Renaming a root via the normal folder-rename is allowed; the tier mapping is fixed.)

## 3. Roles

Rename the three roles and treat them as a **rank**:

| role  | rank | replaces      |
|-------|------|---------------|
| user  | 0    | developer     |
| admin | 1    | manager       |
| owner | 2    | admin         |

Capability matrix:

| capability | user | admin | owner |
|---|:--:|:--:|:--:|
| Read + upload into folders at/below their tier | ✓ (user) | ✓ (user+admin) | ✓ (all) |
| Create / rename / move / delete folders | — | ✓ | ✓ |
| Manage users & assign roles | — | ✓ (up to `admin`) | ✓ (incl. `owner`) |
| First-run setup, change auth mode / models | — | — | ✓ (SP3) |

`HIPPO_ADMIN_EMAILS` bootstrap accounts become **owner** (the old top tier). Default role for a
new user is `user`. The "manager-or-above" notion (`MANAGER_ROLES`) becomes a rank comparison
`rank >= 1` (admin+); "admin-only" API guards become `rank >= 1` for content/user management and
`rank == 2` for owner-only actions. Two FastAPI dependencies: `require_admin` (rank ≥ 1) and
`require_owner` (rank == 2).

## 4. Data model

Evolve the existing `sources` table into a **`folders`** tree (all SQL stays in `storage.py`;
the table rename is contained there). The adjacency-list representation is chosen over a closure
table / materialized path because the tree is shallow and, crucially, we **denormalize the tier
onto every node** so access filtering needs no ancestor walk.

```
folders
  id         INTEGER PK
  parent_id  INTEGER NULL REFERENCES folders(id) ON DELETE CASCADE   -- NULL = a root
  name       TEXT NOT NULL                                            -- unique among siblings
  min_role   TEXT NOT NULL  ('user'|'admin'|'owner')                  -- == the root's tier; inherited
  origin     TEXT NOT NULL  ('manual'|'folder'|'repo')                -- was sources.kind
  location   TEXT NULL                                                -- fs path / owner/repo; NULL for manual
  UNIQUE(parent_id, name)
```

```
documents
  ...
  folder_id  INTEGER NOT NULL REFERENCES folders(id) ON DELETE CASCADE  -- was source_id (nullable)
  path       TEXT UNIQUE          -- now the tree path, e.g. "Default/Retail/onboarding.md"
```

- The three roots are `folders` rows with `parent_id IS NULL`, `origin='manual'` (they hold
  uploads and subfolders), and `min_role` = user/admin/owner.
- `min_role` is set from the root at create/move time and is identical down a subtree (no
  overrides in SP1) — so a child created under a root, or moved, takes that root's tier; moving
  a subtree across roots rewrites `min_role` for the whole subtree.
- `documents.path` stays the document's **unique citation key** (the agent cites `[path >
  section]`, and `documents.path` is UNIQUE). Uploads are stored **folder-qualified** (e.g.
  `Default/Retail/onboarding.md`) so the same filename in different folders stays unique and the
  citation reads meaningfully; **migrated synced docs keep their existing source-relative paths**
  so citations and the eval fixtures stay stable. `folder_id` drives organization + access;
  `path` drives citations. The same file uploaded to N folders becomes N independent document
  rows, each carrying its folder's tier.

**Users & tokens — surrogate key (identity).** While the schema is being reset, adopt a stable
surrogate primary key for users (Hippo is becoming a real product): `users(id INTEGER PK,
email TEXT UNIQUE NOT NULL, role, …)`. `email` becomes a **mutable unique attribute** (the login
identifier), not the primary key — so changing a user's email touches one column instead of
cascading through every reference. `tokens` (and any future user-linked rows) reference
**`user_id`**, not `email`. This is free here (schema reset, no data) and is the base SP2 extends
(it adds a nullable `password_hash`) and SP3's user management builds on. `resolve_role` still
takes an email (from the IdP / login) and resolves it to the user row.
- `origin='manual'` folders accept uploads; `origin in ('folder','repo')` are populated by sync
  and are upload-locked (§2 non-goal).

## 5. Access enforcement (security-critical)

- Retrieval methods (`search_hybrid`, `grep`, `list_documents`, `get_document`) keep the
  **keyword-only `role` with no default** invariant (a forgotten call site must `TypeError`,
  never widen access). They filter `WHERE f.min_role_rank <= :caller_rank` via the
  `documents → folders` join (ranks: user 0, admin 1, owner 2). Hierarchical inheritance falls
  out of `caller_rank >= folder_rank`.
- Write (upload / folder-create-under) requires `caller_rank >= target_folder_rank` **and**, for
  upload, `target.origin == 'manual'`. Folder CRUD additionally requires `rank >= 1`.
- All SQL stays in `storage.py`; the API/agent call the Storage interface (Postgres exit ramp).

## 6. API surface

Replaces the `/sources` endpoints with `/folders` (admin-managed) and reworks `/ingest`:

- `GET /folders` — the tree the caller may **read** (role-filtered); each node returns id,
  parent_id, name, tier, origin, doc count, and `writable` (caller_rank ≥ tier ∧ manual).
- `POST /folders` — create a manual folder (`require_admin`); body `{parent_id, name}`; inherits
  parent tier; caller must be able to write the parent. Or mount a synced source/repo:
  `{parent_id, name, origin: 'folder'|'repo', location}` → inherits parent tier, then sync runs.
- `PATCH /folders/{id}` — rename / move (`require_admin`); a move across roots rewrites the
  subtree's `min_role`.
- `DELETE /folders/{id}` — delete folder + its docs (`require_admin`); roots cannot be deleted.
- `POST /folders/{id}/resync` — admin re-sync for synced/repo origins (keeps the existing
  not-a-directory guard so a missing mount can't wipe the folder).
- `POST /ingest` — now takes **`folder_ids` (one or more)** instead of the `repo` field; each
  must be writable by the caller and `manual`; the upload is ingested into each (N doc rows).
  Size guards (`HIPPO_MAX_UPLOAD_BYTES` / `MAX_DOC_CHARS`) unchanged.
- `GET /documents`, `GET /documents/{id}`, `POST /chat`, `/mcp`, Slack — unchanged surface; they
  already pass the caller's role and now get folder-tier filtering for free.

## 7. UI

- **Settings → "Folders"** (renamed from "Sources"; admin+ only): a **tree view** of the three
  roots and their subfolders, showing tier and origin. Admins can create manual subfolders,
  mount a synced folder / GitHub repo under a chosen root (→ tier), rename, move, delete, and
  re-sync. Synced nodes render read-only with a "synced from <origin>" badge.
- **Upload modal** (replaces the bare "Add doc" button, available to all roles): choose file(s);
  **multi-select destination folders** from a picker showing only the caller's writable manual
  folders (a `user` sees Default-tier manual folders, an `admin` sees Default+Private, etc.); a
  **progress indicator** per file; on completion the primary button switches from **Upload** to
  **Done**. Surfaces per-file failures (size/parse) inline.
- Pure, testable helpers (writable-folder filtering, tree flattening, upload-state reducer) live
  apart from the React components so Vitest can cover them.

## 8. Schema & fresh start (no data migration)

Current data is disposable, so there is **no in-place data migration**. Instead:

1. **New schema in `db.py`:** `connect()` creates the `folders` table and the new
   `documents.folder_id` column directly (replacing `sources` / `source_id`). On first creation
   it **seeds the three roots** — `Default` (user), `Private` (admin), `Owner` (owner),
   `origin='manual'`.
2. **Roles:** new users default to `user`; `HIPPO_ADMIN_EMAILS` resolve to `owner`. No remap of
   existing rows is needed (none worth keeping).
3. **Legacy DB → clear error, not silent breakage.** If `connect()` opens a DB carrying the old
   `sources`/`source_id` schema, it raises a clear "incompatible schema — recreate the database
   (`rm hippo.db`)" error rather than half-migrating. (Acceptable because data is disposable;
   real migrations become relevant only once there's data worth keeping — a later concern.)
4. **Paths:** uploads are folder-qualified (`Default/Retail/onboarding.md`); the eval fixtures,
   re-synced into a chosen root on a fresh DB, keep their source-relative paths, so the eval
   harness and citations are unaffected.

## 9. Testing

Zero-network throughout (`FakeEmbedder`, `TestModel`/`FunctionModel`):

- **Role rank filtering:** user cannot retrieve admin/owner-tier docs; admin sees user+admin not
  owner; owner sees all — via `search_hybrid`/`list_documents`/`get_document`/`grep`.
- **Write gating:** upload into a writable manual folder succeeds; into a higher-tier or a
  synced folder → 403; folder CRUD requires admin+.
- **Tree ops:** nesting, sibling-name uniqueness, move-across-roots rewrites subtree tier,
  delete cascades to docs, roots undeletable.
- **Schema / fresh start:** a fresh DB seeds exactly the three roots with the right tiers and
  `origin='manual'`; new users default to `user`; `HIPPO_ADMIN_EMAILS` resolve to `owner`;
  opening a legacy `sources`-schema DB raises the clear recreate error.
- **Vitest:** writable-folder filtering, tree flattening, upload-state reducer.
- `hippo eval eval/golden.yaml` still passes (fixtures migrate into Default).

## 10. Open items folded into later SPs

- Configurable root-folder names at first run → **SP3** (wizard); seeded defaults here.
- Bidirectional GitHub sync (mix manual + push) → dedicated follow-up.
- Password auth / login → **SP2**.
