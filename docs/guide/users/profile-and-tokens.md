# Your profile & access tokens

Open **Settings** with the gear (⚙) button in the header. The **My Profile** tab
is available to everyone.

## Display name and email

- Your **email is your login identity and is read-only.**
- You can set a friendly **display name** — edit it and save.

## Changing your password (password mode)

If your instance uses password sign-in, the My Profile tab has a password-change
form:

- Enter your **current password**, then the new one twice.
- New passwords must be at least **8 characters**.
- This is self-service — you don't need an admin.

(In Google/OIDC or IAP mode there's no password to change here; sign-in is
handled by your identity provider.)

## Personal access tokens

Tokens let **headless tools** act as you: the MCP server, the Slack bot, CI
scripts, or any API client. A token carries **your own role**, so it can never
see more than you can.

### Creating a token

1. Settings → My Profile → create a token (optionally name it).
2. The **plaintext token (`hk_…`) is shown exactly once.** Copy it immediately —
   Hippo only stores a hash and can never show it again.

### Listing and revoking

- The token list shows metadata only (name, created, last used) — never the
  secret.
- Revoke any of your tokens at any time; that immediately stops it working.

### Using a token

Send it as a bearer header to any Hippo surface:

```bash
curl -H "Authorization: Bearer hk_..." https://hippo.example.com/me
```

For MCP and Slack specifics see [Using MCP](using-mcp.md) and
[Using Slack](using-slack.md).

> **Treat tokens like passwords.** Anyone with your token can query Hippo as you.
> If one leaks, revoke it. Admins can also revoke tokens (but never one belonging
> to a user above their own tier).
