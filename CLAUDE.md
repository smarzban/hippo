# Hippo — agentic org knowledge base

Feed it markdown/text/Google-Docs-exports; ask it questions in chat; it answers **only**
from the indexed docs, with `[path > section]` citations. Org-level and role-governed
(three-tier RBAC user/admin/owner, four auth modes, folder-scoped access, a browser
setup wizard, MCP + Slack surfaces) — and still runs fine self-hosted for a single user.

## Read these before changing anything significant

- `docs/superpowers/specs/2026-06-11-knowledge-hub-design.md` — the original v1 spec (what & how). **Historical:** several listed "non-goals" (.docx parsing, Slack bot, multi-user auth) have since shipped, and the "personal-first" posture is now org-level — see State below.
- `docs/superpowers/specs/2026-06-11-knowledge-hub-decisions.md` — decision log (why; 9 ADRs incl. all rejected alternatives). Historical, same caveat as the design spec.
- `docs/superpowers/plans/2026-06-11-knowledge-hub.md` — the 15-task build plan (code has a few post-plan hardening fixes the plan text doesn't show)
- `docs/superpowers/plans/2026-06-12-roadmap.md` — roadmap / action list (post-v1 hardening + features)
- `docs/superpowers/plans/2026-06-13-sp1-roles-and-folders.md` — SP1 plan (roles & folder tree; merged PR #11)
- `docs/superpowers/plans/2026-06-13-sp2-password-auth.md` — SP2 plan (password auth; merged PR #12)
- `docs/superpowers/plans/2026-06-13-sp3-setup-wizard.md` — SP3 plan (setup wizard & config store; merged PR #13)

Naming: the project was "knowledgeHub" during design (docs keep that name); the product/package is **hippo** (from hippocampus).

## Architecture (src/hippo/)

```
roles.py       ROLE_RANK / VALID_ROLES / DEFAULT_ROLE ("user"). Pure helpers: rank(), can_read(), can_write(), readable_min_roles().
               Single source of truth for the rank comparison — no imports from the rest of hippo. Everything else imports from here.
agent.py       build_agent(model) -> Pydantic AI agent, deps=HubDeps(store, role). 4 tools: search/read_document/list_documents/grep.
               Tool output is framed as ⟦untrusted document data⟧…⟦end⟧ (prompt-injection boundary).
               System prompt enforces cite-everything + never-improvise + untrusted-content rule. defer_model_check=True (don't remove: construction must not need API keys)
api.py         build_app(settings, model_override=None): /chat streams Vercel AI protocol via VercelAIAdapter.dispatch_request
               (deps + usage_limits kwargs work on pydantic-ai 1.107). verify_request real: modes none|oidc|iap|password + bearer tokens every mode.
               require_admin (rank>=1) guards folder/user mutations; require_owner (rank>=2) guards owner-only ops.
               GET /me ({email,role,auth_mode,name}); PATCH /me (self-edit display name only — email is read-only login identity). /auth/login,/auth/callback,/auth/logout (oidc).
               password mode: SessionMiddleware (requires HIPPO_SECRET_KEY; build_app raises ValueError if unset); session keyed by user_id (surrogate).
               POST /auth/login (email+password, argon2id verify, lockout check, generic 401 — no enumeration), POST /auth/logout (clears session).
               GET /auth/config (public, returns {auth_mode}; no secrets). POST /me/password (self-service change; requires current password, 8-char minimum).
               POST /users/{email}/password (admin reset; returns new secret once; gated by tier so admins cannot reset higher-tier users).
               /ingest: size-checked against HIPPO_MAX_UPLOAD_BYTES (413) and HIPPO_MAX_DOC_CHARS (skipped); takes folder_ids (repeated form field): ingests into each destination folder; write-gated by can_write(caller_role, folder.min_role, folder.origin).
               /folders: GET (role-filtered list with writable flag), POST (admin+), PATCH (rename/move, admin+), DELETE (admin+), POST /{id}/resync (admin+, folder-origin only). Allowlist via HIPPO_SOURCE_ROOTS.
               /users (admin): GET list, POST (create-user — effective-role tier guard so an admin can't mint a higher-tier login; returns a one-time password in password mode; 409 on duplicate), PUT /{email}/role with anti-lockout guard.
               /tokens: GET/POST (self-service, secret returned once), DELETE /{id} (self or admin); GET ?all=true (admin).
               /settings/status (admin): effective auth_mode/chat_model/embedding_model (cfg.get — DB overlay wins), setup_complete flag, repo bools, counts — no secrets.
               GET /setup/status (public): {setup_complete, auth_modes_available}. POST /setup (token-gated, once): wizard endpoint — sets owner, renames roots, persists operational config, marks setup complete; 409 if already complete; 403 on wrong token; validates secret env vars are present for the chosen mode.
               GET /config (owner): effective operational config for all DB_OVERRIDABLE keys (DB value else env default); never returns a secret. PUT /config (owner): upsert DB_OVERRIDABLE keys; rejects unknown/secret keys (400); embedding_model/dim locked once documents exist (409); auth_mode switch anti-lockout guard (_validate_auth_switch).
               Serves ui/dist as static files when HIPPO_UI_DIST is set (single origin, :8000).
               Mounts FastMCP server at /mcp (HIPPO_MCP_ENABLED, default true) with _McpBearerAuth middleware (bearer token → role via _mcp_role contextvar).
               cfg = Config(settings, store) built in build_app; operational keys resolved at construction (auth_mode, oidc/iap/domain wiring); chat_model read LIVE per /chat via _live_agent().
               SessionMiddleware mounted once if settings.secret_key (covers password + oidc + wizard auto-login).
mcp_server.py  FastMCP server exposing search/read_document/list_documents/grep; mounted at /mcp in api.py with bearer-token auth + role filtering via the _mcp_role contextvar; `hippo mcp` runs it over stdio (as owner, no token).
auth.py        AuthenticatedUser(email, role), AuthError, check_domain, resolve_role (shared identity→role: normalize+domain-gate+ensure_user+owner-bootstrap for HIPPO_ADMIN_EMAILS, used by api.py and slack_bot.py),
               IapVerifier (ES256 IAP assertions, injectable key_fetcher),
               validate_google_id_token (claims-only, code-flow tokens). Mode wiring lives in api.py: none|oidc|iap|password + bearer tokens any mode.
               hash_password(pw)->str, verify_password(hashed, pw)->bool (argon2id; catches Argon2Error so callers get a clean bool),
               set_password_hasher(hasher) (test hook: swap for reduced-cost profile). Never log or return a hash.
               Role FILTERING lives in storage.py, not here.
chunking.py    chunk_markdown(): heading-aware, atomic code fences, char-based ~750-token chunks; overlap tail is re-checked against max_chars before prepending
cli.py         Typer: sync [--watch] / add / search / reindex / serve / mcp / slack / eval / backup / role set/list / token create/list/revoke / user set-password
slack_bot.py   Slack Q&A bot: Socket Mode via slack-bolt. Pure helpers
               (surface_role/build_history/format_answer/answer_question) + handle_event
               adapter (tested with a fake client) + build_slack_app wiring. Split-by-surface
               access: DM=asker's role, channel @mention=user-tier only. `hippo slack` runs it.
config.py      Settings, env prefix HIPPO_ (pydantic-settings). auth_mode: Literal["none","oidc","iap","password"].
               setup_token: str (env-only, never stored in DB; if empty a random token is logged at startup).
               DB_OVERRIDABLE: frozenset of operational keys the config table may override (auth_mode, chat_model,
               enrich_model, embedding_model, embedding_dim, allowed_domain, oidc_client_id, public_url, iap_audience).
               Config(settings, store).get(key) → DB value if key in DB_OVERRIDABLE and set, else settings.key; secrets
               never read from DB. _coerce() converts embedding_dim to int.
db.py          connect() -> sqlite3: folder-tree schema (folders adjacency table, documents.folder_id, surrogate users(id PK) + tokens(user_id FK), config(key,value)), WAL, sqlite-vec, FTS5 + sync triggers.
               Seeds three root folders on first open (Default/user, Private/admin, Owner/owner). Legacy-DB guard: raises RuntimeError("recreate the database") if documents.source_id found.
embeddings.py  Embedder protocol; OpenAIEmbedder (default text-embedding-3-small); FakeEmbedder (deterministic, tests/offline)
enrich.py      Enricher: doc summary + contextual line per chunk (cheap model; embedding input = context+"\n"+chunk; stored chunk text stays raw)
github.py      GitHubContentsClient.put_file: upload-to-repo via Contents API (1 call/file)
ingest.py      Ingestor: parse->hash dedupe->chunk->enrich->embed+index (1 txn/doc, per-file isolation);
               sync_folder(folder, store, parent_id, ...) mounts a pull-only ('folder' origin) node under parent_id, handles deletions + IGNORED_EXTENSIONS/DIRS noise filter
parsers.py     .md/.txt/.html/.docx -> (title, canonical markdown). SUPPORTED set is the gate.
               .docx via mammoth (docx -> HTML -> markdown, heading styles preserved).
               parse_bytes(filename, data) is the canonical bytes entry point (used by /ingest).
storage.py     Storage(con, embedder): ALL SQL lives here. upsert/delete/get/list docs (takes folder_id, not source_id),
               search_hybrid (FTS5 BM25 + vec KNN merged via RRF, k=60), grep (raises ValueError on bad regex/timeout/pattern-too-long).
               backup(path) via VACUUM INTO for consistent snapshots.
               Folder CRUD: get_folder, list_folders(role), create_folder(parent_id, name, origin, location), rename_folder, move_folder (rewrites whole subtree tier), delete_folder (cascades), folder_path (slash-joined ancestor path), folder_by_location.
               Users/roles (ensure_user, set_role, list_users); profile/create (get_profile(email)->dict|None, set_name(email, name), create_user(email, *, role, password_hash=None)->bool — atomic insert-only, False if the email already exists); surrogate-keyed tokens (create_token, resolve_token,
               list_tokens(email), revoke_token(id,email), list_all_tokens() admin view, revoke_token_any(id) admin revoke).
               Local credentials + lockout: set_password(email, hash, *, role), get_credentials(email)->dict|None (user_id/email/role/password_hash/failed_logins/locked_until),
               get_user_by_id(id)->(email,role)|None, record_failed_login(email) (increments counter; sets locked_until after LOCKOUT_MAX_FAILURES=5),
               reset_login_state(email) (clears on successful login), is_locked(email)->bool (DB-clock comparison).
               LOCKOUT_MAX_FAILURES=5, LOCKOUT_MINUTES=15 (class constants; hardcoded defaults). password_hash is never returned by any API endpoint.
               Config store (SP3): get_config(key)->str|None, set_config(key, value) (upsert), all_config()->dict,
               is_setup_complete()->bool (setup_complete key == "1"), mark_setup_complete(), document_count()->int.
               Role-filtered retrieval: search_hybrid/grep/list_documents/get_document take keyword-only `role` with NO default.
               _role_filter(role) -> SQL fragment + params using readable_min_roles() from roles.py (the single definition of rank logic).
```

`ui/` — Vite + React 19 + `@ai-sdk/react` v2 `useChat` + `DefaultChatTransport({api:"/chat"})`.
Vite dev-server proxies /chat,/ingest,/documents,/folders,/users,/tokens,/settings,/config,/setup,/me,/auth to :8000. Tool parts render as progress lines.
`Settings.tsx` — gear-toggle Settings view; role-gated tabs via tabsForRole(role): user → My Profile only; admin → Folders/Users/My Profile/Status; owner adds System config. Tokens + self-service password change live inside the My Profile tab. Folders tab shows the full tree with Rename/Re-sync/Delete actions.
`App.tsx` — "Add doc" button opens a modal with file picker + multi-destination folder checkboxes (writableFolders from folders.ts); posts folder_ids to /ingest.
`folders.ts` — pure helpers: Folder type, flattenTree, writableFolders (filters to manual+writable), uploadReducer. Vitest-covered across the folders/setup/citations/auth/settings suites.
Token secret shown once after POST; list views show metadata only.

## Commands

```bash
uv run pytest                      # full suite (<7s, ZERO network — must stay that way)
cd ui && npm test                  # vitest (folders + setup + citations + auth + settings suites)
uv run hippo sync <folder>         # ingest; re-run with no arg re-syncs all synced folders
uv run hippo serve                 # API :8000
cd ui && npm run dev               # chat UI :5173
uv run hippo eval eval/golden.yaml # retrieval recall@k regression gate
uv run hippo reindex               # re-embed after changing HIPPO_EMBEDDING_MODEL/DIM
uv run hippo backup <path>         # consistent single-file snapshot via VACUUM INTO
docker compose up --build          # build + run API+UI on :8000 (set .env first)
# CI: .github/workflows/ci.yml runs pytest + npm run build on every PR
```

Config via env (`HIPPO_` prefix) or `.env`: see README table. `HIPPO_EMBEDDING_MODEL=fake` = offline mode.

## Hard rules / gotchas

- **Tests never hit the network.** Use `FakeEmbedder` + pydantic-ai `TestModel`/`FunctionModel`; agent/api/enrich
  tests set `pydantic_ai.models.ALLOW_MODEL_REQUESTS = False`. Keep it.
- **No SQL outside storage.py** (now zero exceptions — `reindex` moved into `Storage.reindex`). The agent/API/ingest
  call the Storage interface — this is the Postgres exit ramp; don't erode it.
- **One `Storage` per connection.** `Storage` serializes its shared sqlite connection with a `threading.Lock`
  (event loop + `run_in_threadpool` workers share one `con`); network embedding stays outside the lock. Two
  `Storage` instances on one connection would each have their own lock — don't.
- **Embedding model is stamped** in `meta` on first ingest; ingesting with a different model is refused until
  `hippo reindex`. `reindex` embeds everything before swapping `chunk_vec`, so a failure leaves the index intact.
- Test dbs must NOT live inside folders that get synced (sqlite WAL files pollute rglob) — use a separate tmp dir
  for the db (see tests/test_ingest.py fixture).
- `Agent(...)` constructions need `defer_model_check=True` or import-time crashes without API keys.
- Chat protocol payloads require `"trigger": "submit-message"` (Vercel AI SubmitMessage schema).
- `chunk_vec` dim is fixed at table creation; changing embedding dim requires `hippo reindex` (drops/recreates table).
- TDD discipline: failing test first; commit per green step.
- **grep uses the `regex` module** with a wall-clock `timeout=` (not stdlib `re`) for ReDoS safety; `re` is still used for FTS tokenization. Pattern length is capped at 200 chars; both violations raise `ValueError`.
- **Tool output is framed as ⟦untrusted document data⟧** — don't strip the delimiters; they are the prompt-injection boundary enforced by the system-prompt "Untrusted content" rule.
- **Retrieval methods take `role` keyword-only with no default** — a forgotten call site must be a TypeError, never an access-control leak. Same for HubDeps.role.
- **Role rank is defined exactly once** in `roles.py` (`ROLE_RANK = {"user":0,"admin":1,"owner":2}`). `storage.py` calls `readable_min_roles(role)` from there; `api.py` calls `can_write`/`rank` from there. Do not copy-paste rank comparisons — import from `roles.py`.
- **Legacy DB is rejected loudly.** A pre-SP1 database (documents.source_id, no folders table) raises RuntimeError on `connect()`. Delete the `.db` file and re-sync — no migration path.
- **Three root folders are seeded** by `db.py` on first open: Default (user), Private (admin), Owner (owner). They cannot be deleted or moved. Child folders inherit the parent's tier.

## State (2026-06-13)

v1 + review-hardening merged to main: storage/hybrid search, ingestion (folder sync + upload),
enrichment, agent, API, CLI, React UI, eval harness. PR #2 landed two independent-review passes
(connection lock, safe reindex, embedding-model stamp, citation resolution, etc.). Roadmap items
1+2 (auth/roles/sources) implemented on branch `build/auth-and-sources` (PR pending). Roadmap
item 3 (production-readiness: ingestion limits, grounding enforcement, grep hardening, chunk/drawer
fixes, `hippo backup`, Docker, CI, logging) implemented on branch `build/production-readiness`
(PR #4 merged). Roadmap item 5 (.docx parsing via mammoth, `parse_bytes()` entry point, UI upload
accepts .docx) implemented on branch `build/docx-parsing` (PR pending). Roadmap item 6 (MCP server:
FastMCP at /mcp with bearer-token auth + role filtering, `hippo mcp` stdio command) implemented on
branch `build/mcp-server` (PR pending). Roadmap item 7 (Slack bot: Socket Mode, role-filtered Q&A,
DM+channel @mention, thread-aware history) implemented on branch `build/slack-integration` (PR pending).
Roadmap item 9 (Settings UI: gear toggle, role-gated tabs, self-service tokens, admin sources/users/status)
implemented on branch `build/settings-ui` (PR pending).

**PRODUCTIZATION EPIC (SP1+SP2+SP3) — COMPLETE, all merged to main 2026-06-13.** Each PR ran the loop
plan→subagent-build→Codex+Opus review-on-PR→fix-with-regression-tests→squash-merge; each surfaced and
fixed one real access-control issue in review.
- **SP1 — roles & content-folder model (PR #11, `c074935`):** roles renamed user/admin/owner (pure
  `roles.py` owns the rank rule); flat sources replaced by a folder adjacency tree with rank-filtered
  retrieval; surrogate-keyed users+tokens; /folders CRUD API + `require_owner`; multi-destination
  /ingest write-gated by `can_write`; React Folders tab + role-scoped upload modal; legacy-DB guard.
  Review fix: /folders PATCH/DELETE/resync now tier-check the target folder (was admin-floor only).
- **SP2 — built-in password auth (PR #12, `ae16979`):** 4th auth mode `password` (email+password,
  argon2id, lockout 5/15min, 7-day session), POST /auth/login + /auth/logout + GET /auth/config,
  self-service POST /me/password, admin reset POST /users/{email}/password, break-glass
  `hippo user set-password` CLI, React login screen + password-change + admin-reset UI. No default
  credentials; generic 401s; hashes never returned. Review fix: admin reset uses the EFFECTIVE role
  (a HIPPO_ADMIN_EMAILS user is owner-tier).
- **SP3 — first-run setup wizard & config store (PR #13, `85d21cd`):** DB `config` table; `Config`
  overlay resolver (DB wins ONLY for `DB_OVERRIDABLE`; **secrets env-only, never DB-sourced/returned/
  writable**); `setup_token` field; `GET /setup/status` + `POST /setup` (token-gated via
  `compare_digest`, atomic `claim_setup`, 409-after); `GET/PUT /config` (owner-only; non-operational
  keys rejected; embedding reindex guard; auth-mode anti-lockout `_validate_auth_switch` with mode
  prereqs); live `chat_model` per `/chat` via `_live_agent()`; other operational keys resolved at
  construction from the overlay (change → restart); SessionMiddleware once if `secret_key`;
  `/settings/status` reports effective overlay values + `setup_complete`; React first-run wizard
  (`setup.ts` pure logic, Vitest) + owner-only Instance Settings tab. Review fix: `allowed_domain`
  override now gates role resolution live; oidc/iap switch/setup prereqs enforced; oidc exchange uses
  effective client_id/public_url.
  **Caveat (by design):** `none` mode is open pre-setup (dev-only, emits a non-localhost startup
  warning) — a secure first-run uses `oidc`/`iap` env (IdP-gated even pre-setup) or keeps the box
  private until the wizard sets `password` mode.

**Full pytest + vitest suite green, eval 4/4 on seed fixtures, UI builds clean.**

**Active plan:** `docs/superpowers/plans/2026-06-13-sp3-setup-wizard.md` — SP3 complete. Next: scale (Postgres+pgvector), MCP client/connectors, deploy.

**Deferred (spec §12):** Google Drive connector (interface: `list_items()` + `fetch()` -> markdown),
PDF parsing (planned), Postgres+pgvector migration (reimplement Storage), hierarchical summaries, GraphRAG.
