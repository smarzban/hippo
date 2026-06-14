# Authentication & RBAC

Two layers: **authentication** (who are you?) in `auth.py` + the API auth
dependencies, and **authorization** (what can you see/do?) anchored by `roles.py`
and enforced in `storage/`.

## Roles and rank (`roles.py`)

The single source of truth for the rank comparison — nothing else defines it:

```python
ROLE_RANK   = {"user": 0, "admin": 1, "owner": 2}
VALID_ROLES = {"user", "admin", "owner"}
DEFAULT_ROLE = "user"
```

Pure helpers: `rank(role)`, `can_read(role, min_role)`, `can_write(role,
min_role, origin)`, `readable_min_roles(role)` (the set of folder tiers this role
may read — used by `_role_filter`). `roles.py` imports nothing from the rest of
Hippo; everything imports rank logic from here. **Do not copy-paste rank
comparisons.**

## The four auth modes

Wired in `api/`; `verify_request` (built by `make_auth_deps`) dispatches:

- **`none`** — every request is `AuthenticatedUser("local", "owner")`. Dev /
  private only.
- **`oidc`** — Google OAuth2 code flow. `/auth/login` redirects to Google;
  `/auth/callback` validates the ID token (`validate_google_id_token`,
  claims-only), checks the domain, stores `email` in the session. The code
  exchange uses the **effective** client_id/public_url.
- **`iap`** — `IapVerifier` verifies the `x-goog-iap-jwt-assertion` (ES256;
  injectable key fetcher) on every request.
- **`password`** — session keyed by surrogate `user_id`; see below.

**Bearer tokens work in every mode** (checked first in `verify_request`): a valid
`hk_…` token resolves to its owner's email, then to a role. This is how MCP/Slack/
CI authenticate.

### Shared identity → role: `resolve_role`

`resolve_role(store, settings, email, allowed_domain=...)` is the one path from
identity to role: normalize the email, domain-gate it, `ensure_user`, and
**bootstrap** — any email in `HIPPO_ADMIN_EMAILS` is force-promoted to **owner**
on every request. It's used by both the HTTP path (`user_for`) and the MCP gate,
and reads the **effective** `allowed_domain` (DB overlay wins) so a domain set via
the wizard/`PUT /config` gates live.

## Password auth specifics

- Hashing is **argon2id** (`hash_password`/`verify_password`, with a test hook to
  swap in a reduced-cost profile). Hashes are never logged or returned.
- **Lockout:** `LOCKOUT_MAX_FAILURES = 5`, `LOCKOUT_MINUTES = 15`. Failures
  increment a counter; reaching the threshold sets `locked_until` (DB-clock
  based). On a successful login the state resets. `clear_lock_if_expired` decays
  an elapsed lockout so the counter doesn't permanently soft-lock an account.
- **No enumeration:** missing user, no local password, and bad password all
  return the same generic 401. Failed attempts are logged (`hippo.auth`, email
  sanitized) for alerting.
- **Sessions:** 7-day signed cookies via `SessionMiddleware` (requires
  `HIPPO_SECRET_KEY`).

## Authorization: folder tiers

Content lives in a folder tree; each folder has a `min_role` tier inherited from
its parent. Two predicates:

- **Read** — `readable_min_roles(role)` drives `_role_filter`, so retrieval only
  returns chunks in folders the role may read. Applied in SQL in `storage/`.
- **Write** — `can_write(role, folder.min_role, folder.origin)` gates `/ingest`:
  your rank must meet the folder's tier, **and** the folder must be `manual` (you
  can't upload into a filesystem-synced folder).

### The fail-closed signature

Retrieval methods (`search_hybrid`/`grep`/`list_documents`/`get_document`/
`list_document_meta`) and `HubDeps.role` take `role` **keyword-only with no
default**. A forgotten call site is a `TypeError` at import/call, never a silent
"everything is visible." This is intentional and must stay.

## Authorization: the API guards

- `require_admin` (rank ≥ 1) gates folder/user mutations; `require_owner` (rank ≥
  2) gates owner-only ops (`/config`).
- **`require_folder_tier`** layers on top: a caller can only manage folders at or
  below their own tier (an admin can't move/delete/resync an owner-tier folder —
  and a move rewrites the subtree's tier, which would otherwise leak owner docs
  down).
- **Effective-role guards** appear in create-user, role-change, password-reset,
  and cross-user token-revoke: because a `HIPPO_ADMIN_EMAILS` email always
  resolves to *owner*, these compare against the **effective** role, so a rank-1
  admin can't mint/escalate/hijack an owner via a bootstrap email.
- **Anti-lockout:** you can't lower your own role, and switching `auth_mode`
  requires you already hold a valid credential in the target mode
  (`validate_auth_switch`).

## Personal access tokens

`hk_…` tokens are minted per user; only the **sha256** is stored, the plaintext
is returned exactly once. Each token carries the **owner's** role (no
escalation). Self-revoke is always allowed; an admin may revoke another user's
token but **never** one owned by a user above their tier (`token_owner` +
effective-role check). See [Profile & tokens](../users/profile-and-tokens.md).

## Why it's shaped this way

- **One rank definition** prevents drift between layers.
- **Filter at the data layer** means every surface (chat/MCP/Slack) is covered by
  construction, not by remembering to re-check.
- **Fail-closed signatures** turn a missing `role` into a crash, not a leak.
- **Effective-role everywhere** closes the bootstrap-admin escalation class that
  review repeatedly probed.
