# Admin tasks

Admins (and owners) get extra tabs in **Settings (⚙)**: **Folders** and **Users**,
plus a read-only **Status** tab. This page covers the day-to-day admin work.

## Managing folders

In **Settings → Folders** you see the whole folder tree with document counts and
per-folder actions.

- **Create a child folder** under any root or folder at or below your tier. The
  child **inherits its parent's tier** — to make an admin-only area, create it
  under the `Private` (admin) root.
- **Rename** a folder.
- **Move** a folder to a new parent. Moving rewrites the whole moved subtree's
  tier to the new parent's tier — so moving a folder under `Owner` makes
  everything in it owner-only.
- **Delete** a folder. This cascades: the folder, its descendants, and all their
  documents are removed.
- The three **root folders** (`Default`/`Private`/`Owner`) can't be moved or
  deleted.

> **Tier guard:** you can only manage folders **at or below your own role's
> tier**. An admin cannot rename, move, delete, or re-sync an owner-tier folder.

## Syncing a directory of files

Instead of uploading files one by one, you can mount a directory as a **synced
folder**:

1. The server must have that directory inside its `HIPPO_SOURCE_ROOTS` allowlist
   (an operator sets this — see [Configuration](../install/configuration.md)).
2. Create a folder with a *filesystem* origin pointing at the directory (via the
   Folders tab or `POST /folders` with `origin: folder` and a `location`).
3. Hippo ingests every supported file in it.
4. Use **Re-sync** on that folder later to pick up additions, changes, and
   deletions (removed files are pruned from the index).

Synced folders are read-only from the UI's perspective — you don't upload into
them; you manage their source directory and re-sync. The allowlist is re-checked
on every re-sync, so a path that falls outside a tightened allowlist stops
syncing.

## Managing users

In **Settings → Users**:

- **List** all users and their roles. The list shows each user's **effective**
  role (a bootstrap-admin email always shows as owner).
- **Create a user.** In password mode you can mint an initial password (shown
  once). In OIDC/IAP the user becomes real on first sign-in; you just pre-set the
  role.
- **Change a role.**

Guards that protect the instance:

- You **cannot grant a role above your own** tier.
- You **cannot lower your own role** (anti-lockout).
- You **cannot create or reset a user above your tier** — and because a
  bootstrap-admin email (`HIPPO_ADMIN_EMAILS`) always resolves to *owner*,
  creating any login for such an email is treated as creating an owner.

## Resetting a password (password mode)

From the Users tab, reset a lower-tier (or same-tier peer admin) user's password.
The new password is **shown once** — copy and share it securely. You can't reset
a user above your tier.

## The Status tab

A read-only view of the instance: effective auth mode and models (from the live
config overlay if set), setup status, MCP/Slack enabled flags, and document /
folder / user counts. No secrets are shown.

## Setting roles from the CLI

```bash
hippo role set someone@example.com admin
hippo role list
```

See the [CLI reference](../technical/cli.md) for the full command set.
