# Authentication setup

Hippo has four authentication modes, chosen with `HIPPO_AUTH_MODE`. **Bearer
tokens are accepted in every mode** for headless clients (MCP, Slack, CI) —
create one with `hippo token create <email>` or in Settings → My Profile.

This page covers when to use each mode, the env it needs, and the first-run
wizard. For the underlying model (roles, ranks, lockout, token resolution) see
[RBAC internals](../technical/auth-and-rbac.md).

## Choosing a mode

| Mode | Use when | Requires |
|---|---|---|
| `none` | Personal use or a trusted private network only. Every request is a local **owner**. | nothing |
| `password` | A team without an existing IdP. Email + password, argon2id-hashed, with lockout. | `HIPPO_SECRET_KEY` |
| `oidc` | You have Google Workspace and want in-app Google sign-in. | `HIPPO_SECRET_KEY`, `HIPPO_OIDC_CLIENT_ID`, `HIPPO_OIDC_CLIENT_SECRET`, `HIPPO_PUBLIC_URL` |
| `iap` | Deployed behind Google Cloud Identity-Aware Proxy. | `HIPPO_IAP_AUDIENCE` |

> **Security note on `none`:** the API is fully open, **including during the
> first-run window**. For anything network-reachable, either start in `oidc`/`iap`
> (gated by the IdP even before setup) or keep the box private until the wizard
> switches it to `password`. `hippo serve` prints a warning if `none` is bound
> beyond localhost.

## `password` mode

```bash
HIPPO_AUTH_MODE=password
HIPPO_SECRET_KEY=<a long random string>   # e.g. `openssl rand -hex 32`
```

- Email + password; the hash is argon2id; **no default credentials exist**.
- An account locks for **15 minutes after 5 consecutive failures**.
- Sign-in sets a signed session cookie with a 7-day lifetime (its `Secure` flag
  follows `HIPPO_PUBLIC_URL`'s scheme).
- The first owner is created by the [first-run wizard](#first-run-wizard) or the
  break-glass CLI:

  ```bash
  hippo user set-password owner@example.com --role owner
  ```

  It prompts twice (no echo). Re-run it to reset a forgotten password or unlock a
  locked-out account. `--role` only applies when creating a new user.

- **In the UI:** a login form replaces the Google button. Users change their own
  password in Settings → My Profile (requires the current password). Admins can
  reset a lower-tier user's password in the Users tab (shown once).

## `oidc` mode (Google sign-in)

```bash
HIPPO_AUTH_MODE=oidc
HIPPO_SECRET_KEY=<random>
HIPPO_OIDC_CLIENT_ID=<google oauth2 client id>
HIPPO_OIDC_CLIENT_SECRET=<google oauth2 client secret>
HIPPO_PUBLIC_URL=https://hippo.example.com    # the externally reachable HTTPS base
# HIPPO_ALLOWED_DOMAIN=example.com             # optional: restrict to one Workspace domain
```

Set up a Google OAuth 2.0 Web client. Its **authorized redirect URI** must be
`${HIPPO_PUBLIC_URL}/auth/callback`. Users hit `/auth/login`, authenticate with
Google, and get a session cookie.

> `HIPPO_PUBLIC_URL` matters twice: it forms the OAuth `redirect_uri`, and its
> scheme sets the session cookie's `Secure` flag. Behind a TLS-terminating proxy
> that forwards plain HTTP, still set it to the external `https://…` base.

## `iap` mode (GCP Identity-Aware Proxy)

```bash
HIPPO_AUTH_MODE=iap
HIPPO_IAP_AUDIENCE=/projects/<num>/global/backendServices/<id>
```

Deploy Hippo behind IAP; Hippo verifies the signed `x-goog-iap-jwt-assertion`
header (ES256) on every request and maps the asserted email to a role.

## Roles and bootstrap

Three roles: **`user`** (default) < **`admin`** < **`owner`**.

- Set roles with `hippo role set <email> <role>` or in Settings → Users (admin).
- **Bootstrap:** any email in `HIPPO_ADMIN_EMAILS` (comma-separated) is promoted
  to **owner** on every sign-in — your break-glass owner that doesn't depend on
  DB state. (The variable name is historical; it grants owner.)
- Content visibility is by folder tier, not just role — see
  [Documents & folders](../users/documents-and-folders.md).

## First-run wizard

When Hippo starts with an empty database it enters **setup mode**: the browser
shows a one-page setup form instead of chat.

1. **Setup token** — set `HIPPO_SETUP_TOKEN` before starting, or read the
   one-time token printed to the startup console (`first-run setup token:`).
2. **Auth mode** — `password`, `oidc`, or `iap` (`none` is dev-only env, not
   offered here).
3. **Owner account** — owner email; for `password`, an initial password (8-char
   minimum, validated inline).
4. **Models** (optional) — override `chat_model`/`enrich_model`; blank uses the
   env defaults. (Embedding model/dim are env-only.)

Submitting `POST /setup` creates the owner, persists the operational config, and
marks setup complete. It is **token-gated and one-shot** (409 afterward). The
three seeded root folders (`Default`/`Private`/`Owner`) keep their names — rename
them later in Settings → Folders.

The wizard is the recommended path for team deployments. For the secrets policy
and the endpoint internals, see [Config & setup](../technical/config-and-setup.md).
