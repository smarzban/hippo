# Upgrading & maintenance

Hippo's state is one SQLite file. Upgrades are usually just "pull the new code
and restart" — the cases below are the exceptions worth knowing.

## Upgrading the code

- **Docker:** `docker compose up --build -d` (or `pull` first). The `hippo-data`
  volume — your knowledge base — is untouched by a rebuild.
- **Local:** `git pull && uv sync`, then restart `hippo serve`. Rebuild the UI
  (`cd ui && npm run build`) if you serve it via `HIPPO_UI_DIST`.

## Changing the embedding model or dimension

The embedding model and dimension define the vector space and the `chunk_vec`
table width. They are **fixed when the index is created** and stamped into the
database on first ingest. Hippo refuses to ingest with a different embedding
model than the one stamped, to avoid silently blending two incompatible vector
spaces.

To change them:

```bash
# set the new model/dim in the environment
HIPPO_EMBEDDING_MODEL=<new-model> HIPPO_EMBEDDING_DIM=<new-dim> uv run hippo reindex
```

`hippo reindex` re-embeds **every** chunk with the new model and rebuilds
`chunk_vec`. It embeds everything *before* destroying the old index, so a
mid-run failure (bad key, rate limit, wrong dimension) leaves the existing index
intact. Run it with **no concurrent sync/upload** — it aborts safely if it
detects the chunk set changed underneath it.

> These keys are env-only and not DB-overridable — see
> [Configuration](configuration.md#two-things-that-are-never-in-the-database) and
> [Embeddings internals](../technical/embeddings.md).

## The legacy-database guard

Hippo's role + folder model (introduced in the SP1 milestone) changed the schema
with **no migration path**. A database from before that change (one with a
`documents.source_id` column and no `folders` table) is **rejected on startup**
with a clear `recreate the database` error.

If you hit this: delete the old `.db` file and re-sync your content. There is no
in-place migration.

## Operational config changes (no restart for some)

Owners can change operational settings live via **Settings → System config** or
`PUT /config`:

- **`chat_model`** takes effect immediately (read live per request).
- `auth_mode`, `enrich_model`, `allowed_domain`, and the oidc/iap wiring take
  effect on the next `hippo serve` restart.

Switching `auth_mode` has an **anti-lockout guard**: you must already hold a
valid credential in the *target* mode (e.g. an owner password before switching to
`password`, or an owner email under the allowed domain before `oidc`/`iap`),
otherwise the switch is refused.

## Backups

```bash
hippo backup snapshot.db
```

A consistent single-file snapshot via SQLite `VACUUM INTO` — safe regardless of
WAL state. Restore by pointing `HIPPO_DB_PATH` at the snapshot (or copying it
into place) while the server is stopped.

## Health check

`GET /health` returns `{"status": "ok"}` for an authenticated caller and is a
convenient liveness probe (it still requires auth in non-`none` modes, so probe
it with a bearer token or expect a 401 that still proves the app is up).
