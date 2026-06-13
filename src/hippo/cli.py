from pathlib import Path

import typer

from .config import Settings
from .db import connect
from .embeddings import build_embedder
from .enrich import Enricher
from .ingest import Ingestor, sync_folder
from .storage import Storage

app = typer.Typer(help="Hippo: ingest documents, search, serve.")


def _store(settings: Settings) -> tuple[Storage, Ingestor]:
    con = connect(settings.db_path, embedding_dim=settings.embedding_dim)
    store = Storage(con, build_embedder(settings))
    enricher = Enricher(settings.enrich_model) if settings.enrich_enabled else None
    ing = Ingestor(
        store, max_chars=settings.chunk_max_chars,
        overlap_chars=settings.chunk_overlap_chars, enricher=enricher,
        max_doc_chars=settings.max_doc_chars,
        max_decompressed_bytes=settings.max_decompressed_bytes,
    )
    return store, ing


role_app = typer.Typer(help="Manage user roles (user | admin | owner).")
app.add_typer(role_app, name="role")
token_app = typer.Typer(help="Personal access tokens for MCP/API clients.")
app.add_typer(token_app, name="token")
user_app = typer.Typer(help="Local user accounts (password auth).")
app.add_typer(user_app, name="user")


@role_app.command("set")
def role_set(email: str, role: str):
    """Set a user's role (creates the user if new)."""
    store, _ = _store(Settings())
    try:
        store.set_role(email, role)
    except ValueError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1)
    typer.echo(f"{email}: {role}")


@role_app.command("list")
def role_list():
    """List all users and their roles."""
    store, _ = _store(Settings())
    for email, role in store.list_users():
        typer.echo(f"{role:10} {email}")


@token_app.command("create")
def token_create(email: str, name: str = typer.Option("", help="label, e.g. 'claude-code laptop'")):
    """Mint a bearer token tied to a user. Shown once; only its hash is stored."""
    store, _ = _store(Settings())
    typer.echo(store.create_token(email, name))
    typer.echo("save it now — it cannot be shown again", err=True)


@token_app.command("list")
def token_list(email: str):
    """List a user's tokens (id, name, created, last used) — never the secret."""
    store, _ = _store(Settings())
    rows = store.list_tokens(email)
    if not rows:
        typer.echo("no tokens")
        return
    for tid, name, created, last in rows:
        typer.echo(f"#{tid}  {name or '(unnamed)':20}  created {created}  last used {last or 'never'}")


@token_app.command("revoke")
def token_revoke(email: str, token_id: int):
    """Revoke (delete) one of a user's tokens by id."""
    store, _ = _store(Settings())
    if store.revoke_token(token_id, email):
        typer.echo(f"revoked token #{token_id} for {email}")
    else:
        typer.echo(f"no token #{token_id} for {email}", err=True)
        raise typer.Exit(1)


@user_app.command("set-password")
def user_set_password(
    email: str,
    role: str = typer.Option(None, help="role for a NEW user: user | admin | owner"),
):
    """Set (or reset) a local password for EMAIL. Prompts twice, no echo. Creates
    the user if absent. Break-glass bootstrap / locked-out-owner recovery."""
    from .auth import hash_password

    pw = typer.prompt("New password", hide_input=True, confirmation_prompt=True)
    if len(pw) < 8:
        typer.echo("password must be at least 8 characters", err=True)
        raise typer.Exit(1)
    store, _ = _store(Settings())
    try:
        store.set_password(email, hash_password(pw), role=role)
    except ValueError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1)
    typer.echo(f"password set for {email}" + (f" (role {role})" if role else ""))


@app.command()
def sync(folder: str = typer.Argument(None), watch: bool = typer.Option(False, "--watch")):
    """Sync a folder (and register it), or re-sync all registered sources."""
    settings = Settings()
    store, ing = _store(settings)
    enricher = ing.enricher

    def run_all() -> None:
        folders = ([Path(folder)] if folder
                   else [Path(f.location) for f in store.list_folders(role="owner")
                         if f.origin == "folder" and f.location])
        if not folders:
            typer.echo("no sources registered; run: hippo sync <folder>")
            raise typer.Exit(1)
        default_root = next(
            f.id for f in store.list_folders(role="owner")
            if f.parent_id is None and f.min_role == "user")
        for f in folders:
            report = sync_folder(
                f, store, parent_id=default_root, max_chars=settings.chunk_max_chars,
                overlap_chars=settings.chunk_overlap_chars, enricher=enricher,
                max_doc_chars=settings.max_doc_chars,
            )
            typer.echo(f"{f}: {report.summary()}")

    run_all()
    if watch:
        from watchfiles import watch as fswatch

        targets = ([folder] if folder
                   else [f.location for f in store.list_folders(role="owner")
                         if f.origin == "folder" and f.location])
        typer.echo(f"watching {targets} (ctrl-c to stop)")
        for _changes in fswatch(*targets):
            run_all()


@app.command()
def add(file: str):
    """Ingest a single file into the Default folder."""
    settings = Settings()
    store, ing = _store(settings)
    default_root = next(
        f.id for f in store.list_folders(role="owner")
        if f.parent_id is None and f.min_role == "user")
    res = ing.ingest_file(Path(file), source_type="upload", folder_id=default_root)
    typer.echo(f"{res.path}: {res.status} ({res.chunks} chunks)"
               + (f" error: {res.error}" if res.error else ""))
    if res.status == "failed":
        raise typer.Exit(1)


@app.command()
def search(query: str, top_k: int = 5):
    """Run a hybrid search directly (debugging aid)."""
    settings = Settings()
    store, _ = _store(settings)
    for hit in store.search_hybrid(query, top_k=top_k, role="owner"):
        typer.echo(f"{hit.score:.4f}  {hit.path}  [{hit.heading_path}]")
        typer.echo(f"        {hit.text[:120]!r}")


@app.command()
def reindex():
    """Re-embed every chunk (after changing embedding model). Rebuilds chunk_vec.

    Safe: embeds everything before swapping, so a failure leaves the old index intact."""
    settings = Settings()
    store, _ = _store(settings)
    n = store.reindex(settings.embedding_dim)
    typer.echo(f"reindexed {n} chunks with {store.embedder.model}")


@app.command()
def mcp():
    """Run an MCP server over stdio (local single-user; runs as admin).

    For remote/multi-user use, run `hippo serve` and connect to /mcp with a
    bearer token (`hippo token create <email>`)."""
    from .mcp_server import _mcp_role, build_mcp_server

    settings = Settings()
    store, _ = _store(settings)
    _mcp_role.set("owner")  # local owner
    build_mcp_server(store, require_auth=False).run(transport="stdio")


@app.command()
def slack():
    """Run the Slack bot over Socket Mode (read-only Q&A; requires HIPPO_SLACK_*)."""
    import asyncio

    from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

    from .agent import build_agent
    from .slack_bot import build_slack_app

    settings = Settings()
    if not settings.slack_enabled:
        typer.echo("Slack bot is disabled. Set HIPPO_SLACK_ENABLED=true to run it.", err=True)
        raise typer.Exit(code=1)
    if not settings.slack_bot_token or not settings.slack_app_token:
        typer.echo("Missing Slack tokens: set HIPPO_SLACK_BOT_TOKEN and "
                   "HIPPO_SLACK_APP_TOKEN.", err=True)
        raise typer.Exit(code=1)
    if not settings.allowed_domain:
        # The Slack bot is reachable by the whole workspace (incl. guests). Without
        # a domain gate, every Slack profile email is auto-provisioned as developer
        # and can query everyone-access docs. Strongly recommend setting it.
        typer.echo("WARNING: HIPPO_ALLOWED_DOMAIN is unset — every Slack workspace "
                   "user (including guests) can query Hippo. Set it to your work "
                   "domain (e.g. superbalist.com) to gate access.", err=True)

    con = connect(settings.db_path, embedding_dim=settings.embedding_dim)
    store = Storage(con, build_embedder(settings))
    agent = build_agent(settings.chat_model)
    slack_app = build_slack_app(store, agent, settings)

    async def _run():
        # AsyncSocketModeHandler opens an aiohttp session in its constructor, which
        # requires a running event loop — so build it inside asyncio.run, not before.
        handler = AsyncSocketModeHandler(slack_app, settings.slack_app_token)
        typer.echo("Hippo Slack bot connecting over Socket Mode…")
        await handler.start_async()

    asyncio.run(_run())


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8000):
    """Run the API server."""
    import logging
    import uvicorn

    from .api import build_app

    settings = Settings()
    if settings.auth_mode == "none" and host not in ("127.0.0.1", "localhost", "::1"):
        typer.secho(
            f"WARNING: serving in auth_mode=none on {host} — every request is an "
            f"implicit admin and source registration is unrestricted. Set "
            f"HIPPO_AUTH_MODE=oidc|iap before exposing Hippo beyond localhost.",
            fg=typer.colors.RED, err=True,
        )
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("hippo").info("serving on %s:%d (auth_mode=%s)", host, port, settings.auth_mode)
    uvicorn.run(build_app(settings), host=host, port=port)


@app.command()
def backup(dest: str):
    """Write a consistent snapshot of the database to DEST (VACUUM INTO)."""
    import sqlite3
    settings = Settings()
    store, _ = _store(settings)
    try:
        store.backup(dest)
    except sqlite3.Error as e:
        typer.echo(f"backup failed: {e} (does {dest} already exist?)", err=True)
        raise typer.Exit(1)
    typer.echo(f"backup written to {dest}")


@app.command()
def eval(golden_file: str, top_k: int = 5):
    """Retrieval-quality eval: % of golden questions whose expected doc is in top-k."""
    import yaml

    settings = Settings()
    store, _ = _store(settings)
    cases = yaml.safe_load(Path(golden_file).read_text())
    hits = 0
    for case in cases:
        results = store.search_hybrid(case["question"], top_k=top_k, role="owner")
        found = any(case["expect_path"] in r.path for r in results)
        hits += found
        typer.echo(f"{'PASS' if found else 'MISS'}  {case['question']}")
    typer.echo(f"recall@{top_k}: {hits}/{len(cases)}")


if __name__ == "__main__":
    app()
