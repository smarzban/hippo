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
    )
    return store, ing


role_app = typer.Typer(help="Manage user roles (developer | manager | admin).")
app.add_typer(role_app, name="role")
token_app = typer.Typer(help="Personal access tokens for MCP/API clients.")
app.add_typer(token_app, name="token")


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


@app.command()
def sync(folder: str = typer.Argument(None), watch: bool = typer.Option(False, "--watch")):
    """Sync a folder (and register it), or re-sync all registered sources."""
    settings = Settings()
    store, ing = _store(settings)
    enricher = ing.enricher

    def run_all() -> None:
        folders = [Path(folder)] if folder else [Path(loc) for _, kind, loc, _access in store.list_sources(role="admin") if kind == "folder"]
        if not folders:
            typer.echo("no sources registered; run: hippo sync <folder>")
            raise typer.Exit(1)
        for f in folders:
            report = sync_folder(
                f, store, max_chars=settings.chunk_max_chars,
                overlap_chars=settings.chunk_overlap_chars, enricher=enricher,
            )
            typer.echo(f"{f}: {report.summary()}")

    run_all()
    if watch:
        from watchfiles import watch as fswatch

        targets = [folder] if folder else [loc for _, kind, loc, _access in store.list_sources(role="admin") if kind == "folder"]
        typer.echo(f"watching {targets} (ctrl-c to stop)")
        for _changes in fswatch(*targets):
            run_all()


@app.command()
def add(file: str):
    """Ingest a single file."""
    settings = Settings()
    _, ing = _store(settings)
    res = ing.ingest_file(Path(file), source_type="upload")
    typer.echo(f"{res.path}: {res.status} ({res.chunks} chunks)" + (f" error: {res.error}" if res.error else ""))
    if res.status == "failed":
        raise typer.Exit(1)


@app.command()
def search(query: str, top_k: int = 5):
    """Run a hybrid search directly (debugging aid)."""
    settings = Settings()
    store, _ = _store(settings)
    for hit in store.search_hybrid(query, top_k=top_k, role="admin"):
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
def serve(host: str = "127.0.0.1", port: int = 8000):
    """Run the API server."""
    import uvicorn

    from .api import build_app

    uvicorn.run(build_app(Settings()), host=host, port=port)


@app.command()
def eval(golden_file: str, top_k: int = 5):
    """Retrieval-quality eval: % of golden questions whose expected doc is in top-k."""
    import yaml

    settings = Settings()
    store, _ = _store(settings)
    cases = yaml.safe_load(Path(golden_file).read_text())
    hits = 0
    for case in cases:
        results = store.search_hybrid(case["question"], top_k=top_k, role="admin")
        found = any(case["expect_path"] in r.path for r in results)
        hits += found
        typer.echo(f"{'PASS' if found else 'MISS'}  {case['question']}")
    typer.echo(f"recall@{top_k}: {hits}/{len(cases)}")


if __name__ == "__main__":
    app()
