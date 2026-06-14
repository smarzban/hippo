# Troubleshooting & FAQ

## "No sources cited — verify independently"

Hippo answered without citations. A good answer should be grounded, so this is a
prompt to double-check it. Often it means the docs don't cover the question well
— try adding a relevant document and asking again, or rephrase more specifically.
See [Asking questions](asking-questions.md).

## "I reached my research limit for this question"

Your question needed more lookups than the per-answer budget allows. Narrow it,
or split it into separate questions. (Operators can raise `HIPPO_MAX_TOOL_CALLS`.)

## A citation isn't clickable

The cited source didn't resolve to a document you can currently open — it may
have been removed, or it's in a folder above your tier. Treat the claim with
extra care.

## I can't see a document I know exists

Visibility is by folder tier vs. your role. If a document is in an `admin`- or
`owner`-tier folder and you're a `user`, you won't see it in answers or lists.
Ask an admin where it lives, or to move it to a folder you can read. See
[Documents & folders](documents-and-folders.md).

## My account is locked (password mode)

Five wrong passwords in a row locks the account for **15 minutes**. Wait, or ask
an admin to reset your password (which also clears the lock). After the window
passes, the failure counter resets on your next attempt.

## I can't sign in with Google

- Your instance may be restricted to one Workspace domain — use an account on
  that domain.
- If you're an operator: confirm the OAuth client's authorized redirect URI is
  `${HIPPO_PUBLIC_URL}/auth/callback` and that `HIPPO_PUBLIC_URL` is your external
  `https://…` base. See [Auth setup](../install/auth-setup.md).

## Upload was rejected

- **Too large (413):** over `HIPPO_MAX_UPLOAD_BYTES` (10 MB default).
- **Skipped:** over `HIPPO_MAX_DOC_CHARS` (very large document).
- **Unsupported type:** Hippo accepts `.md`, `.txt`, `.html`/`.htm`, `.docx`. For
  Google Docs, download as `.docx` first.
- **No writable folder shown:** you can only upload into manual folders at or
  below your tier.

## My MCP client gets 401 / "invalid or missing token"

The bearer token is missing, wrong, or revoked. Create a fresh one in Settings →
My Profile and re-add the server with the `Authorization: Bearer hk_…` header.
See [Using MCP](using-mcp.md).

## The server won't start: "recreate the database"

Your database predates the current schema (it has no `folders` table). There's no
migration — delete the old `.db` file and re-sync your content. See
[Upgrading](../install/upgrading.md).

## Answers changed after I switched models

`chat_model` is live — changing it (Settings → System config, owner) affects the
next answer immediately. Embedding changes are different: they need
`hippo reindex`. See [Owner tasks](owner-tasks.md).

## Operator: where are the logs I should watch?

`hippo.auth` (auth denials) and `hippo.audit` (privileged changes). See the
[Production checklist](../install/production.md).

## Still stuck?

If you're an engineer, the [Technical docs](../technical/README.md) explain how
each piece works — start with the [Architecture](../technical/architecture.md).
