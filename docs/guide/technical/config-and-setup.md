# Configuration & first-run setup

Two pieces: `Settings` (env) and the `Config` overlay (a DB layer for a small set
of operational keys), plus the first-run wizard endpoints.

## `Settings` (`config.py`)

`Settings` is a pydantic-settings model with the `HIPPO_` env prefix. It holds
every configurable value (db path, models, chunking, limits, auth wiring, source
roots, integration toggles, the setup token). `auth_mode` is typed
`Literal["none","oidc","iap","password"]`. See the full table in
[Configuration reference](../install/configuration.md).

`setup_token` is **env-only and never stored in the DB**; if it's empty and setup
is incomplete, `build_context` generates a random one and prints it to **stderr**
(not the application logger, so it doesn't persist in a log aggregator).

## The `Config` overlay

`Config(settings, store).get(key)` resolves a value:

- If `key` is in **`DB_OVERRIDABLE`** *and* set in the `config` table → the DB
  value wins.
- Otherwise → `settings.<key>` (the env default).
- **Secrets are never read from the DB**, even if a row somehow existed.

`DB_OVERRIDABLE` is a frozenset:

```
{auth_mode, chat_model, enrich_model, allowed_domain,
 oidc_client_id, public_url, iap_audience}
```

### Live vs. resolved-at-construction

- **`chat_model`** is read **live per `/chat`** via `AppContext.live_agent()` — no
  restart needed; the agent is rebuilt + cached when it changes.
- **`allowed_domain`** is read **live** in role resolution / create-user /
  auth-switch validation.
- The rest (`auth_mode`, `enrich_model`, oidc/iap wiring) are resolved at
  construction and take effect on the next `hippo serve` restart.

### Why `embedding_model`/`embedding_dim` are NOT overridable

They were deliberately **removed** from `DB_OVERRIDABLE` and are **env-only**. The
vector space and the `chunk_vec` table width are fixed at index creation; a DB
override could neither take effect (the embedder is built from env *before* the
overlay exists) nor stay accurate after a `reindex`. So the env-built embedder is
the single source of truth, and a `PUT /config` that tries to set them is rejected
(400). To change embeddings: set `HIPPO_EMBEDDING_*` and run `hippo reindex`. See
[Embeddings](embeddings.md).

## The secrets policy

`OPENAI_API_KEY`, `HIPPO_OIDC_CLIENT_SECRET`, `HIPPO_SECRET_KEY`,
`HIPPO_SETUP_TOKEN`, and Slack tokens are **never stored in the DB, never
returned by any endpoint, and never editable via `/config`**. `GET /config` and
`GET /settings/status` return only operational (non-secret) values.

## The config-store methods (`storage/config_store.py`)

`get_config`/`set_config` (upsert), `is_setup_complete`/`mark_setup_complete`,
`claim_setup` (atomic first-run claim), and the cheap counts (`document_count`,
`folder_count`).

## First-run wizard endpoints (`routes_session.py`)

- **`GET /setup/status`** (public): `{setup_complete, auth_modes_available}`.
- **`POST /setup`** (token-gated, one-shot): the wizard endpoint. It
  1. 409s if already complete;
  2. compares the token with `secrets.compare_digest`;
  3. validates the chosen mode's required **secrets + effective prereqs**
     (`require_mode_prereqs`) — you can't enable a mode that would brick on
     restart;
  4. **atomically claims** setup (`claim_setup`) so concurrent requests can't both
     create an owner;
  5. creates the owner (password hash, or pre-set role for oidc/iap);
  6. persists the operational config (DB-overridable keys only — never embedding
     keys, even if sent);
  7. for password mode, logs the owner in immediately.

## Live config endpoints (`routes_admin.py`)

- **`GET /config`** (owner): effective value per `DB_OVERRIDABLE` key (DB else
  env); never a secret.
- **`PUT /config`** (owner): upsert overridable keys. Rejects unknown/secret/
  env-only keys (including embedding keys) with 400. An `auth_mode` change runs
  `validate_auth_switch` (mode prereqs + anti-lockout: an owner must hold a valid
  credential in the target mode).

## The pre-setup security caveat

By design, **`none` mode is open pre-setup** (dev convenience; `serve` warns when
bound beyond localhost). A secure first run uses `oidc`/`iap` env (IdP-gated even
before setup) or keeps the instance private until the wizard switches it to
`password`. See [Production](../install/production.md) and
[Security model](security-model.md).
