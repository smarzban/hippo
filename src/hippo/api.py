from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel
from pydantic_ai.ui.vercel_ai import VercelAIAdapter
from pydantic_ai.usage import UsageLimits

from .agent import HubDeps, build_agent
from .config import Settings
from .db import connect
from .embeddings import build_embedder
from .enrich import Enricher
from .ingest import Ingestor, sync_folder
from .storage import Storage


async def verify_request(request: Request) -> None:
    """Auth stub. v1 is local/single-user; team deployment implements this one
    function (e.g. check an API key header) instead of retrofitting routes."""
    return None


class SourceIn(BaseModel):
    kind: str = "folder"
    location: str


def _usage_limits(settings: Settings) -> UsageLimits:
    """Cap the agent's *tool calls* (ADR D9's ~15 research budget). request_limit
    bounds model requests, not tool calls — one request can emit several — so it
    only serves as a generous backstop here."""
    return UsageLimits(
        tool_calls_limit=settings.max_tool_calls,
        request_limit=settings.max_tool_calls + 5,
    )


def build_app(settings: Settings | None = None, model_override=None) -> FastAPI:
    settings = settings or Settings()
    con = connect(settings.db_path, embedding_dim=settings.embedding_dim)
    embedder = build_embedder(settings)
    store = Storage(con, embedder)
    enricher = Enricher(settings.enrich_model) if settings.enrich_enabled else None
    ingestor = Ingestor(
        store, max_chars=settings.chunk_max_chars,
        overlap_chars=settings.chunk_overlap_chars, enricher=enricher,
    )
    agent = build_agent(model_override or settings.chat_model)
    deps = HubDeps(store=store, role="admin")  # Task 9 makes this per-request

    app = FastAPI(title="Hippo")
    # No CORS middleware: the React UI reaches the API same-origin through the Vite
    # dev-server proxy, so cross-origin access is never needed. A permissive
    # allow_origins=["*"] would let any website you visit read /documents, /sources,
    # etc. from a localhost server that has no auth (verify_request is a stub) — an
    # info-leak with no benefit here. Real auth + an explicit origin policy land with
    # team deployment (see verify_request).

    @app.get("/health")
    async def health(_=Depends(verify_request)):
        return {"status": "ok"}

    @app.post("/chat")
    async def chat(request: Request, _=Depends(verify_request)):
        return await VercelAIAdapter.dispatch_request(
            request, agent=agent, deps=deps, usage_limits=_usage_limits(settings)
        )

    @app.post("/ingest")
    async def ingest(file: UploadFile, _=Depends(verify_request)):
        raw = (await file.read()).decode("utf-8", errors="replace")
        suffix = Path(file.filename or "upload.md").suffix or ".md"
        # Threadpool: ingestion blocks (embeddings + enrichment), and Enricher's
        # run_sync cannot run on the event loop thread.
        result = await run_in_threadpool(
            ingestor.ingest_text, file.filename or "upload.md", raw, suffix=suffix
        )
        if result.status == "failed":
            raise HTTPException(status_code=422, detail=result.error)
        return {"path": result.path, "status": result.status, "chunks": result.chunks}

    @app.get("/documents")
    async def documents(query: str | None = None, _=Depends(verify_request)):
        return [
            {"id": d.id, "path": d.path, "title": d.title, "summary": d.summary}
            for d in store.list_documents(query=query, role="admin")
        ]

    @app.get("/documents/{doc_id}")
    async def document(doc_id: int, _=Depends(verify_request)):
        doc = store.get_document(doc_id, role="admin")
        if doc is None:
            raise HTTPException(status_code=404, detail="document not found")
        return {"id": doc.id, "path": doc.path, "title": doc.title, "content": doc.content, "summary": doc.summary}

    @app.get("/sources")
    async def sources(_=Depends(verify_request)):
        return [{"id": i, "kind": k, "location": loc, "access": acc}
                for i, k, loc, acc in store.list_sources()]

    @app.post("/sources")
    async def add_source(body: SourceIn, _=Depends(verify_request)):
        folder = Path(body.location)
        if not folder.is_dir():
            raise HTTPException(status_code=400, detail=f"not a directory: {folder}")
        report = await run_in_threadpool(
            sync_folder, folder, store, max_chars=settings.chunk_max_chars,
            overlap_chars=settings.chunk_overlap_chars, enricher=enricher,
        )
        return {"report": {"added": report.added, "updated": report.updated,
                           "skipped": report.skipped, "removed": report.removed,
                           "failed": report.failed}}

    return app
