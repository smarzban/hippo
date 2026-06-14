# Owner tasks

Owners can do everything admins can, plus live-edit the instance's operational
configuration in **Settings (⚙) → System config**.

## System config

These settings live in the database and can be changed without editing
environment files. The owner UI (or `PUT /config`) edits them:

| Setting | Effect |
|---|---|
| **`chat_model`** | The model used to answer questions. **Live** — the next chat uses it, no restart. |
| **`enrich_model`** | The (cheap) model used to add context at ingest. Takes effect on next restart. |
| **`auth_mode`** | `none` / `oidc` / `iap` / `password`. Takes effect on next restart, and has an anti-lockout guard (below). |
| **`allowed_domain`** | Restrict sign-in to one Google Workspace domain. Next restart. |
| **`oidc_client_id` / `public_url` / `iap_audience`** | OIDC/IAP wiring. Next restart. |

**`embedding_model` / `embedding_dim` are read-only here** once documents exist —
the vector space is fixed at index creation. To change embeddings, an operator
sets `HIPPO_EMBEDDING_*` in the environment and runs `hippo reindex`. See
[Upgrading](../install/upgrading.md).

### The anti-lockout guard on `auth_mode`

You can't switch into a mode you couldn't then sign into. Before switching to:

- **`password`** — an owner must already have a password set (use the break-glass
  CLI or an admin reset first).
- **`oidc` / `iap`** — an owner email must satisfy the allowed-domain gate, and
  the mode's required wiring (client id / audience) must be present.

This prevents accidentally locking everyone (including yourself) out.

## What stays env-only

**Secrets are never editable here and never stored in the database:** the OpenAI
key, the OIDC client *secret*, the session `HIPPO_SECRET_KEY`, the setup token,
and Slack tokens. They always come from the environment. The System config tab
shows operational settings only — never a secret.

## The first-run wizard

The very first time Hippo starts with an empty database, *you* (the first owner)
set it up through the browser wizard: enter the setup token, choose the auth
mode, create the owner account, and optionally pick the chat model. See
[Auth setup → First-run wizard](../install/auth-setup.md#first-run-wizard).

After setup, rename the three default root folders (`Default`/`Private`/`Owner`)
to suit your org in **Settings → Folders**.

## Owner-only via CLI / API

- `hippo user set-password owner@example.com --role owner` — the break-glass way
  to (re)create the owner credential or unlock it.
- `GET /config` / `PUT /config` — the API behind the System config tab
  (owner-only). See the [API layer](../technical/api-layer.md).
