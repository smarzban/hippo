import contextlib
import hashlib
import logging
import re
import secrets
from pathlib import Path
from typing import Literal
from urllib.parse import urlencode

log = logging.getLogger("hippo.auth")

from fastapi import Depends, FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from pydantic_ai.ui.vercel_ai import VercelAIAdapter
from pydantic_ai.usage import UsageLimits
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import JSONResponse, RedirectResponse

from .agent import HubDeps, build_agent
from .mcp_server import _mcp_role, build_mcp_server
from .auth import AuthError, AuthenticatedUser, IapVerifier, check_domain, resolve_role, validate_google_id_token
from .config import Settings
from .db import connect
from .embeddings import build_embedder
from .enrich import Enricher
from .github import GitHubContentsClient, GitHubError
from .ingest import Ingestor, sync_folder
from .storage import Storage


_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]")


def _safe_filename(name: str) -> str:
    """Reduce an upload filename to a safe, URL-clean basename. Path-stripped by
    the caller; this removes query/fragment/space chars that would corrupt the
    GitHub Contents API URL or the repo path."""
    base = Path(name).name  # strip any path components
    cleaned = _SAFE_NAME.sub("_", base).strip("._") or "upload"
    return cleaned


class _McpBearerAuth:
    """Pure-ASGI gate for the mounted /mcp app: require a valid Hippo bearer token,
    resolve it to a role, and expose that role to the MCP tools via _mcp_role.
    Pure ASGI (not BaseHTTPMiddleware) so the contextvar propagates into the tool
    task and unauthenticated requests are rejected before MCP processing."""

    def __init__(self, app, store, resolve):
        self.app = app
        self.store = store
        self.resolve = resolve  # callable: email -> role (raises AuthError on domain failure)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers") or [])
        authz = headers.get(b"authorization", b"").decode("latin-1")
        role = None
        if authz.lower().startswith("bearer "):
            email = self.store.resolve_token(authz[7:].strip())
            if email:
                try:
                    role = self.resolve(email)        # shared: check_domain + role
                except AuthError:
                    role = None                        # out-of-domain token -> reject
        if role is None:
            return await JSONResponse(
                {"detail": "invalid or missing token"}, status_code=401)(scope, receive, send)
        tok = _mcp_role.set(role)
        try:
            await self.app(scope, receive, send)
        finally:
            _mcp_role.reset(tok)


class SourceIn(BaseModel):
    kind: str = "folder"
    location: str
    access: Literal["everyone", "managers"] | None = None


class RoleIn(BaseModel):
    role: str  # validated manually in the route handler so we return 400 (not 422)


class TokenIn(BaseModel):
    name: str = ""


def _usage_limits(settings: Settings) -> UsageLimits:
    """Cap the agent's *tool calls* (ADR D9's ~15 research budget). request_limit
    bounds model requests, not tool calls — one request can emit several — so it
    only serves as a generous backstop here."""
    return UsageLimits(
        tool_calls_limit=settings.max_tool_calls,
        request_limit=settings.max_tool_calls + 5,
    )


def _exchange_code_with_google(code: str, settings: Settings) -> dict:
    import httpx

    r = httpx.post("https://oauth2.googleapis.com/token", data={
        "code": code, "client_id": settings.oidc_client_id,
        "client_secret": settings.oidc_client_secret,
        "redirect_uri": f"{settings.public_url}/auth/callback",
        "grant_type": "authorization_code",
    }, timeout=10)
    r.raise_for_status()
    return r.json()


def build_app(settings: Settings | None = None, model_override=None, *,
              iap_verifier=None, code_exchanger=None, google_key_fetcher=None,
              github_factory=None) -> FastAPI:
    settings = settings or Settings()
    con = connect(settings.db_path, embedding_dim=settings.embedding_dim)
    embedder = build_embedder(settings)
    store = Storage(con, embedder)
    enricher = Enricher(settings.enrich_model) if settings.enrich_enabled else None
    ingestor = Ingestor(
        store, max_chars=settings.chunk_max_chars,
        overlap_chars=settings.chunk_overlap_chars, enricher=enricher,
        max_doc_chars=settings.max_doc_chars,
        max_decompressed_bytes=settings.max_decompressed_bytes,
    )
    agent = build_agent(model_override or settings.chat_model)

    github_factory = github_factory or (
        lambda repo: GitHubContentsClient(repo, settings.github_token, settings.github_branch))

    mcp_server_obj = build_mcp_server(store, require_auth=True) if settings.mcp_enabled else None
    lifespan = None
    if mcp_server_obj is not None:
        @contextlib.asynccontextmanager
        async def lifespan(_app):  # runs the MCP streamable-http session manager
            async with mcp_server_obj.session_manager.run():
                yield

    app = FastAPI(title="Hippo", lifespan=lifespan)
    app.state.store = store
    # No CORS middleware: the React UI reaches the API same-origin through the Vite
    # dev-server proxy (dev) or is served by the same origin (prod), so cross-origin
    # access is never needed. A permissive allow_origins=["*"] would let any website
    # read /documents, /sources, etc. cross-origin even though verify_request now
    # enforces auth in iap/oidc modes — the browser's same-origin policy is an
    # independent defence layer worth keeping.

    if settings.auth_mode == "iap" and iap_verifier is None and not settings.iap_audience:
        raise ValueError("HIPPO_IAP_AUDIENCE is required when HIPPO_AUTH_MODE=iap")
    iap = iap_verifier or (IapVerifier(settings.iap_audience) if settings.auth_mode == "iap" else None)

    def _email_to_role(email: str) -> str:
        """Canonical domain-check + role resolution. Raises AuthError on domain failure.
        Used by both the HTTP bearer path (_user_for) and the MCP ASGI middleware."""
        return resolve_role(store, settings, email)

    def _user_for(email: str) -> AuthenticatedUser:
        email = email.strip().lower()
        try:
            role = _email_to_role(email)
        except AuthError as e:
            log.warning("auth denied: domain not allowed for %s", email)
            raise HTTPException(status_code=403, detail=str(e))
        return AuthenticatedUser(email=email, role=role)

    async def verify_request(request: Request) -> AuthenticatedUser:
        # Bearer tokens are accepted in every mode (MCP/CLI clients, spec §1).
        authz = request.headers.get("authorization", "")
        if authz.lower().startswith("bearer "):
            email = store.resolve_token(authz[7:].strip())
            if email is None:
                log.warning("auth denied: invalid bearer token")
                raise HTTPException(status_code=401, detail="invalid token")
            return _user_for(email)
        if settings.auth_mode == "none":
            return AuthenticatedUser(email="local", role="admin")
        if settings.auth_mode == "iap":
            assertion = request.headers.get("x-goog-iap-jwt-assertion", "")
            if not assertion:
                log.warning("auth denied: missing IAP assertion")
                raise HTTPException(status_code=401, detail="missing IAP assertion")
            try:
                return _user_for(iap.verify(assertion))
            except AuthError as e:
                log.warning("auth denied (iap): %s", e)
                raise HTTPException(status_code=401, detail=str(e))
        email = request.session.get("email", "")  # oidc: session cookie (Task 10)
        if not email:
            log.warning("auth denied: no session")
            raise HTTPException(status_code=401, detail="not signed in")
        return _user_for(email)

    async def require_admin(user: AuthenticatedUser = Depends(verify_request)) -> AuthenticatedUser:
        if user.role != "admin":
            raise HTTPException(status_code=403, detail="admin only")
        return user

    if settings.auth_mode == "oidc":
        if not settings.secret_key:
            raise ValueError("HIPPO_SECRET_KEY is required when HIPPO_AUTH_MODE=oidc")
        app.add_middleware(SessionMiddleware, secret_key=settings.secret_key,
                           https_only=settings.public_url.startswith("https"),
                           same_site="lax")
        exchange = code_exchanger or _exchange_code_with_google

        @app.get("/auth/login")
        async def auth_login(request: Request):
            state = secrets.token_urlsafe(16)
            request.session["oauth_state"] = state
            params = {
                "client_id": settings.oidc_client_id,
                "redirect_uri": f"{settings.public_url}/auth/callback",
                "response_type": "code", "scope": "openid email", "state": state,
            }
            if settings.allowed_domain:
                params["hd"] = settings.allowed_domain  # UX hint; check_domain enforces
            return RedirectResponse("https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params))

        @app.get("/auth/callback")
        async def auth_callback(request: Request, code: str, state: str):
            if state != request.session.pop("oauth_state", None):
                raise HTTPException(status_code=400, detail="state mismatch")
            tokens = await run_in_threadpool(exchange, code, settings)
            try:
                email = validate_google_id_token(
                    tokens.get("id_token", ""), settings.oidc_client_id,
                    key_fetcher=google_key_fetcher,
                )
                check_domain(email, settings.allowed_domain)
            except AuthError as e:
                raise HTTPException(status_code=403, detail=str(e))
            store.ensure_user(email)
            request.session["email"] = email
            return RedirectResponse("/")

        @app.get("/auth/logout")
        async def auth_logout(request: Request):
            request.session.clear()
            return RedirectResponse("/")

    @app.get("/health")
    async def health(_=Depends(verify_request)):
        return {"status": "ok"}

    @app.get("/me")
    async def me(user: AuthenticatedUser = Depends(verify_request)):
        return {
            "email": user.email, "role": user.role, "auth_mode": settings.auth_mode,
            "upload": {
                "team_repo": bool(settings.github_token and settings.github_docs_repo),
                "managers_repo": bool(settings.github_token and settings.github_managers_repo)
                                 and user.role in ("manager", "admin"),
            },
        }

    @app.post("/chat")
    async def chat(request: Request, user: AuthenticatedUser = Depends(verify_request)):
        deps = HubDeps(store=store, role=user.role)
        return await VercelAIAdapter.dispatch_request(
            request, agent=agent, deps=deps, usage_limits=_usage_limits(settings)
        )

    @app.post("/ingest")
    async def ingest(request: Request, file: UploadFile, repo: str = Form("team"),
                     user: AuthenticatedUser = Depends(verify_request)):
        cl = request.headers.get("content-length")
        if cl and cl.isdigit() and int(cl) > settings.max_upload_bytes:
            raise HTTPException(status_code=413, detail="file too large")
        raw_bytes = await file.read()
        if len(raw_bytes) > settings.max_upload_bytes:
            raise HTTPException(status_code=413, detail="file too large")
        name = _safe_filename(file.filename or "upload.md")
        if repo == "managers" and user.role not in ("manager", "admin"):
            raise HTTPException(status_code=403, detail="managers repo requires the manager role")
        target = settings.github_managers_repo if repo == "managers" else settings.github_docs_repo
        if settings.github_token and target:
            text = raw_bytes.decode("utf-8", errors="replace")
            if settings.max_doc_chars and len(text) > settings.max_doc_chars:
                raise HTTPException(status_code=413, detail="document too large")
            gh = github_factory(target)
            # Content-hash-qualified path: mirrors ingest.py's L4 fix so two different
            # docs sharing a filename coexist instead of silently overwriting; an
            # identical re-upload converges on the same path (idempotent update).
            digest = hashlib.sha256(raw_bytes).hexdigest()[:8]
            repo_path = f"uploads/{digest}-{name}"
            try:
                sha = await run_in_threadpool(
                    gh.put_file, repo_path, raw_bytes,
                    f"hippo upload: {name} (by {user.email})")
            except GitHubError as e:
                raise HTTPException(status_code=502, detail=str(e))
            # The doc is now versioned in git; the next repo sync ingests it (spec §1).
            return {"status": "committed", "repo": target, "path": repo_path, "commit": sha}
        if repo == "managers":
            raise HTTPException(status_code=400, detail="managers repo is not configured")
        # No GitHub configured (personal mode): direct, unversioned ingestion.
        # Threadpool: ingestion blocks (embeddings + enrichment), and Enricher's
        # run_sync cannot run on the event loop thread.
        suffix = Path(name).suffix or ".md"
        result = await run_in_threadpool(ingestor.ingest_bytes, name, raw_bytes, suffix=suffix)
        if result.status == "failed":
            raise HTTPException(status_code=422, detail=result.error)
        return {"path": result.path, "status": result.status,
                "chunks": result.chunks, "versioned": False}

    @app.get("/documents")
    async def documents(query: str | None = None, user: AuthenticatedUser = Depends(verify_request)):
        return [
            {"id": d.id, "path": d.path, "title": d.title, "summary": d.summary}
            for d in store.list_documents(query=query, role=user.role)
        ]

    @app.get("/documents/{doc_id}")
    async def document(doc_id: int, user: AuthenticatedUser = Depends(verify_request)):
        doc = store.get_document(doc_id, role=user.role)
        if doc is None:
            raise HTTPException(status_code=404, detail="document not found")
        return {"id": doc.id, "path": doc.path, "title": doc.title, "content": doc.content, "summary": doc.summary}

    @app.get("/sources")
    async def sources(user: AuthenticatedUser = Depends(verify_request)):
        return [{"id": i, "kind": k, "location": loc, "access": acc}
                for i, k, loc, acc in store.list_sources(role=user.role)]

    @app.post("/sources")
    async def add_source(body: SourceIn, user: AuthenticatedUser = Depends(require_admin)):
        folder = Path(body.location).resolve()  # symlink/.. tricks must not escape the roots
        roots = settings.source_root_list
        if settings.auth_mode != "none" and not roots:
            raise HTTPException(status_code=403,
                detail="source registration is disabled: no HIPPO_SOURCE_ROOTS configured")
        if roots and not any(folder == r or r in folder.parents for r in roots):
            raise HTTPException(status_code=403, detail=f"{folder} is outside HIPPO_SOURCE_ROOTS")
        if not folder.is_dir():
            raise HTTPException(status_code=400, detail=f"not a directory: {folder}")
        report = await run_in_threadpool(
            sync_folder, folder, store, max_chars=settings.chunk_max_chars,
            overlap_chars=settings.chunk_overlap_chars, enricher=enricher, access=body.access,
            max_doc_chars=settings.max_doc_chars,
        )
        return {"report": {"added": report.added, "updated": report.updated,
                           "skipped": report.skipped, "removed": report.removed,
                           "failed": report.failed}}

    @app.delete("/sources/{source_id}")
    async def remove_source(source_id: int, user: AuthenticatedUser = Depends(require_admin)):
        if not store.delete_source(source_id):
            raise HTTPException(status_code=404, detail="source not found")
        return {"deleted": source_id}

    @app.post("/sources/{source_id}/resync")
    async def resync_source(source_id: int, user: AuthenticatedUser = Depends(require_admin)):
        match = next((s for s in store.list_sources(role="admin") if s[0] == source_id), None)
        if match is None:
            raise HTTPException(status_code=404, detail="source not found")
        location = match[2]  # (id, kind, location, access)
        report = await run_in_threadpool(
            sync_folder, Path(location), store, max_chars=settings.chunk_max_chars,
            overlap_chars=settings.chunk_overlap_chars, enricher=enricher,
            max_doc_chars=settings.max_doc_chars,
        )
        return {"report": {"added": report.added, "updated": report.updated,
                           "skipped": report.skipped, "removed": report.removed,
                           "failed": report.failed}}

    @app.get("/users")
    async def list_users(user: AuthenticatedUser = Depends(require_admin)):
        return [{"email": e, "role": r} for e, r in store.list_users()]

    @app.put("/users/{email}/role")
    async def set_user_role(email: str, body: RoleIn,
                            user: AuthenticatedUser = Depends(require_admin)):
        target = email.strip().lower()
        valid_roles = {"developer", "manager", "admin"}
        if body.role not in valid_roles:
            raise HTTPException(status_code=400,
                detail=f"invalid role {body.role!r}; expected one of {sorted(valid_roles)}")
        if target == user.email and body.role != "admin":
            raise HTTPException(status_code=400,
                detail="you can't remove your own admin role")
        store.set_role(target, body.role)
        return {"email": target, "role": body.role}

    @app.get("/tokens")
    async def list_tokens_route(all: bool = False,
                                user: AuthenticatedUser = Depends(verify_request)):
        if all:
            if user.role != "admin":
                raise HTTPException(status_code=403, detail="admin only")
            return [{"id": i, "email": e, "name": n, "created_at": c, "last_used_at": lu}
                    for i, e, n, c, lu in store.list_all_tokens()]
        return [{"id": i, "name": n, "created_at": c, "last_used_at": lu}
                for i, n, c, lu in store.list_tokens(user.email)]

    @app.post("/tokens")
    async def create_token_route(body: TokenIn, user: AuthenticatedUser = Depends(verify_request)):
        secret = store.create_token(user.email, body.name)   # tied to caller -> caller's role
        tok = store.list_tokens(user.email)[-1]               # newest row for the id
        return {"id": tok[0], "token": secret}

    @app.delete("/tokens/{token_id}")
    async def delete_token(token_id: int, user: AuthenticatedUser = Depends(verify_request)):
        ok = (store.revoke_token_any(token_id) if user.role == "admin"
              else store.revoke_token(token_id, user.email))
        if not ok:
            raise HTTPException(status_code=404, detail="token not found")
        return {"revoked": token_id}

    @app.get("/settings/status")
    async def settings_status(user: AuthenticatedUser = Depends(require_admin)):
        return {
            "auth_mode": settings.auth_mode,
            "chat_model": settings.chat_model,
            "embedding_model": settings.embedding_model,
            "repos": {
                "team": bool(settings.github_token and settings.github_docs_repo),
                "managers": bool(settings.github_token and settings.github_managers_repo),
            },
            "mcp_enabled": settings.mcp_enabled,
            "slack_enabled": settings.slack_enabled,
            "counts": {
                "documents": len(store.list_documents(role="admin")),
                "sources": len(store.list_sources(role="admin")),
                "users": len(store.list_users()),
            },
        }

    if mcp_server_obj is not None:
        app.mount("/mcp", _McpBearerAuth(mcp_server_obj.streamable_http_app(), store, _email_to_role))

    # Serve the built React UI (single-origin with the API) when configured.
    # API routes above take precedence; the catch-all only handles unmatched
    # (SPA) paths. The Vite dev server + proxy remains the dev workflow.
    if settings.ui_dist:
        dist = Path(settings.ui_dist)
        if dist.is_dir():
            assets = dist / "assets"
            if assets.is_dir():
                app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")
            index = dist / "index.html"

            RESERVED = ("auth", "chat", "ingest", "documents", "sources", "me",
                        "users", "tokens", "settings",
                        "health", "openapi.json", "docs", "redoc", "assets", "mcp")

            @app.get("/{full_path:path}")
            async def spa(full_path: str):
                first = full_path.split("/", 1)[0]
                if first in RESERVED:
                    raise HTTPException(status_code=404, detail="not found")
                return FileResponse(str(index))

    return app
