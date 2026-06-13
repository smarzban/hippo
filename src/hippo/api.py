import contextlib
import hashlib
import logging
import re
import secrets
from pathlib import Path
from typing import Literal
from urllib.parse import urlencode

log = logging.getLogger("hippo.auth")

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request, UploadFile
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
from .auth import (AuthError, AuthenticatedUser, IapVerifier, check_domain,
                   hash_password, resolve_role, validate_google_id_token, verify_password)
from .config import Settings
from .db import connect
from .embeddings import build_embedder
from .enrich import Enricher
from .github import GitHubContentsClient, GitHubError
from .ingest import Ingestor, sync_folder
from .roles import rank
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


class FolderIn(BaseModel):
    parent_id: int
    name: str
    origin: Literal["manual", "folder", "repo"] = "manual"
    location: str | None = None


class FolderPatch(BaseModel):
    name: str | None = None
    parent_id: int | None = None


class RoleIn(BaseModel):
    role: str  # validated manually in the route handler so we return 400 (not 422)


class TokenIn(BaseModel):
    name: str = ""


MIN_PASSWORD_LEN = 8


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

    # Operational config: a DB override (config table) wins over the env Settings
    # default, but ONLY for DB_OVERRIDABLE keys (secrets/env-only keys never come
    # from the DB). With an empty config table every cfg.get(...) returns the env
    # value, so these locals are identical to settings.X — no behavior change.
    # Resolved-at-construction (changes take effect on next restart): auth_mode and
    # the oidc/iap/domain wiring. chat_model is read LIVE per /chat request below.
    from .config import Config
    cfg = Config(settings, store)
    auth_mode = cfg.get("auth_mode")
    allowed_domain = cfg.get("allowed_domain")
    oidc_client_id = cfg.get("oidc_client_id")
    public_url = cfg.get("public_url")
    iap_audience = cfg.get("iap_audience")

    # First-run wizard gate. Use HIPPO_SETUP_TOKEN (env, never stored) if set; else
    # generate a random token and LOG it once at startup so the operator can read it
    # from the logs. Only generated while setup is incomplete (after that the wizard
    # is inert). Compared constant-time in POST /setup.
    effective_setup_token = settings.setup_token
    if not effective_setup_token and not store.is_setup_complete():
        effective_setup_token = secrets.token_urlsafe(24)
        log.warning("HIPPO_SETUP_TOKEN not set — first-run setup token is: %s",
                    effective_setup_token)

    enricher = Enricher(settings.enrich_model) if settings.enrich_enabled else None
    ingestor = Ingestor(
        store, max_chars=settings.chunk_max_chars,
        overlap_chars=settings.chunk_overlap_chars, enricher=enricher,
        max_doc_chars=settings.max_doc_chars,
        max_decompressed_bytes=settings.max_decompressed_bytes,
    )
    # chat_model is live (spec §3): rebuild the agent when the DB overlay changes it.
    default_model = model_override or cfg.get("chat_model")
    agent_cache = {"model": default_model, "agent": build_agent(default_model)}

    def _live_agent():
        m = model_override or cfg.get("chat_model")
        if m != agent_cache["model"]:
            agent_cache.update(model=m, agent=build_agent(m))
        return agent_cache["agent"]

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

    if auth_mode == "iap" and iap_verifier is None and not iap_audience:
        raise ValueError("HIPPO_IAP_AUDIENCE is required when HIPPO_AUTH_MODE=iap")
    iap = iap_verifier or (IapVerifier(iap_audience) if auth_mode == "iap" else None)

    # SessionMiddleware is added ONCE, up front, whenever a secret_key is set —
    # not per auth-mode block. oidc/password sessions need it, and so does the
    # first-run wizard's password auto-login (which runs while the pre-setup env
    # mode may be `none`/`iap` but a secret_key is present). For none/iap with a
    # secret_key, an unused session cookie is mounted — harmless. Starlette applies
    # middleware in reverse-add order; a single up-front add is fine.
    if settings.secret_key:
        app.add_middleware(SessionMiddleware, secret_key=settings.secret_key,
                           https_only=public_url.startswith("https"),
                           same_site="lax")

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
        if auth_mode == "none":
            return AuthenticatedUser(email="local", role="owner")
        if auth_mode == "iap":
            assertion = request.headers.get("x-goog-iap-jwt-assertion", "")
            if not assertion:
                log.warning("auth denied: missing IAP assertion")
                raise HTTPException(status_code=401, detail="missing IAP assertion")
            try:
                return _user_for(iap.verify(assertion))
            except AuthError as e:
                log.warning("auth denied (iap): %s", e)
                raise HTTPException(status_code=401, detail=str(e))
        if auth_mode == "password":
            uid = request.session.get("user_id")
            if not uid:
                raise HTTPException(status_code=401, detail="not signed in")
            found = store.get_user_by_id(uid)
            if found is None:
                request.session.clear()
                raise HTTPException(status_code=401, detail="not signed in")
            email, _role = found
            return _user_for(email)   # re-resolves role (bootstrap/admin_emails honored)
        email = request.session.get("email", "")  # oidc: session cookie (Task 10)
        if not email:
            log.warning("auth denied: no session")
            raise HTTPException(status_code=401, detail="not signed in")
        return _user_for(email)

    async def require_admin(user: AuthenticatedUser = Depends(verify_request)) -> AuthenticatedUser:
        if rank(user.role) < 1:  # admin or owner
            raise HTTPException(status_code=403, detail="admin only")
        return user

    async def require_owner(user: AuthenticatedUser = Depends(verify_request)) -> AuthenticatedUser:
        if rank(user.role) < 2:
            raise HTTPException(status_code=403, detail="owner only")
        return user

    def _require_folder_tier(user: AuthenticatedUser, folder) -> None:
        """A folder may only be managed (renamed/moved/deleted/resynced/created-under)
        by a caller whose rank is at least the folder's tier — the same rule that
        gates reading it. require_admin only sets the rank≥1 floor; without this, an
        admin could move/delete/resync an owner-tier folder (and a move rewrites the
        whole subtree's tier, leaking owner-only docs down to everyone). Folder ids
        are guessable, so this must be enforced server-side, not by visibility."""
        if rank(user.role) < rank(folder.min_role):
            raise HTTPException(status_code=403,
                detail="cannot manage a folder above your tier")

    if auth_mode == "oidc":
        if not settings.secret_key:
            raise ValueError("HIPPO_SECRET_KEY is required when HIPPO_AUTH_MODE=oidc")
        # SessionMiddleware is added once up front (see above) when secret_key is set.
        exchange = code_exchanger or _exchange_code_with_google

        @app.get("/auth/login")
        async def auth_login(request: Request):
            state = secrets.token_urlsafe(16)
            request.session["oauth_state"] = state
            params = {
                "client_id": oidc_client_id,
                "redirect_uri": f"{public_url}/auth/callback",
                "response_type": "code", "scope": "openid email", "state": state,
            }
            if allowed_domain:
                params["hd"] = allowed_domain  # UX hint; check_domain enforces
            return RedirectResponse("https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params))

        @app.get("/auth/callback")
        async def auth_callback(request: Request, code: str, state: str):
            if state != request.session.pop("oauth_state", None):
                raise HTTPException(status_code=400, detail="state mismatch")
            tokens = await run_in_threadpool(exchange, code, settings)
            try:
                email = validate_google_id_token(
                    tokens.get("id_token", ""), oidc_client_id,
                    key_fetcher=google_key_fetcher,
                )
                check_domain(email, allowed_domain)
            except AuthError as e:
                raise HTTPException(status_code=403, detail=str(e))
            store.ensure_user(email)
            request.session["email"] = email
            return RedirectResponse("/")

        @app.get("/auth/logout")
        async def auth_logout(request: Request):
            request.session.clear()
            return RedirectResponse("/")

    if auth_mode == "password" and not settings.secret_key:
        raise ValueError("HIPPO_SECRET_KEY is required when HIPPO_AUTH_MODE=password")
    # Register the password login/logout routes whenever a secret_key is present —
    # not only in construction-time password mode. This lets the first-run wizard
    # switch to password mode and have the owner log in immediately (the wizard sets
    # auth_mode=password in the DB overlay, which governs verify_request on the next
    # restart; until then verify_request follows the env mode, but the login route
    # must exist so the freshly created owner can authenticate). The route is inert
    # in none/iap modes (verify_request ignores the session there) and the
    # SessionMiddleware it needs is mounted once up front when secret_key is set.
    if auth_mode == "password" or settings.secret_key:

        @app.post("/auth/login")
        async def auth_login_password(request: Request):
            body = await request.json()
            email = (body.get("email") or "").strip().lower()
            password = body.get("password") or ""
            creds = store.get_credentials(email)
            # Generic failure for missing user / no local password / bad password.
            generic = HTTPException(status_code=401, detail="invalid email or password")
            if store.is_locked(email):
                raise HTTPException(status_code=401,
                    detail=f"account locked — try again in up to {store.LOCKOUT_MINUTES} minutes")
            if creds is None or not creds["password_hash"]:
                # still do nothing leak-y; no counter to bump on a non-user
                raise generic
            if not verify_password(creds["password_hash"], password):
                store.record_failed_login(email)
                raise generic
            store.reset_login_state(email)
            request.session["user_id"] = creds["user_id"]
            user = _user_for(email)
            return {"email": user.email, "role": user.role}

        @app.post("/auth/logout")
        async def auth_logout_password(request: Request):
            request.session.clear()
            return {"ok": True}

    @app.get("/auth/config")
    async def auth_config():
        return {"auth_mode": auth_mode}

    @app.get("/setup/status")
    async def setup_status():
        return {"setup_complete": store.is_setup_complete(),
                "auth_modes_available": ["password", "oidc", "iap"]}

    @app.post("/setup")
    async def run_setup(request: Request):
        if store.is_setup_complete():
            raise HTTPException(status_code=409, detail="setup already complete")
        body = await request.json()
        if not secrets.compare_digest(str(body.get("token", "")), effective_setup_token):
            raise HTTPException(status_code=403, detail="invalid setup token")
        mode = body.get("auth_mode")
        if mode not in ("password", "oidc", "iap"):
            raise HTTPException(status_code=400, detail="auth_mode must be password|oidc|iap")
        owner_email = (body.get("owner_email") or "").strip().lower()
        if not owner_email:
            raise HTTPException(status_code=400, detail="owner_email is required")
        # validate the chosen mode's required SECRET env vars are present (env-only)
        if mode in ("password", "oidc") and not settings.secret_key:
            raise HTTPException(status_code=400,
                detail="HIPPO_SECRET_KEY (env) is required for this auth mode")
        if mode == "oidc" and not settings.oidc_client_secret:
            raise HTTPException(status_code=400,
                detail="HIPPO_OIDC_CLIENT_SECRET (env) is required for oidc")
        # create the owner
        if mode == "password":
            pw = body.get("owner_password") or ""
            if len(pw) < MIN_PASSWORD_LEN:
                raise HTTPException(status_code=400,
                    detail=f"owner password must be at least {MIN_PASSWORD_LEN} characters")
            store.set_password(owner_email, hash_password(pw), role="owner")
        else:
            store.set_role(owner_email, "owner")  # becomes owner on first oidc/iap sign-in
        # name the three roots (rename the seeded folders)
        roots = body.get("roots") or {}
        for f in store.list_folders(role="owner"):
            if f.parent_id is None and f.min_role in roots and roots[f.min_role]:
                store.rename_folder(f.id, roots[f.min_role])
        # persist operational config (DB-overridable keys only)
        store.set_config("auth_mode", mode)
        models = body.get("models") or {}
        for k in ("chat_model", "enrich_model", "embedding_model", "embedding_dim"):
            if k in models and models[k] not in (None, ""):
                store.set_config(k, str(models[k]))
        oidc = body.get("oidc") or {}
        for k_body, k_cfg in (("client_id", "oidc_client_id"), ("public_url", "public_url")):
            if oidc.get(k_body):
                store.set_config(k_cfg, oidc[k_body])
        if body.get("iap_audience"):
            store.set_config("iap_audience", body["iap_audience"])
        if body.get("allowed_domain"):
            store.set_config("allowed_domain", body["allowed_domain"])
        store.mark_setup_complete()
        # for password mode, log the owner in immediately
        if mode == "password":
            request.session["user_id"] = store.get_credentials(owner_email)["user_id"]
        return {"ok": True, "auth_mode": mode}

    @app.get("/health")
    async def health(_=Depends(verify_request)):
        return {"status": "ok"}

    @app.get("/me")
    async def me(user: AuthenticatedUser = Depends(verify_request)):
        return {"email": user.email, "role": user.role, "auth_mode": auth_mode}

    @app.post("/chat")
    async def chat(request: Request, user: AuthenticatedUser = Depends(verify_request)):
        deps = HubDeps(store=store, role=user.role)
        return await VercelAIAdapter.dispatch_request(
            request, agent=_live_agent(), deps=deps, usage_limits=_usage_limits(settings)
        )

    @app.post("/ingest")
    async def ingest(request: Request, file: UploadFile,
                     folder_ids: list[int] = Form(...),
                     user: AuthenticatedUser = Depends(verify_request)):
        from .roles import can_write
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
                ingestor.ingest_bytes, name, raw_bytes,
                folder_id=f.id, path_prefix=prefix, suffix=suffix)
            if res.status == "failed":
                raise HTTPException(status_code=422, detail=res.error)
            results.append({"path": res.path, "chunks": res.chunks})
        # one document per destination folder
        return {"status": "added", "paths": [r["path"] for r in results],
                "results": results, "versioned": False}

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

    @app.get("/folders")
    async def folders(user: AuthenticatedUser = Depends(verify_request)):
        from .roles import can_write
        return [
            {"id": f.id, "parent_id": f.parent_id, "name": f.name, "tier": f.min_role,
             "origin": f.origin, "doc_count": f.doc_count,
             "writable": can_write(user.role, f.min_role, f.origin)}
            for f in store.list_folders(role=user.role)
        ]

    @app.post("/folders")
    async def create_folder(body: FolderIn, user: AuthenticatedUser = Depends(require_admin)):
        from .roles import rank as _rank
        parent = store.get_folder(body.parent_id)
        if parent is None:
            raise HTTPException(status_code=404, detail="parent folder not found")
        if _rank(user.role) < _rank(parent.min_role):
            raise HTTPException(status_code=403, detail="cannot create a folder above your tier")
        try:
            if body.origin == "manual":
                fid = store.create_folder(parent_id=body.parent_id, name=body.name)
            else:
                # mount a synced folder / repo, then sync it
                if not body.location:
                    raise HTTPException(status_code=400, detail="location required for synced origin")
                if body.origin == "folder":
                    folder = Path(body.location).resolve()
                    roots = settings.source_root_list
                    if auth_mode != "none" and not roots:
                        raise HTTPException(status_code=403,
                            detail="folder mounts disabled: no HIPPO_SOURCE_ROOTS configured")
                    if roots and not any(folder == r or r in folder.parents for r in roots):
                        raise HTTPException(status_code=403,
                            detail=f"{folder} is outside HIPPO_SOURCE_ROOTS")
                    if not folder.is_dir():
                        raise HTTPException(status_code=400, detail=f"not a directory: {folder}")
                    fid = store.create_folder(parent_id=body.parent_id, name=body.name,
                                              origin="folder", location=str(folder))
                    await run_in_threadpool(
                        sync_folder, folder, store, parent_id=body.parent_id,
                        max_chars=settings.chunk_max_chars,
                        overlap_chars=settings.chunk_overlap_chars, enricher=enricher,
                        max_doc_chars=settings.max_doc_chars)
                else:  # repo
                    fid = store.create_folder(parent_id=body.parent_id, name=body.name,
                                              origin="repo", location=body.location)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        f = store.get_folder(fid)
        return {"id": f.id, "name": f.name, "tier": f.min_role, "origin": f.origin}

    @app.patch("/folders/{folder_id}")
    async def patch_folder(folder_id: int, body: FolderPatch,
                           user: AuthenticatedUser = Depends(require_admin)):
        f = store.get_folder(folder_id)
        if f is None:
            raise HTTPException(status_code=404, detail="folder not found")
        _require_folder_tier(user, f)  # can't rename/move a folder above your tier
        if body.parent_id is not None:
            dest = store.get_folder(body.parent_id)
            if dest is None:
                raise HTTPException(status_code=404, detail="parent folder not found")
            _require_folder_tier(user, dest)  # nor move it into one above your tier
        try:
            if body.name is not None:
                store.rename_folder(folder_id, body.name)
            if body.parent_id is not None:
                store.move_folder(folder_id, body.parent_id)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"id": folder_id}

    @app.delete("/folders/{folder_id}")
    async def delete_folder(folder_id: int, user: AuthenticatedUser = Depends(require_admin)):
        f = store.get_folder(folder_id)
        if f is None:
            raise HTTPException(status_code=404, detail="folder not found")
        _require_folder_tier(user, f)  # can't delete a folder above your tier
        try:
            ok = store.delete_folder(folder_id)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        if not ok:
            raise HTTPException(status_code=404, detail="folder not found")
        return {"deleted": folder_id}

    @app.post("/folders/{folder_id}/resync")
    async def resync_folder(folder_id: int, user: AuthenticatedUser = Depends(require_admin)):
        f = store.get_folder(folder_id)
        if f is None:
            raise HTTPException(status_code=404, detail="folder not found")
        _require_folder_tier(user, f)  # can't resync (and prune) a folder above your tier
        if f.origin != "folder" or not f.location:
            raise HTTPException(status_code=400, detail="only filesystem-synced folders resync")
        if not Path(f.location).is_dir():
            raise HTTPException(status_code=400,
                detail=f"folder path is not currently a directory: {f.location}")
        report = await run_in_threadpool(
            sync_folder, Path(f.location), store, parent_id=f.parent_id,
            max_chars=settings.chunk_max_chars,
            overlap_chars=settings.chunk_overlap_chars, enricher=enricher,
            max_doc_chars=settings.max_doc_chars)
        return {"report": {"added": report.added, "updated": report.updated,
                           "skipped": report.skipped, "removed": report.removed,
                           "failed": report.failed}}

    @app.get("/users")
    async def list_users(user: AuthenticatedUser = Depends(require_admin)):
        # Show the EFFECTIVE role: HIPPO_ADMIN_EMAILS always resolve to owner at
        # request time (resolve_role), so reflect that here rather than the (possibly
        # stale stored value) — otherwise the list misrepresents power.
        admins = settings.admin_email_list
        return [{"email": e, "role": "owner" if e in admins else r}
                for e, r in store.list_users()]

    @app.put("/users/{email}/role")
    async def set_user_role(email: str, body: RoleIn,
                            user: AuthenticatedUser = Depends(require_admin)):
        from .roles import VALID_ROLES, rank as _rank
        target = email.strip().lower()
        if body.role not in VALID_ROLES:
            raise HTTPException(status_code=400,
                detail=f"invalid role {body.role!r}; expected one of {list(VALID_ROLES)}")
        if _rank(body.role) > _rank(user.role):
            raise HTTPException(status_code=403,
                detail="you cannot grant a role above your own")
        if target == user.email and _rank(body.role) < _rank(user.role):
            raise HTTPException(status_code=400, detail="you can't lower your own role")
        # A HIPPO_ADMIN_EMAILS user is force-promoted by resolve_role every request,
        # so demoting them here is a no-op that would make /users lie. Refuse it.
        if target in settings.admin_email_list and body.role != "owner":
            raise HTTPException(status_code=400,
                detail="this user is a bootstrap admin (HIPPO_ADMIN_EMAILS); "
                       "remove them from that env var to change their role")
        store.set_role(target, body.role)
        return {"email": target, "role": body.role}

    @app.post("/me/password")
    async def change_own_password(request: Request,
                                  user: AuthenticatedUser = Depends(verify_request)):
        body = await request.json()
        current = body.get("current") or ""
        new = body.get("new") or ""
        creds = store.get_credentials(user.email)
        if creds is None or not creds["password_hash"] or not verify_password(
                creds["password_hash"], current):
            raise HTTPException(status_code=403, detail="current password is incorrect")
        if len(new) < MIN_PASSWORD_LEN:
            raise HTTPException(status_code=400,
                detail=f"new password must be at least {MIN_PASSWORD_LEN} characters")
        store.set_password(user.email, hash_password(new))
        return {"ok": True}

    @app.post("/users/{email}/password")
    async def admin_reset_password(email: str,
                                   user: AuthenticatedUser = Depends(require_admin)):
        target = email.strip().lower()
        creds = store.get_credentials(target)
        if creds is None:
            raise HTTPException(status_code=404, detail="user not found")
        # Compare against the target's EFFECTIVE role: a HIPPO_ADMIN_EMAILS user is
        # force-promoted to owner at request time (resolve_role), so their stored
        # role may understate their power. Using the stored role would let a rank-1
        # admin reset (and hijack/lock out) a bootstrap owner's local credential.
        target_role = "owner" if target in settings.admin_email_list else creds["role"]
        if rank(target_role) > rank(user.role):
            raise HTTPException(status_code=403, detail="cannot reset a user above your tier")
        new_pw = secrets.token_urlsafe(12)   # >= MIN_PASSWORD_LEN; shown once
        store.set_password(target, hash_password(new_pw))
        return {"email": target, "password": new_pw}

    def _validate_auth_switch(user: AuthenticatedUser, target: str) -> None:
        if target not in ("password", "oidc", "iap"):
            raise HTTPException(status_code=400, detail="auth_mode must be password|oidc|iap")
        # the target mode's required SECRET env vars must be present (env-only)
        if target in ("password", "oidc") and not settings.secret_key:
            raise HTTPException(status_code=400,
                detail="HIPPO_SECRET_KEY (env) is required for the target auth mode")
        if target == "oidc" and not settings.oidc_client_secret:
            raise HTTPException(status_code=400,
                detail="HIPPO_OIDC_CLIENT_SECRET (env) is required for oidc")
        # anti-lockout: an owner must hold a valid credential in the TARGET mode
        owners = [e for e, r in store.list_users() if r == "owner"] + sorted(settings.admin_email_list)
        if target == "password":
            if not any((store.get_credentials(e) or {}).get("password_hash") for e in owners):
                raise HTTPException(status_code=400,
                    detail="set an owner password before switching to password mode "
                           "(anti-lockout) — use the break-glass CLI or an admin reset")
        else:  # oidc / iap: an owner email must satisfy the domain gate
            dom = cfg.get("allowed_domain")
            if dom and not any(e.endswith("@" + dom.lower()) for e in owners):
                raise HTTPException(status_code=400,
                    detail=f"no owner email under @{dom} — would lock out of {target} mode")

    @app.get("/config")
    async def get_config(user: AuthenticatedUser = Depends(require_owner)):
        from .config import DB_OVERRIDABLE
        # effective value per key (DB override else env default); never a secret
        return {k: cfg.get(k) for k in sorted(DB_OVERRIDABLE)}

    @app.put("/config")
    async def put_config(request: Request, user: AuthenticatedUser = Depends(require_owner)):
        from .config import DB_OVERRIDABLE
        body = await request.json()
        for key in body:
            if key not in DB_OVERRIDABLE:
                raise HTTPException(status_code=400,
                    detail=f"{key!r} is not a settable operational key (secrets/env-only keys are rejected)")
        # embedding model/dim cannot change once documents exist (chunk_vec dim is fixed)
        if ("embedding_model" in body or "embedding_dim" in body) and store.document_count() > 0:
            raise HTTPException(status_code=409,
                detail="embedding_model/embedding_dim can't change after documents exist — "
                       "run `hippo reindex` (CLI) to re-embed")
        if "auth_mode" in body:
            _validate_auth_switch(user, body["auth_mode"])
        for key, value in body.items():
            store.set_config(key, str(value))
        return {"ok": True}

    @app.get("/tokens")
    async def list_tokens_route(all_users: bool = Query(False, alias="all"),
                                user: AuthenticatedUser = Depends(verify_request)):
        if all_users:
            if rank(user.role) < 1:  # admin or owner
                raise HTTPException(status_code=403, detail="admin only")
            return [{"id": i, "email": e, "name": n, "created_at": c, "last_used_at": lu}
                    for i, e, n, c, lu in store.list_all_tokens()]
        return [{"id": i, "name": n, "created_at": c, "last_used_at": lu}
                for i, n, c, lu in store.list_tokens(user.email)]

    @app.post("/tokens")
    async def create_token_route(body: TokenIn, user: AuthenticatedUser = Depends(verify_request)):
        # Atomic id from the insert (not a follow-up query) — tied to the caller,
        # so the minted token carries the caller's role; no privilege escalation.
        token_id, secret = store.create_token_returning_id(user.email, body.name)
        return {"id": token_id, "token": secret}

    @app.delete("/tokens/{token_id}")
    async def delete_token(token_id: int, user: AuthenticatedUser = Depends(verify_request)):
        ok = (store.revoke_token_any(token_id) if rank(user.role) >= 1
              else store.revoke_token(token_id, user.email))
        if not ok:
            raise HTTPException(status_code=404, detail="token not found")
        return {"revoked": token_id}

    @app.get("/settings/status")
    async def settings_status(user: AuthenticatedUser = Depends(require_admin)):
        return {
            "auth_mode": auth_mode,
            "chat_model": settings.chat_model,
            "embedding_model": settings.embedding_model,
            "repos": {
                "team": bool(settings.github_token and settings.github_docs_repo),
                "managers": bool(settings.github_token and settings.github_managers_repo),
            },
            "mcp_enabled": settings.mcp_enabled,
            "slack_enabled": settings.slack_enabled,
            "counts": {
                "documents": len(store.list_documents(role="owner")),
                "folders": len(store.list_folders(role="owner")),
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

            RESERVED = ("auth", "chat", "ingest", "documents", "folders", "me",
                        "users", "tokens", "settings",
                        "health", "openapi.json", "docs", "redoc", "assets", "mcp")

            @app.get("/{full_path:path}")
            async def spa(full_path: str):
                first = full_path.split("/", 1)[0]
                if first in RESERVED:
                    raise HTTPException(status_code=404, detail="not found")
                return FileResponse(str(index))

    return app
