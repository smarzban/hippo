"""build_app — the thin assembler. Builds the dependency context, creates the
FastAPI app, mounts session middleware, registers the four route groups, then
mounts the MCP app and the SPA static fallback (which must be registered LAST so
real API routes win). Everything substantive lives in the context/auth/route
modules; this file just wires them together (MED-04)."""

import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import JSONResponse

from ..auth import AuthError
from ..config import Settings
from ..mcp_server import _mcp_role
from .auth import make_auth_deps
from .context import build_context
from . import routes_account, routes_admin, routes_content, routes_session

log = logging.getLogger("hippo.auth")


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


def build_app(settings: Settings | None = None, model_override=None, *,
              iap_verifier=None, code_exchanger=None, google_key_fetcher=None) -> FastAPI:
    settings = settings or Settings()
    # Ensure hippo's logs survive a direct ASGI launch (uvicorn 'hippo.api:build_app'
    # --factory, gunicorn, etc.), not only `hippo serve` (LOW-21). Idempotent: add a
    # handler only if neither the 'hippo' logger nor the root has one — so running via
    # `hippo serve` (which calls logging.basicConfig on root) doesn't double-log.
    _hippo_log = logging.getLogger("hippo")
    if not _hippo_log.handlers and not logging.getLogger().handlers:
        _h = logging.StreamHandler()
        _h.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
        _hippo_log.addHandler(_h)
        _hippo_log.setLevel(logging.INFO)

    ctx = build_context(settings, model_override=model_override, iap_verifier=iap_verifier,
                        code_exchanger=code_exchanger, google_key_fetcher=google_key_fetcher)

    app = FastAPI(title="Hippo", lifespan=ctx.lifespan)
    app.state.store = ctx.store
    # No CORS middleware: the React UI reaches the API same-origin through the Vite
    # dev-server proxy (dev) or is served by the same origin (prod), so cross-origin
    # access is never needed. A permissive allow_origins=["*"] would let any website
    # read /documents, /sources, etc. cross-origin even though verify_request now
    # enforces auth in iap/oidc modes — the browser's same-origin policy is an
    # independent defence layer worth keeping.

    # SessionMiddleware is added ONCE, up front, whenever a secret_key is set —
    # not per auth-mode block. oidc/password sessions need it, and so does the
    # first-run wizard's password auto-login (which runs while the pre-setup env
    # mode may be `none`/`iap` but a secret_key is present). For none/iap with a
    # secret_key, an unused session cookie is mounted — harmless. Starlette applies
    # middleware in reverse-add order; a single up-front add is fine.
    if settings.secret_key:
        # The session cookie's Secure flag follows public_url's scheme. Behind a
        # TLS-terminating proxy that forwards plain HTTP, set HIPPO_PUBLIC_URL to the
        # external https:// base (also required for oidc) so the cookie is Secure and
        # can't leak over the internal hop (LOW-37); the http default is dev-only.
        # max_age sets the documented 7-day session lifetime explicitly (without it
        # Starlette defaults to ~14 days).
        app.add_middleware(SessionMiddleware, secret_key=settings.secret_key,
                           https_only=ctx.public_url.startswith("https"),
                           same_site="lax", max_age=7 * 24 * 60 * 60)

    auth = make_auth_deps(ctx)
    routes_session.register(app, ctx, auth)
    routes_account.register(app, ctx, auth)
    routes_content.register(app, ctx, auth)
    routes_admin.register(app, ctx, auth)

    if ctx.mcp_server_obj is not None:
        app.mount("/mcp", _McpBearerAuth(
            ctx.mcp_server_obj.streamable_http_app(), ctx.store, auth.email_to_role))

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
