"""Operational routes: GET+PUT /config (owner-only operational overlay; secrets and
env-only keys rejected; auth-switch anti-lockout), the /tokens self-service +
admin endpoints, and GET /settings/status. Every config read is `cfg.get(...)` so
the DB overlay is reflected live."""

import logging

from fastapi import Depends, HTTPException, Query, Request

from ..auth import AuthenticatedUser, safe_log
from ..config import DB_OVERRIDABLE
from ..roles import rank
from .auth import validate_auth_switch
from .models import TokenIn

audit = logging.getLogger("hippo.audit")


def register(app, ctx, auth) -> None:
    store, settings, cfg = ctx.store, ctx.settings, ctx.cfg
    verify_request = auth.verify_request
    require_admin = auth.require_admin
    require_owner = auth.require_owner

    @app.get("/config")
    async def get_config(user: AuthenticatedUser = Depends(require_owner)):
        # effective value per key (DB override else env default); never a secret
        return {k: cfg.get(k) for k in sorted(DB_OVERRIDABLE)}

    @app.put("/config")
    async def put_config(request: Request, user: AuthenticatedUser = Depends(require_owner)):
        body = await request.json()
        for key in body:
            if key not in DB_OVERRIDABLE:
                # embedding_model/dim land here too: they are env-only (MED-07), so a
                # PUT that tries to set them is rejected as non-overridable.
                raise HTTPException(status_code=400,
                    detail=f"{key!r} is not a settable operational key (secrets/env-only keys are rejected)")
        if "auth_mode" in body:
            # consider oidc_client_id/iap_audience set in this SAME request when
            # validating prereqs for the target mode.
            validate_auth_switch(ctx, user, body["auth_mode"],
                oidc_client_id=body.get("oidc_client_id"),
                iap_audience=body.get("iap_audience"))
        for key, value in body.items():
            store.set_config(key, str(value))
        # Log which keys changed (not values — auth_mode/model names are operational,
        # but keep the audit line uniformly value-free as the rule for this logger).
        audit.info("config change: %s set %s", safe_log(user.email), sorted(body))
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
        # Self-revoke is always allowed (matches the caller's own user_id).
        if store.revoke_token(token_id, user.email):
            return {"revoked": token_id}
        # Not the caller's own token: only admins may revoke another user's token,
        # and never one owned by a user ABOVE their tier — mirroring the effective-role
        # guard in admin_reset_password (a HIPPO_ADMIN_EMAILS owner outranks an admin).
        # Without this, a rank-1 admin could DoS an owner's CLI/MCP automation.
        if rank(user.role) < 1:
            raise HTTPException(status_code=404, detail="token not found")
        owner = store.token_owner(token_id)
        if owner is None:
            raise HTTPException(status_code=404, detail="token not found")
        owner_email, owner_role = owner
        target_role = "owner" if owner_email in settings.admin_email_list else owner_role
        if rank(target_role) > rank(user.role):
            raise HTTPException(status_code=403,
                detail="cannot revoke a token owned by a user above your tier")
        if not store.revoke_token_any(token_id):
            raise HTTPException(status_code=404, detail="token not found")
        audit.info("token revoked: %s revoked token %s owned by %s",
                   safe_log(user.email), token_id, safe_log(owner_email))
        return {"revoked": token_id}

    @app.get("/settings/status")
    async def settings_status(user: AuthenticatedUser = Depends(require_admin)):
        return {
            "auth_mode": cfg.get("auth_mode"),
            "chat_model": cfg.get("chat_model"),
            "embedding_model": cfg.get("embedding_model"),   # env-only (not DB-overridable)
            "embedding_dim": settings.embedding_dim,         # env-only; reported for the UI
            "setup_complete": store.is_setup_complete(),
            "mcp_enabled": settings.mcp_enabled,
            "slack_enabled": settings.slack_enabled,
            "counts": {
                # count-only queries — don't materialize the whole corpus just to len() it (LOW-33)
                "documents": store.document_count(),
                "folders": store.folder_count(),
                "users": len(store.list_users()),
            },
        }
