# Testing round — bugs & improvements (2026-06-13)

Clean-room test of the SP1–SP3 productized build (password mode, fresh DB).
Branch: `fix/testing-round-bugs`.

## Fixed (this branch)

1. **Vite dev proxy stale** — proxied removed `/sources`, missing `/folders`,
   `/setup`, `/config`; wizard + Folders + Instance tab 404'd through `:5173`.
   `6e5e80f`.
2. **Setup wizard: wrong token only caught at the final step.** Single-page
   rewrite → server validates token first, 403 renders inline immediately.
   `ea9b763`.
3. **Setup wizard: invalid email / short password silently disabled Next with
   no message.** Now inline per-field errors. `ea9b763`.
4. **Folder step dropped from setup** — tier-labeled folder naming isn't a
   first-run decision; roots keep seeded defaults. `ea9b763`.
5. **Setup form rendered as a horizontal row** (inherited chat-composer
   `form{display:flex}`). Scoped `.setup-form` block. `9214060`.

## Deferred (noted, NOT fixing now — per Saeed)

- **Folder creation UX is not fluent.** The create-folder flow in Settings →
  Folders feels clunky. Revisit the whole folder management UX later.
- **Owner cannot create / add / rename a top-level (root) folder.** By current
  design the three seeded roots can't be renamed/moved/deleted and new roots
  can't be added from the UI. Confirm whether that's the intended product
  behavior or a gap; address with the folder-UX rework.

## To do (this round, pending scope confirmation)

- **Users tab: cannot create a new user.** The panel only lists existing users
  and acts on those rows (set role / reset password). No "add user" form, so an
  admin can't onboard a teammate in password mode. (Backend already upserts via
  admin password-set.)
- **Rename "Tokens" tab → "My Profile."** Show the signed-in user's email +
  name, both editable, alongside password change and API tokens.
- **Add a `name` field to users** (currently only email/role) for the profile.
- **Rename "Instance" tab → "System config"** (or similar).
