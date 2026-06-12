# Hippo Team Readiness — Design (2026-06-12)

Covers roadmap items **1 (Google SSO auth)**, **2 (`/sources` allowlist)**, **3
(production-readiness)**, **5 (Word parsing)**, **6 (MCP server)** — brainstormed and
approved with Saeed on 2026-06-12. Implementation order: 1+2 → 3 → 5 → 6.

Deployment target: Google Kubernetes Engine behind **IAP**, own HTTPS domain (e.g.
`hippo.example.com`). Gate #1: a `example.com` Google Workspace account.
Local/personal use must keep working with zero auth infrastructure.

---

## 1+2. Authentication, roles, and the sources allowlist (one work item)

### Identity: three pluggable modes behind `verify_request`

The app-level contract is small: *give me a verified email for this request*. Everything
downstream (domain gate, role lookup, retrieval filtering) is identical regardless of how
the email was established. `HIPPO_AUTH_MODE` selects one of three verifiers:

| Mode | Mechanism | Use |
|------|-----------|-----|
| `iap` | Verify GCP IAP's signed `x-goog-iap-jwt-assertion` header (signature against Google's public keys + audience check), extract email. No login UI or sessions in-app — IAP already did that at the load balancer. | Acme production |
| `oidc` | Hippo runs its own Google login: redirect to Google, verify the ID token (incl. `hd` domain claim), set a signed session cookie. | Orgs without IAP; demos; full-stack local testing |
| `none` | Single implicit user with `admin` role. Current behavior. | Personal-first / laptop / dev |

All modes converge on `AuthenticatedUser(email, role)` attached to the request before any
route logic runs. `HIPPO_ALLOWED_DOMAIN` (e.g. `example.com`) is enforced in both real
modes — IAP already gates entry, but defense-in-depth costs one `if`.

**Personal access tokens** are a fourth credential (not a mode): `Authorization: Bearer
hk_…` headers are accepted in any mode, resolved against tokens stored **hashed** in the
DB, each tied to a user (and therefore a role). Created via `hippo token create` or a UI
button. This is how MCP clients and other headless callers authenticate. In the IAP
deployment, `/mcp` is exempted from IAP (IAP bypass for that path only) and protected by
Hippo tokens instead.

Rejected alternatives:
- *IAP-only* — ties auth to GCP; local dev diverges from prod; orgs without IAP locked out.
- *In-app OIDC only* — wastes the IAP the org already runs; double login.
- *Auth middleware per route* — `verify_request` is already the single seam on every route;
  keep it that way.

### Roles: a `users` table, three tiers

- `developer` (default on first login) — sees everything except manager sources.
- `manager` — everything, including manager sources.
- `admin` — everything + source management, role management, settings.

Bootstrap: `HIPPO_ADMIN_EMAILS` (comma-separated) are admins on first login. Promotion:
`hippo role set <email> <role>` now; settings UI later (roadmap item 9). Role lookup is one
function — swappable later for Google Groups membership if duplicate admin becomes a pain
(rejected for v1: needs Workspace Directory API + domain-wide delegation + a service
account, and excludes non-Workspace orgs).

### Authorization: access level per *source*, enforced in `Storage`

The unit of access is the **source**, not the document. Each registered source carries
`access: everyone | managers`. Documents inherit their source's level. Manager content =
whatever lives in a manager-level source. No per-document ACLs (rejected: complexity with
no current requirement).

**Enforcement lives at the retrieval layer.** `search_hybrid`, `grep`, `read_document`,
`list_documents` all take the caller's role and filter manager-source documents out for
non-managers — inside `Storage`, so the agent's own tools physically cannot surface
manager content to a developer, and the same guarantee covers chat, REST, and MCP without
per-surface logic. UI-level hiding alone was rejected: the agent quotes retrieved chunks,
so anything reachable by retrieval is disclosed.

Admin-only routes: source registration/removal, role changes.

### Document feed: two git repos, synced into the pod

- **`hippo-docs`** — whole team read/write on GitHub. Source access: `everyone`.
- **`hippo-docs-managers`** — restricted to the managers GitHub team. Source access: `managers`.

GitHub permissions gate raw-file access; Hippo roles gate query access — the two models
line up one-to-one. (One repo with a protected folder was rejected: git read access is
repo-granularity; CODEOWNERS gates writes, not reads.)

The pod syncs both repos to local checkouts (git-sync sidecar in k8s; plain `git pull` +
`hippo sync` on a laptop). Hippo's existing `sync_folder` already handles updates and
deletions idempotently, so a periodic re-sync is the whole ingestion story — no new
machinery. Version control comes free: git is the canonical store, the DB is a derived,
rebuildable index, and doc history is `git log`.

### `/sources` allowlist (closes review M1)

- `HIPPO_SOURCE_ROOTS` — list of directory roots the server may ingest from. Registration
  of any path outside them is refused.
- Source registration/removal is **admin-only**.
- With auth on every route, the unauthenticated-mutation hole is closed; the allowlist
  additionally stops an *admin* mistake (or stolen admin session) from indexing `/etc`.

### Upload-to-repo: version control as the default path

The UI "Add doc" button commits the file to the appropriate repo via the **GitHub Contents
API** (one HTTP call per file — no clone, no local git state), with the uploader's name in
the commit message. The next sync ingests it. Managers get a repo picker (team repo or
managers repo); developers always commit to the team repo. Config: a GitHub token +
repo names. If no GitHub config is present (personal mode), upload falls back to today's
direct-to-DB ingestion, labeled "unversioned".

---

## 3. Production-readiness

From the two independent reviews (verdicts: `/tmp/hippo-review-response.md`,
`/tmp/hippo-review-2-assessment.md`) plus deployment needs:

- **Ingestion limits** (M2/L7): cap upload bytes, per-file bytes, max doc chars
  pre-enrichment, max files+bytes per sync. Reject before decode/embed; oversized →
  `skipped`, not `failed`.
- **Grounding enforcement** (M3): (a) delimit + label tool output as untrusted *data*
  in the prompt; (b) lightweight post-stream check (≥1 citation when factual claims made,
  cited paths exist in the retrieved set) surfaced as a soft UI warning — NOT hard
  reject/rewrite (research-grade, deferred); (c) prompt-injection test fixtures (a doc
  that says "ignore your instructions" must still get cite-or-refuse behavior).
- **grep hardening** (M4): keep regex; cap pattern length; wall-clock timeout via the
  `regex` module's `timeout=`. Literal-only mode rejected — regex is grep's purpose.
- **Chunk overlap fix** (L5): re-check `len(tail)+len(text)+sep <= max_chars` after
  prepending overlap; regression test for near-limit adjacent paragraphs.
- **Doc-drawer HTTP-error fix** (L6): check `r.ok` in `DocDrawer.tsx`; keep `doc` null on
  failure.
- **`hippo backup`** (L7): `VACUUM INTO` for a consistent single-file snapshot regardless
  of WAL state; document WAL-safe copy.
- **Serve the built UI from FastAPI** (`ui/dist` as static files). Stops being optional
  with auth: the session cookie and the app must share one origin. Vite dev server remains
  the dev workflow (proxy unchanged).
- **CI**: GitHub Actions on PRs — `uv run pytest` + `npm run build` (+ `hippo eval` on the
  golden fixtures).
- **Logging/observability**: structured request + ingestion logging; quiet by default.
- **Dependency version bounds** in `pyproject.toml`.
- **Docker**: multi-stage Dockerfile (build `ui/dist` → `uv` install → one image serving
  API + static UI) + a compose file for local testing (env, volumes, host-Ollama
  networking). Kubernetes manifests are **deferred** to a new roadmap item, *Deploy at
  Acme* (manifests, IAP audience config, git-sync sidecar, probes): IAP and the
  real load balancer can't be exercised locally, so a local kind/minikube setup would test
  none of the parts that carry risk, while `docker run` exercises everything that can be.

---

## 5. Word parsing (PDF dropped)

- Add `.docx` via **mammoth** (small, pure-Python): real heading styles survive into
  markdown, so heading-aware chunking and section citations work properly. This is the
  Google-Docs-download default format — the path managers actually use.
- **PDF support dropped from this round** (most of the corpus is markdown/Google Docs;
  pdfminer-class extraction loses headings; docling recovers structure but costs gigabytes
  of ML dependencies). Future item; the `parse_file` interface keeps it a one-file change.
- **Google-Doc-by-URL rejected here** — that's the Drive connector (roadmap item 8): OAuth
  scopes, whose-credentials questions, narrower-sharing edge cases. Flow for now:
  download as `.docx` → upload button → committed to repo → ingested.

---

## 6. MCP server

Expose Hippo's four tools (`search`, `read_document`, `list_documents`, `grep`) to
MCP-speaking harnesses:

- Official MCP Python SDK, **mounted inside the existing FastAPI app at `/mcp`**
  (streamable-HTTP transport): same process, same `Storage`, same role filtering, no
  second service. Plus `hippo mcp` (stdio) for the local single-user case.
- **Auth: personal access tokens** (see §1). The token maps to a user; the user's role
  filters retrieval. A manager's Claude Code sees manager docs; a developer's cannot —
  enforced in `Storage`, not the client.
- Client support: Claude Code natively (`claude mcp add --transport http hippo
  https://…/mcp --header "Authorization: Bearer …"`); Claude Desktop via the `mcp-remote`
  stdio↔HTTP proxy. **claude.ai web connectors deferred**: they require the full MCP
  OAuth flow (dynamic client registration, authorization-code + PKCE) and a publicly
  reachable endpoint; the `oidc` mode is most of the groundwork, so this becomes its own
  roadmap item rather than a rework.

---

## Testing strategy

The zero-network rule holds everywhere:

- Auth: forge/verify IAP-style JWTs against a local test key; OIDC flow tested with a
  stubbed token verifier; token auth round-trips against the hashed store.
- Role filtering: fixtures with `everyone` + `managers` sources; assert each tool's
  visibility per role at the `Storage` level and through the API.
- Upload-to-repo: GitHub Contents API behind a small client interface; fake in tests.
- Grounding: prompt-injection fixture docs + `FunctionModel` agents; post-stream citation
  check unit-tested on canned transcripts.
- MCP: in-process client from the MCP SDK against the mounted app; no sockets.
- Docker build verified in CI (build-only is fine; no network at test time).

## Out of scope this round (→ roadmap)

Scale/Postgres+pgvector (4) · Slack (7) · Drive connector (8) · settings UI (9) ·
**Deploy at Acme** (k8s manifests, IAP wiring, git-sync sidecar) · **MCP OAuth for
claude.ai** · PDF parsing (docling or similar) · Google Groups role mapping.
