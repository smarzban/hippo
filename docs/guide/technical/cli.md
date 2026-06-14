# CLI reference

The `hippo` command is a [Typer](https://typer.tiangolo.com/) app (`cli.py`). Run
it with `uv run hippo <command>` locally, or `docker compose exec hippo uv run
--no-sync hippo <command>` in a container.

> Remember the `OPENAI_*` env vars are read from the process environment — load
> `.env` first: `set -a; source .env; set +a`.

## Ingestion

```bash
hippo sync [FOLDER] [--watch]
```
Register and sync a filesystem folder. With no `FOLDER`, re-syncs all
already-synced folders. `--watch` re-syncs on change. Sync handles additions,
updates, and **deletions** (prunes docs whose files were removed), and respects
the `HIPPO_SOURCE_ROOTS` allowlist.

```bash
hippo add FILE
```
Ingest a single file.

## Search & maintenance

```bash
hippo search QUERY [--top-k 5]
```
Debug hybrid search from the CLI — prints the ranked hits (handy for checking
retrieval without the chat UI).

```bash
hippo reindex
```
Re-embed every chunk with the current embedder and rebuild `chunk_vec`. Run after
changing `HIPPO_EMBEDDING_MODEL`/`HIPPO_EMBEDDING_DIM`, with **no concurrent
sync/upload**. Safe: it embeds everything before swapping the index. See
[Embeddings](embeddings.md).

```bash
hippo backup DEST
```
Write a consistent single-file snapshot via SQLite `VACUUM INTO` (safe regardless
of WAL state).

```bash
hippo eval GOLDEN_FILE [--top-k 5]
```
Run the retrieval recall@k regression gate against a golden YAML (e.g.
`eval/golden.yaml`). See [Development](development.md).

## Serving

```bash
hippo serve [--host 127.0.0.1] [--port 8000]
```
Run the FastAPI app (API + UI when `HIPPO_UI_DIST` is set). Configures logging;
warns if `none` auth mode is bound beyond localhost.

```bash
hippo mcp
```
Run the MCP server over **stdio**, as owner, no token — for a local single-user
MCP client. See [Using MCP](../users/using-mcp.md).

```bash
hippo slack
```
Run the Slack bot over Socket Mode. Refuses to start unless
`HIPPO_SLACK_ENABLED=true` and the Slack tokens are set. See
[Using Slack](../users/using-slack.md).

## Roles

```bash
hippo role set EMAIL ROLE      # ROLE = user | admin | owner
hippo role list                # all users and their roles
```

## Tokens

```bash
hippo token create EMAIL       # mint a bearer token (prints hk_… once)
hippo token list EMAIL         # list a user's tokens (metadata only, never the secret)
hippo token revoke EMAIL ID    # revoke a token by id
```

## Users (password auth)

```bash
hippo user set-password EMAIL [--role ROLE]
```
The break-glass way to create or reset a local password (password mode). Prompts
twice (no echo). Re-run to reset a forgotten password or unlock a locked-out
account. `--role` is only applied when creating a new user; existing users keep
their role unless `--role` is given. This is how you bootstrap the first owner
outside the wizard:

```bash
hippo user set-password owner@example.com --role owner
```

## Notes

- All commands operate on the database at `HIPPO_DB_PATH`.
- Commands that read/write the index go through the same `Storage` interface as
  the server — no SQL lives in the CLI.
