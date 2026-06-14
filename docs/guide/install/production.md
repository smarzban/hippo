# Production hardening checklist

Running Hippo for a real team on a network-reachable host. Work through this list
before exposing it.

## 1. Pick a real auth mode

Do **not** run `none` on anything network-reachable. Use `password`, `oidc`, or
`iap` (see [Auth setup](auth-setup.md)). Remember the first-run window: in `none`
the API is open even before setup, so for a public box start in `oidc`/`iap`
(IdP-gated pre-setup) or keep it private until the wizard sets `password`.

## 2. Set a strong session secret

```bash
HIPPO_SECRET_KEY=$(openssl rand -hex 32)
```

Required for `oidc` and `password`. Keep it stable (rotating it invalidates all
sessions) and out of version control. It is a secret — never stored in the DB.

## 3. Terminate TLS and set the public URL

Put Hippo behind a TLS-terminating reverse proxy (nginx, Caddy, a cloud LB) and
set:

```bash
HIPPO_PUBLIC_URL=https://hippo.example.com
```

This makes the session cookie `Secure` and forms the correct OAuth redirect URI.
Even if the proxy forwards plain HTTP internally, set the external `https://…`
base so the cookie can't leak over the internal hop.

There is **no CORS middleware** by design: the UI is same-origin (served by the
API or proxied), so the browser's same-origin policy is an extra defense layer —
don't add a permissive CORS config.

## 4. Lock down ingest sources

```bash
HIPPO_SOURCE_ROOTS=/srv/hippo/docs        # colon-separated allowlist
```

A filesystem folder mount can only sync from inside an allowlisted root, in
**every** auth mode. Without this set, folder mounts are disabled entirely. This
prevents an owner-tier caller (which is *every* caller in `none` mode) from
mounting `/`, `/etc`, `~/.ssh`, etc. and exfiltrating host files through chat or
grep. The allowlist is re-checked on every re-sync, so tightening it stops a
previously-mounted outside path from syncing again.

## 5. Set ingestion limits to taste

The defaults are sane (10 MB upload cap, 1M-char doc cap, `.docx` ZIP-bomb
guard). Tune `HIPPO_MAX_UPLOAD_BYTES`, `HIPPO_MAX_DOC_CHARS`, and
`HIPPO_MAX_DECOMPRESSED_BYTES` for your content. See [Configuration](configuration.md).

## 6. Logging and audit trail

Hippo logs to the `hippo` logger family. Two streams matter in production:

- **`hippo.auth`** — auth denials (bad token, locked account, domain rejected),
  with caller emails sanitized.
- **`hippo.audit`** — every privileged mutation (role changes, user creation,
  password resets, config changes, folder create/rename/delete/resync, token
  revokes), value-free.

`hippo serve` configures logging. If you launch the ASGI app directly (e.g.
`uvicorn hippo.api:build_app --factory`, gunicorn), Hippo attaches a default
handler to the `hippo` logger so these messages still surface. Route/retain
`hippo.audit` separately if your platform supports it.

> The first-run setup token, when auto-generated, is printed to **stderr (the
> console)** — never to the application logger — so it doesn't persist in a
> centralized log aggregator.

## 7. Embeddings are fixed at index time

Decide your `HIPPO_EMBEDDING_MODEL` / `HIPPO_EMBEDDING_DIM` before first ingest.
They're env-only and stamped into the DB on first write; changing them later
requires `hippo reindex`. See [Upgrading](upgrading.md) and
[Embeddings](../technical/embeddings.md).

## 8. Back up the one file

The whole brain is `HIPPO_DB_PATH`. Take consistent snapshots with:

```bash
hippo backup /backups/hippo-$(date +%F).db
```

This uses SQLite `VACUUM INTO`, so it's consistent regardless of WAL state — no
need to stop writes. Schedule it; copy snapshots off-host.

## 9. Integrations

- **MCP:** mounted at `/mcp`; each user authenticates with their own bearer
  token, so MCP access is role-filtered like chat. Disable with
  `HIPPO_MCP_ENABLED=false` if unused. See [Using MCP](../users/using-mcp.md).
- **Slack:** runs as a **separate process** (`hippo slack`) over Socket Mode (an
  outbound WebSocket) — no inbound endpoint, works behind IAP. Run it as its own
  container/service. See [Using Slack](../users/using-slack.md).

## Quick checklist

- [ ] `HIPPO_AUTH_MODE` is not `none`
- [ ] `HIPPO_SECRET_KEY` set (oidc/password) and stable
- [ ] TLS terminated; `HIPPO_PUBLIC_URL` is the external `https://…`
- [ ] `HIPPO_SOURCE_ROOTS` set if you mount any directories
- [ ] Embedding model/dim chosen before first ingest
- [ ] `hippo.audit` log retained; scheduled `hippo backup`
- [ ] Slack runs as its own process; MCP disabled if unused
