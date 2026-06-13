# SP2 — Built-in password auth (design)

> Second of three productization sub-projects. Builds on SP1's surrogate-keyed `users` table
> and role model. SP3's setup wizard is the UI bootstrap for the owner account created here.
> Brainstormed 2026-06-13. See `memory: hippo-productization-roadmap` and the SP1 spec.

## 1. Goal

Add a fourth auth mode, **`password`**, so Hippo can be self-hosted without Google (oidc) or GCP
(iap): users sign in with **email + password**, credentials are stored hashed, and the existing
role model + bearer tokens keep working unchanged.

## 2. Scope

**In scope:** the `password` auth mode; a nullable `password_hash` on the `users` table;
argon2id hashing; a login/logout flow + session; failed-login lockout; self-service password
change; admin password reset in the Users UI; a break-glass `hippo user set-password` CLI.

**Explicit non-goals (deferred / elsewhere):**
- **Owner bootstrap is SP3's wizard.** SP2 ships the account-creation *primitive*
  (`storage.set_password` / create-local-user); the first owner is created by SP3's first-run
  wizard (or the break-glass CLI). No default credential, ever.
- **No identity-key change** — SP1 already moved users to a surrogate `user_id` PK with `email`
  as a unique attribute; SP2 only adds `password_hash`.
- oidc / iap / none modes are unchanged. A deployment runs exactly one mode.
- No SSO-to-local linking, no email verification, no "forgot password" self-service email
  (there's no mail layer) — recovery is admin reset or the break-glass CLI.

## 3. Identity & storage

- `users.password_hash TEXT NULL` — set only for local accounts; NULL for oidc/iap users.
- Hashing: **argon2id** (via `argon2-cffi`). A single `hash_password` / `verify_password` pair
  lives in `auth.py`. Tests use **reduced argon2 cost params** (low time/memory) via a profile so
  the suite stays fast and offline — hashing never touches the network.
- Lockout state on the user row: `failed_logins INTEGER`, `locked_until` timestamp (or a small
  helper table); cleared on success.

## 4. Auth flow

`verify_request` (api.py) gains a `password` branch, after the always-on bearer-token check:

1. Bearer token → resolve as today (works in every mode).
2. `auth_mode == "password"`: read `user_id` from the signed session (the same
   `SessionMiddleware` oidc uses). Missing/expired → `401` (the SPA shows the login screen).
3. Resolve `user_id → user` (email, role) and return `AuthenticatedUser`.

Endpoints (reusing the `/auth/*` namespace oidc established; behavior is per-mode):

- `POST /auth/login` — JSON `{email, password}`. Verifies against `password_hash`, enforces
  lockout, on success sets `session["user_id"]` and returns the user; on failure increments the
  counter and returns `401` (generic "invalid credentials" — no account enumeration).
- `POST /auth/logout` — clears the session.
- `password` mode **requires `HIPPO_SECRET_KEY`** (session signing), same as oidc; `serve`
  refuses to start in password mode without it.

## 5. Security

- **No default credentials.** The owner is created explicitly (wizard / CLI).
- **argon2id**, per-hash salt (argon2-cffi handles it); never log or return hashes.
- **Lockout:** after **5** consecutive failures for an email, lock that account for **15 min**
  (`locked_until`); successful login resets the counter. Generic error messages (no "no such
  user" vs "wrong password" leak).
- **Session:** signed cookie via `SessionMiddleware`; **7-day** expiry; `HttpOnly`, `SameSite=Lax`,
  `Secure` when served over HTTPS. Logout clears it.
- Login is a same-origin JSON POST guarded by the SameSite session cookie.
- Bearer tokens remain the headless path (MCP/Slack/CI) in password mode, unchanged.

(Lockout thresholds and session length are the defaults above — easy to make configurable later;
flag if you want different numbers.)

## 6. Bootstrap & recovery

- **Bootstrap = SP3 wizard.** On first run in password mode the wizard collects the owner's email
  + password and calls the same `set_password` primitive. SP2 alone is operable via:
- **Break-glass CLI:** `hippo user set-password <email>` — prompts for a new password (twice, no
  echo), hashes and stores it; creates the user if absent (with a role flag, default `user`).
  This is the locked-out-owner escape hatch and the headless-deploy path. Runs against the DB
  directly (shell access required), no server round-trip.

## 7. API + UI

- **Login screen** (React): shown when `auth_mode == "password"` and `/me` returns `401`. Email +
  password, error line, "locked — try again in N min" message. On success, the app loads as today.
- **Logout** control in the header (already present for oidc; extend to password mode).
- **Self-service password change:** Settings → a "Password" control (current + new + confirm) →
  `POST /me/password` (verifies current, enforces strength minimum, re-hashes).
- **Admin password reset:** in the Users tab (admin+), a "Reset password" action sets a new
  password for a local account and shows it **once** (no email to send it through); the user then
  changes it. Owners can reset anyone; admins up to admin.
- `/me` gains nothing structural — it already returns email + role + auth_mode (the UI keys the
  login screen off `auth_mode` + 401).

## 8. Schema & fresh start (no data migration)

Consistent with SP1: `db.py` creates `users.password_hash` (+ lockout columns) in the fresh
schema; no back-fill. Existing dev DBs are recreated. `password` is added to the `auth_mode`
`Literal` in `config.py`.

## 9. Testing

Zero-network (`FakeEmbedder`, `TestModel`; argon2 reduced-cost profile for speed):

- `hash_password`/`verify_password` round-trip; wrong password fails; hash never equals plaintext.
- `POST /auth/login`: success sets session; wrong password → 401; **lockout** after 5 fails, and
  `locked_until` blocks even a correct password until it lapses; counter resets on success.
- `verify_request` password branch: no session → 401; valid session → correct user/role; **bearer
  token still works in password mode**.
- Self-service change requires the correct current password; admin reset gated to admin+ and
  returns the new secret once; password never appears in any list/`/me`/status response.
- `password` mode without `HIPPO_SECRET_KEY` → `serve` refuses to start.
- CLI `hippo user set-password` sets a working credential (login succeeds afterward).

## 10. Interactions

- **SP1:** uses the surrogate `user_id` users table + role model (owner/admin/user).
- **`none` mode stays dev-only.** It remains a valid `auth_mode` for local development / single-user
  on a trusted machine (no login wall), but the SP3 wizard never offers it — real deployments only
  ever choose `password` / `oidc` / `iap`, so nothing lands wide-open by default. Documented as
  "no access control — local use only."
- **SP3:** the wizard is the UI bootstrap for the first owner and selects `auth_mode=password`;
  it calls the same `set_password` primitive defined here.
- **Auth mode is switchable later (SP3, owner-only).** Because identity is keyed by **email**,
  switching `password → oidc/iap` carries every user over unchanged (the `password_hash` just goes
  dormant); `oidc/iap → password` requires setting passwords for existing users (admin reset /
  break-glass). SP3 owns the switch UI, with **anti-lockout** (the owner must hold valid
  credentials in the target mode before the switch applies) and collection of the target mode's
  config. SP2's job is only to make `password` a first-class mode that participates cleanly.
