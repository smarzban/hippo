"""Content routes: POST /chat (Vercel AI stream), POST /ingest (multi-destination
upload), GET /documents + /documents/{id}, and the /folders tree CRUD
(GET/POST/PATCH/DELETE/resync). Folder mutations layer the per-tier guard
(require_folder_tier) on top of the require_admin floor, and synced folders are
re-checked against the HIPPO_SOURCE_ROOTS allowlist on create and resync."""

import logging
from pathlib import Path

from fastapi import Depends, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from pydantic_ai.ui.vercel_ai import VercelAIAdapter

from ..agent import HubDeps, usage_limits
from ..auth import AuthenticatedUser, safe_log
from ..ingest import sync_folder
from ..roles import can_write, rank
from .auth import require_folder_tier, require_within_roots
from .models import FolderIn, FolderPatch, _safe_filename

audit = logging.getLogger("hippo.audit")


def register(app, ctx, auth) -> None:
    store, settings = ctx.store, ctx.settings
    verify_request = auth.verify_request
    require_admin = auth.require_admin

    @app.post("/chat")
    async def chat(request: Request, user: AuthenticatedUser = Depends(verify_request)):
        deps = HubDeps(store=store, role=user.role)
        return await VercelAIAdapter.dispatch_request(
            request, agent=ctx.live_agent(), deps=deps, usage_limits=usage_limits(settings)
        )

    @app.post("/ingest")
    async def ingest(request: Request, file: UploadFile,
                     folder_ids: list[int] = Form(...),
                     user: AuthenticatedUser = Depends(verify_request)):
        cl = request.headers.get("content-length")
        if cl and cl.isdigit() and int(cl) > settings.max_upload_bytes:
            raise HTTPException(status_code=413, detail="file too large")
        raw_bytes = await file.read()
        if len(raw_bytes) > settings.max_upload_bytes:
            raise HTTPException(status_code=413, detail="file too large")
        name = _safe_filename(file.filename or "upload.md")
        suffix = Path(name).suffix or ".md"
        targets = []
        for fid in folder_ids:
            f = store.get_folder(fid)
            if f is None:
                raise HTTPException(status_code=404, detail=f"folder {fid} not found")
            if not can_write(user.role, f.min_role, f.origin):
                raise HTTPException(status_code=403,
                    detail=f"cannot upload into {f.name!r} (tier or synced-folder lock)")
            targets.append(f)
        results = []
        for f in targets:
            prefix = store.folder_path(f.id)
            res = await run_in_threadpool(
                ctx.ingestor.ingest_bytes, name, raw_bytes,
                folder_id=f.id, path_prefix=prefix, suffix=suffix)
            if res.status == "failed":
                raise HTTPException(status_code=422, detail=res.error)
            results.append({"path": res.path, "chunks": res.chunks})
        # one document per destination folder. `versioned` is retained as a constant
        # False for API backward-compat (it was the now-removed upload-to-repo signal) —
        # dropping the key could KeyError an existing headless client.
        return {"status": "added", "paths": [r["path"] for r in results],
                "results": results, "versioned": False}

    @app.get("/documents")
    async def documents(query: str | None = None, user: AuthenticatedUser = Depends(verify_request)):
        return [
            {"id": d.id, "path": d.path, "title": d.title, "summary": d.summary}
            for d in store.list_document_meta(query=query, role=user.role)
        ]

    @app.get("/documents/{doc_id}")
    async def document(doc_id: int, user: AuthenticatedUser = Depends(verify_request)):
        doc = store.get_document(doc_id, role=user.role)
        if doc is None:
            raise HTTPException(status_code=404, detail="document not found")
        return {"id": doc.id, "path": doc.path, "title": doc.title, "content": doc.content, "summary": doc.summary}

    @app.get("/folders")
    async def folders(user: AuthenticatedUser = Depends(verify_request)):
        return [
            {"id": f.id, "parent_id": f.parent_id, "name": f.name, "tier": f.min_role,
             "origin": f.origin, "doc_count": f.doc_count,
             "writable": can_write(user.role, f.min_role, f.origin)}
            for f in store.list_folders(role=user.role)
        ]

    @app.post("/folders")
    async def create_folder(body: FolderIn, user: AuthenticatedUser = Depends(require_admin)):
        parent = store.get_folder(body.parent_id)
        if parent is None:
            raise HTTPException(status_code=404, detail="parent folder not found")
        if rank(user.role) < rank(parent.min_role):
            raise HTTPException(status_code=403, detail="cannot create a folder above your tier")
        try:
            if body.origin == "manual":
                fid = store.create_folder(parent_id=body.parent_id, name=body.name)
            else:  # "folder": mount a filesystem path, then sync it
                if not body.location:
                    raise HTTPException(status_code=400, detail="location required for synced origin")
                folder = require_within_roots(ctx, body.location)
                if not folder.is_dir():
                    raise HTTPException(status_code=400, detail=f"not a directory: {folder}")
                fid = store.create_folder(parent_id=body.parent_id, name=body.name,
                                          origin="folder", location=str(folder))
                await run_in_threadpool(
                    sync_folder, folder, store, parent_id=body.parent_id,
                    max_chars=settings.chunk_max_chars,
                    overlap_chars=settings.chunk_overlap_chars, enricher=ctx.enricher,
                    max_doc_chars=settings.max_doc_chars)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        f = store.get_folder(fid)
        audit.info("folder created: %s created %r (id=%s, origin=%s, tier=%s)",
                   safe_log(user.email), f.name, f.id, f.origin, f.min_role)
        return {"id": f.id, "name": f.name, "tier": f.min_role, "origin": f.origin}

    @app.patch("/folders/{folder_id}")
    async def patch_folder(folder_id: int, body: FolderPatch,
                           user: AuthenticatedUser = Depends(require_admin)):
        f = store.get_folder(folder_id)
        if f is None:
            raise HTTPException(status_code=404, detail="folder not found")
        require_folder_tier(user, f)  # can't rename/move a folder above your tier
        if body.parent_id is not None:
            dest = store.get_folder(body.parent_id)
            if dest is None:
                raise HTTPException(status_code=404, detail="parent folder not found")
            require_folder_tier(user, dest)  # nor move it into one above your tier
        try:
            if body.name is not None:
                store.rename_folder(folder_id, body.name)
            if body.parent_id is not None:
                store.move_folder(folder_id, body.parent_id)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        audit.info("folder updated: %s patched folder %s (name=%r, parent_id=%s)",
                   safe_log(user.email), folder_id, body.name, body.parent_id)
        return {"id": folder_id}

    @app.delete("/folders/{folder_id}")
    async def delete_folder(folder_id: int, user: AuthenticatedUser = Depends(require_admin)):
        f = store.get_folder(folder_id)
        if f is None:
            raise HTTPException(status_code=404, detail="folder not found")
        require_folder_tier(user, f)  # can't delete a folder above your tier
        try:
            ok = store.delete_folder(folder_id)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        if not ok:
            raise HTTPException(status_code=404, detail="folder not found")
        audit.info("folder deleted: %s deleted folder %s", safe_log(user.email), folder_id)
        return {"deleted": folder_id}

    @app.post("/folders/{folder_id}/resync")
    async def resync_folder(folder_id: int, user: AuthenticatedUser = Depends(require_admin)):
        f = store.get_folder(folder_id)
        if f is None:
            raise HTTPException(status_code=404, detail="folder not found")
        require_folder_tier(user, f)  # can't resync (and prune) a folder above your tier
        if f.origin != "folder" or not f.location:
            raise HTTPException(status_code=400, detail="only filesystem-synced folders resync")
        # Re-check the allowlist on the STORED location: a path mounted while
        # HIPPO_SOURCE_ROOTS was loose (or empty) must stop syncing once the
        # allowlist is tightened, rather than re-ingesting from outside it.
        loc = require_within_roots(ctx, f.location)
        if not loc.is_dir():
            raise HTTPException(status_code=400,
                detail=f"folder path is not currently a directory: {f.location}")
        report = await run_in_threadpool(
            sync_folder, loc, store, parent_id=f.parent_id,
            max_chars=settings.chunk_max_chars,
            overlap_chars=settings.chunk_overlap_chars, enricher=ctx.enricher,
            max_doc_chars=settings.max_doc_chars)
        audit.info("folder resynced: %s resynced folder %s (added=%s updated=%s removed=%s)",
                   safe_log(user.email), folder_id, report.added, report.updated, report.removed)
        return {"report": {"added": report.added, "updated": report.updated,
                           "skipped": report.skipped, "removed": report.removed,
                           "failed": report.failed}}
