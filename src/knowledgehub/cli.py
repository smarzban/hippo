from pathlib import Path

import typer

from .config import Settings
from .db import connect
from .embeddings import build_embedder
from .enrich import Enricher
from .ingest import Ingestor, sync_folder
from .storage import Storage

app = typer.Typer(help="Knowledge hub: ingest documents, search, serve.")


def _store(settings: Settings) -> tuple[Storage, Ingestor]:
    con = connect(settings.db_path, embedding_dim=settings.embedding_dim)
    store = Storage(con, build_embedder(settings))
    enricher = Enricher(settings.enrich_model) if settings.enrich_enabled else None
    ing = Ingestor(
        store, max_chars=settings.chunk_max_chars,
        overlap_chars=settings.chunk_overlap_chars, enricher=enricher,
    )
    return store, ing


@app.command()
def sync(folder: str = typer.Argument(None), watch: bool = typer.Option(False, "--watch")):
    """Sync a folder (and register it), or re-sync all registered sources."""
    settings = Settings()
    store, ing = _store(settings)
    enricher = ing.enricher

    def run_all() -> None:
        folders = [Path(folder)] if folder else [Path(loc) for _, kind, loc in store.list_sources() if kind == "folder"]
        if not folders:
            typer.echo("no sources registered; run: hub sync <folder>")
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

        targets = [folder] if folder else [loc for _, kind, loc in store.list_sources() if kind == "folder"]
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
    for hit in store.search_hybrid(query, top_k=top_k):
        typer.echo(f"{hit.score:.4f}  {hit.path}  [{hit.heading_path}]")
        typer.echo(f"        {hit.text[:120]!r}")


@app.command()
def reindex():
    """Re-embed every chunk (after changing embedding model). Rebuilds chunk_vec."""
    settings = Settings()
    store, _ = _store(settings)
    con = store.con
    con.execute("DROP TABLE IF EXISTS chunk_vec")
    con.execute(f"CREATE VIRTUAL TABLE chunk_vec USING vec0(embedding float[{settings.embedding_dim}])")
    rows = list(con.execute("SELECT id, text FROM chunks ORDER BY id"))
    import sqlite_vec

    batch = 64
    for i in range(0, len(rows), batch):
        part = rows[i : i + batch]
        vecs = store.embedder.embed([t for _, t in part])
        with con:
            for (cid, _), v in zip(part, vecs):
                con.execute("INSERT INTO chunk_vec(rowid, embedding) VALUES (?,?)", (cid, sqlite_vec.serialize_float32(v)))
    with con:
        con.execute(
            "INSERT INTO meta(key, value) VALUES ('embedding_model', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (store.embedder.model,),
        )
    typer.echo(f"reindexed {len(rows)} chunks with {store.embedder.model}")


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
        results = store.search_hybrid(case["question"], top_k=top_k)
        found = any(case["expect_path"] in r.path for r in results)
        hits += found
        typer.echo(f"{'PASS' if found else 'MISS'}  {case['question']}")
    typer.echo(f"recall@{top_k}: {hits}/{len(cases)}")


if __name__ == "__main__":
    app()
