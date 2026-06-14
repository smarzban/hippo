"""HTTP auth + authorization helpers, factored out of build_app so they are
importable and unit-testable without standing up the whole FastAPI app (MED-04).

`make_auth_deps(ctx)` builds the FastAPI dependency callables (verify_request /
require_admin / require_owner) plus the bound `email_to_role`/`user_for` the MCP
mount needs. The authorization predicates (folder tier, source-root allowlist,
mode prereqs, auth-switch anti-lockout) are plain functions taking `ctx` first.
Every effective-config read goes through `ctx.cfg.get(...)` so the DB overlay
keeps governing live, exactly as the original closures did."""

import logging
from pathlib import Path
from types import SimpleNamespace

from fastapi import Depends, HTTPException, Request

from ..auth import AuthError, AuthenticatedUser, resolve_role, safe_log, verify_password
from ..roles import rank

log = logging.getLogger("hippo.auth")


def email_to_role(ctx, email: str) -> str:
    """Canonical domain-check + role resolution. Raises AuthError on domain failure.
    Used by both the HTTP bearer path (user_for) and the MCP ASGI middleware.
    Passes the EFFECTIVE allowed_domain (DB overlay wins) so a domain set via the
    wizard / PUT /config actually gates role resolution live."""
    return resolve_role(ctx.store, ctx.settings, email,
                        allowed_domain=ctx.cfg.get("allowed_domain"))


def user_for(ctx, email: str) -> AuthenticatedUser:
    email = email.strip().lower()
    try:
        role = email_to_role(ctx, email)
    except AuthError as e:
        log.warning("auth denied: domain not allowed for %s", email)
        raise HTTPException(status_code=403, detail=str(e))
    return AuthenticatedUser(email=email, role=role)


def require_folder_tier(user: AuthenticatedUser, folder) -> None:
    """A folder may only be managed (renamed/moved/deleted/resynced/created-under)
    by a caller whose rank is at least the folder's tier — the same rule that
    gates reading it. require_admin only sets the rank≥1 floor; without this, an
    admin could move/delete/resync an owner-tier folder (and a move rewrites the
    whole subtree's tier, leaking owner-only docs down to everyone). Folder ids
    are guessable, so this must be enforced server-side, not by visibility."""
    if rank(user.role) < rank(folder.min_role):
        raise HTTPException(status_code=403,
            detail="cannot manage a folder above your tier")


def require_within_roots(ctx, location: str) -> Path:
    """Resolve a filesystem mount location and enforce the HIPPO_SOURCE_ROOTS
    allowlist. A non-empty allowlist is required in EVERY auth mode (including
    none) — without it an owner-tier caller (every caller, in none mode) could
    mount any host directory (/, /etc, ~/.ssh) and exfiltrate local files
    through chat/grep. Re-checked on resync too, so a path stops syncing once
    it falls outside a tightened allowlist. Returns the resolved path."""
    roots = ctx.settings.source_root_list
    if not roots:
        raise HTTPException(status_code=403,
            detail="folder mounts disabled: no HIPPO_SOURCE_ROOTS configured")
    p = Path(location).resolve()
    if not any(p == r or r in p.parents for r in roots):
        raise HTTPException(status_code=403, detail=f"{p} is outside HIPPO_SOURCE_ROOTS")
    return p


def require_mode_prereqs(ctx, target: str, *, oidc_client_id: str | None = None,
                         iap_audience: str | None = None) -> None:
    """Reject enabling a mode that would be unusable / brick the instance.
    Checks both env-only SECRETS and the EFFECTIVE (DB-overlay or same-request)
    non-secret prerequisites. oidc_client_id/iap_audience override the stored
    cfg value when supplied in the SAME request body (so /setup validates the
    effective-after-write value before persisting)."""
    settings, cfg = ctx.settings, ctx.cfg
    if target in ("password", "oidc") and not settings.secret_key:
        raise HTTPException(status_code=400,
            detail="HIPPO_SECRET_KEY (env) is required for this auth mode")
    if target == "oidc":
        if not settings.oidc_client_secret:
            raise HTTPException(status_code=400,
                detail="HIPPO_OIDC_CLIENT_SECRET (env) is required for oidc")
        cid = oidc_client_id if oidc_client_id is not None else cfg.get("oidc_client_id")
        if not cid:
            raise HTTPException(status_code=400,
                detail="oidc_client_id is required for oidc mode "
                       "(set it via /config or the setup wizard)")
    if target == "iap":
        aud = iap_audience if iap_audience is not None else cfg.get("iap_audience")
        if not aud:
            raise HTTPException(status_code=400,
                detail="iap_audience is required for iap mode "
                       "(set it via /config or the setup wizard)")


def validate_auth_switch(ctx, user: AuthenticatedUser, target: str,
                         *, oidc_client_id: str | None = None,
                         iap_audience: str | None = None) -> None:
    store, cfg, settings = ctx.store, ctx.cfg, ctx.settings
    if target not in ("password", "oidc", "iap"):
        raise HTTPException(status_code=400, detail="auth_mode must be password|oidc|iap")
    # the target mode's required SECRET env vars + effective prereqs must be present
    require_mode_prereqs(ctx, target, oidc_client_id=oidc_client_id, iap_audience=iap_audience)
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


def make_auth_deps(ctx) -> SimpleNamespace:
    """Build the FastAPI request dependencies that close over `ctx`. Returned as a
    namespace so route modules share one verify_request/require_admin/require_owner
    instance (FastAPI dedups identical Depends callables). Also exposes the bound
    email_to_role/user_for the MCP mount + login routes use."""

    async def verify_request(request: Request) -> AuthenticatedUser:
        store = ctx.store
        # Bearer tokens are accepted in every mode (MCP/CLI clients, spec §1).
        authz = request.headers.get("authorization", "")
        if authz.lower().startswith("bearer "):
            email = store.resolve_token(authz[7:].strip())
            if email is None:
                log.warning("auth denied: invalid bearer token")
                raise HTTPException(status_code=401, detail="invalid token")
            return user_for(ctx, email)
        if ctx.auth_mode == "none":
            return AuthenticatedUser(email="local", role="owner")
        if ctx.auth_mode == "iap":
            assertion = request.headers.get("x-goog-iap-jwt-assertion", "")
            if not assertion:
                log.warning("auth denied: missing IAP assertion")
                raise HTTPException(status_code=401, detail="missing IAP assertion")
            try:
                return user_for(ctx, ctx.iap.verify(assertion))
            except AuthError as e:
                log.warning("auth denied (iap): %s", e)
                raise HTTPException(status_code=401, detail=str(e))
        if ctx.auth_mode == "password":
            uid = request.session.get("user_id")
            if not uid:
                raise HTTPException(status_code=401, detail="not signed in")
            found = store.get_user_by_id(uid)
            if found is None:
                request.session.clear()
                raise HTTPException(status_code=401, detail="not signed in")
            email, _role = found
            return user_for(ctx, email)   # re-resolves role (bootstrap/admin_emails honored)
        email = request.session.get("email", "")  # oidc: session cookie (Task 10)
        if not email:
            log.warning("auth denied: no session")
            raise HTTPException(status_code=401, detail="not signed in")
        return user_for(ctx, email)

    async def require_admin(user: AuthenticatedUser = Depends(verify_request)) -> AuthenticatedUser:
        if rank(user.role) < 1:  # admin or owner
            raise HTTPException(status_code=403, detail="admin only")
        return user

    async def require_owner(user: AuthenticatedUser = Depends(verify_request)) -> AuthenticatedUser:
        if rank(user.role) < 2:
            raise HTTPException(status_code=403, detail="owner only")
        return user

    return SimpleNamespace(
        verify_request=verify_request,
        require_admin=require_admin,
        require_owner=require_owner,
        email_to_role=lambda email: email_to_role(ctx, email),
        user_for=lambda email: user_for(ctx, email),
    )
