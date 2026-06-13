# SP3 — First-run setup wizard & config store (design)

> Third productization sub-project — the capstone. Turns Hippo into "`docker run` it, open the
> browser, set it up." Builds on SP1 (roles + folder roots) and SP2 (password auth). Brainstormed
> 2026-06-13. See `memory: hippo-productization-roadmap` and the SP1/SP2 specs.

## 1. Goal

Let an operator stand up Hippo entirely from the browser on first run — choose the auth mode,
create the owner, name the role-tier root folders, and pick the LLM models — backed by a
**runtime-mutable config store** so owners can change operational settings later without editing
env or restarting.

## 2. Scope

**In scope:** a DB **config store** overlaying env defaults; **first-run detection** + a
token-gated **setup wizard**; a post-setup owner-only **Instance Settings** panel (change model
names, switch auth mode, rename roots); the model/provider step (model names only — keys stay in
env, per SP2 §A).

**Explicit non-goals (deferred / elsewhere):**
- **Secrets never enter the DB.** API keys, oidc `client_secret`, `HIPPO_SECRET_KEY` stay in env;
  the wizard collects model **names** + non-secret params and *validates* required secret env vars
  are present (SP2 decision A). No encryption-at-rest subsystem.
- **No `none` in the wizard** — it only offers `password`/`oidc`/`iap` (SP2 §10).
- **Owner bootstrap mechanism** (the `set_password` primitive, break-glass CLI) is SP2; this spec
  *calls* it.
- No multi-tenant / multi-instance config; one instance, one config row-set.

## 3. Config store

A `config` table (`key TEXT PK, value TEXT`) holds the **operational, non-secret** settings.
`Settings` resolves each key as **env first (default), then DB override** — so env remains the
ops/k8s escape hatch and a DB value only overrides operational keys. Split:

- **DB-overridable (UI-settable):** `auth_mode`, `chat_model`, `embedding_model`, `enrich_model`,
  `embedding_dim`, oidc `client_id` / `public_url` / `iap_audience` / `allowed_domain`, the three
  root-folder names, lockout/session numbers.
- **Env-only (never in DB):** provider keys + `OPENAI_BASE_URL`, oidc `client_secret`,
  `HIPPO_SECRET_KEY`, `HIPPO_DB_PATH`, `HIPPO_SETUP_TOKEN`.

Reload semantics: operational values are read **fresh** (cheap SQLite read, cached with
invalidation on write) so changes take effect without a restart — the agent reads the current
`chat_model`/`enrich_model` when it's built per request. **Exception — `embedding_model` /
`embedding_dim`:** changing these after documents exist requires `hippo reindex` (the `chunk_vec`
dim is fixed at table creation). The wizard sets them at **first run (empty index → safe)**;
post-setup, Instance Settings makes them **read-only with a "change via `hippo reindex`" note**
rather than silently corrupting the index.

## 4. First-run detection & setup token

- **First-run** = no owner user exists (≡ setup not complete). A `config` flag `setup_complete`
  is set true at the end of the wizard.
- **Gate:** while not complete, the wizard endpoints require the **setup token**:
  `HIPPO_SETUP_TOKEN` from env if set; otherwise Hippo generates a random token on first start and
  **logs it** (so the wizard is never ungated). Submitted token is compared in constant time.
- Once `setup_complete`, the wizard endpoints return `404/409` and the token is inert. The app
  serves normally (login screen for password/oidc, or the chat UI).
- Threat model: the brief first-run window is unauthenticated-but-token-gated, so a reachable
  fresh instance can't be claimed without the token (which lives in env/logs = server access).

## 5. Wizard flow (UI)

A dedicated first-run view (no header gear, no chat) stepping through:

1. **Setup token** — enter the token to unlock the rest.
2. **Auth mode** — `password` | `oidc` | `iap` (not `none`).
3. **Owner account** —
   - `password`: owner **email + password** → calls SP2 `set_password`, role `owner`.
   - `oidc`/`iap`: owner **email** (becomes owner on first sign-in) + the mode's non-secret config
     (client_id/public_url or audience); the wizard validates the secret env vars
     (`client_secret`/`secret_key`) are present and refuses to finish otherwise.
4. **Root folders** — names for the three tiers (defaults `Default`/`Private`/`Owner`), written as
   SP1's three root folder rows.
5. **Models** — `chat_model`, `enrich_model`, `embedding_model` + `embedding_dim` (presets for
   OpenAI / Ollama-local, or custom), with a note that the provider **key/base-URL come from env**;
   the wizard validates the provider responds (best-effort) or that the key env var is set.
6. **Finish** — write config, set `setup_complete`, drop into the app (logged in as owner for
   `password`).

## 6. Post-setup: Instance Settings (owner-only)

A new owner-only Settings tab to change what the wizard set, without re-running it:

- Edit model names (chat/enrich); `embedding_model`/`embedding_dim` shown read-only with the
  reindex note (§3).
- Rename the three root folders.
- Edit lockout/session numbers.
- **Switch auth mode** (SP2 §10) with **anti-lockout**: the owner must hold valid credentials in
  the *target* mode before the switch applies (→`password` requires the owner set a password
  first; →`oidc` requires the owner email match `allowed_domain`), and the target mode's required
  secret env vars must be present. `none` is not offered here either.

## 7. API

- `GET /setup/status` — `{setup_complete, auth_modes_available}` (unauthenticated; no secrets).
- `POST /setup` — token-gated, only while `not setup_complete`; performs steps 2–6 atomically;
  idempotent-safe (re-submitting after completion → `409`).
- `GET /config` / `PUT /config` — owner-only; read/write the DB-overridable keys; **secret values
  are never returned** and writing a secret key is rejected (those are env-only).
- Existing `/settings/status` keeps reporting effective (non-secret) config + counts.

## 8. Schema & fresh start (no data migration)

Consistent with SP1/SP2: `db.py` creates the `config` table; `setup_complete` defaults false on a
fresh DB. No back-fill. `auth_mode` `Literal` already includes `password` (SP2) and `none`
(dev-only). Recreate dev DBs.

## 9. Testing

Zero-network (`FakeEmbedder`, `TestModel`; argon2 reduced-cost):

- **Config overlay:** DB value overrides env for an operational key; a secret key is never taken
  from DB; unknown key rejected.
- **Setup gating:** with no owner, `/setup` requires the token (wrong/absent → 401/403; env token
  and logged-fallback token both work); after `setup_complete`, `/setup` → 409 and the token is
  inert.
- **Wizard happy paths:** `password` run creates the owner + roots + model config and flips
  `setup_complete`; `oidc` run refuses to finish if `client_secret`/`secret_key` env are missing.
- **Embedding guard:** changing `embedding_dim` via `/config` after documents exist is rejected
  with the reindex message; allowed on an empty index.
- **Auth switch anti-lockout:** owner can't switch to a mode where they'd have no valid credential;
  switch to a mode missing its secret env vars is rejected.
- **Secrets:** no secret value ever appears in `/config`, `/setup/status`, `/settings/status`.
- **Vitest:** wizard step-state reducer; "is setup complete" routing.

## 10. Interactions

- **SP1:** the wizard names + creates the three root folders; config store sits beside the folder
  tree in the same DB.
- **SP2:** the wizard is the UI bootstrap calling `set_password`; Instance Settings hosts the
  auth-mode switch SP2 designed for.
- After SP3, the standard path is: `docker run` (env: DB path, `SECRET_KEY`, any provider key,
  `SETUP_TOKEN`) → open browser → wizard → running.
