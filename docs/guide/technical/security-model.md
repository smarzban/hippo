# Security model

Hippo's security rests on a few invariants enforced in code, plus a deliberate
threat model. This page collects the "why" behind the hard rules.

## What Hippo defends

1. **Unauthorized read of higher-tier content.** A `user` must never receive an
   answer (or a search hit, or a document) sourced from an `admin`/`owner`-tier
   folder — across chat, MCP, and Slack.
2. **Privilege escalation.** A lower-tier user must not be able to grant
   themselves a higher role, create/reset a higher-tier account, or hijack a
   higher-tier credential.
3. **Prompt injection.** A malicious document must not be able to make the agent
   ignore its instructions or exfiltrate other content.
4. **Resource abuse.** Caller-supplied input (queries, regex, uploads) must not
   exhaust memory/CPU or run unbounded.
5. **Secret leakage.** Secrets must never land in the database, an API response,
   or a shipped log.

## The load-bearing invariants

These are enforced and must stay true (also in `CLAUDE.md`):

- **No SQL outside `storage/`.** All access control is enforced in one place. A
  rogue query elsewhere could bypass `_role_filter`.
- **Retrieval methods take `role` keyword-only with no default.** A forgotten
  call site is a `TypeError`, not a silent "return everything." Fail closed.
  Same for `HubDeps.role`.
- **Role rank is defined exactly once** (`roles.py`). No copy-pasted comparisons
  to drift out of sync.
- **Tool output is framed `⟦untrusted document data⟧…⟦end⟧`**, with the glyphs
  and the no-sources sentinel neutralized inside content. This is the
  prompt-injection boundary the system prompt relies on.
- **Secrets are env-only** — never stored in or returned from the DB; the
  auto-generated setup token goes to stderr, not the logger.
- **`HIPPO_SOURCE_ROOTS` gates every filesystem mount, in every mode**, and is
  re-checked on re-sync.

## Access control, concretely

- **Reads** are filtered in SQL by `_role_filter` (via `readable_min_roles`) in
  every retrieval method.
- **Writes** (`/ingest`) are gated by `can_write(role, folder.min_role,
  folder.origin)`.
- **Folder management** layers `require_folder_tier` over the `require_admin`
  floor — an admin can't touch an owner-tier folder (a move rewrites the
  subtree's tier, which would otherwise leak owner docs downward).
- **Effective-role guards** in create-user, role-change, password-reset, and
  cross-user token-revoke compare against the *effective* role, because a
  `HIPPO_ADMIN_EMAILS` email always resolves to owner. This closes the
  bootstrap-admin escalation class.
- **Anti-lockout:** you can't lower your own role, and switching `auth_mode`
  requires a valid credential in the target mode.

See [Auth & RBAC](auth-and-rbac.md) for the mechanics.

## Prompt-injection posture

Retrieved document text is *data*, not instructions. The `⟦…⟧` boundary plus the
system prompt's "untrusted content" rule tell the model so. Crucially, even if a
document *did* slip an instruction past the model, **role filtering is
independent** — the model can only ever be handed content the caller may read, so
an injection can't widen access. Grounding is *detected and logged* (not
hard-enforced via retry) because a fabricated-but-resolvable citation is a
quality/trust issue, not an access hole. See [Agent](agent.md).

## Password & session hardening

argon2id hashing; 5-failures/15-minute lockout with counter decay; generic 401s
(no user enumeration); 7-day signed session cookies whose `Secure` flag follows
`HIPPO_PUBLIC_URL`'s scheme; failed-login telemetry on `hippo.auth`. Tokens store
only a sha256; the plaintext is shown once.

## Resource bounds

- Uploads: `HIPPO_MAX_UPLOAD_BYTES` (413) and `HIPPO_MAX_DOC_CHARS` (skip);
  `.docx` decompression capped (`HIPPO_MAX_DECOMPRESSED_BYTES`).
- Grep: `regex` module with a wall-clock timeout (ReDoS-safe), pattern length cap,
  whole-operation deadline, and a chunk-materialization cap that logs rather than
  silently truncates.
- Agent: a per-question tool-call budget (`HIPPO_MAX_TOOL_CALLS`).
- KNN: a single bounded over-fetch, no unbounded backoff loop.

## The known, deliberate caveat

**`none` mode is open pre-setup.** This is a dev convenience; `serve` warns when
bound beyond localhost. A secure first run uses `oidc`/`iap` env (IdP-gated even
before setup) or keeps the instance private until the wizard switches it to
`password`. Documented in [Production](../install/production.md) and
[Config & setup](config-and-setup.md).

## Audit trail

`hippo.audit` logs every privileged mutation (role/user/password/config/folder/
token changes), value-free; `hippo.auth` logs auth denials with sanitized emails.
Retain these in production.

## What's out of scope

Network-layer security (TLS, WAF), OS hardening, and IdP configuration are the
operator's responsibility — see the [Production checklist](../install/production.md).
There's no rate-limiting middleware in Hippo itself; put it at the proxy.
