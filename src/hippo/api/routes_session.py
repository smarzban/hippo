"""Authentication + first-run wizard routes: oidc login/callback/logout (oidc mode
only), password login/logout (whenever a secret_key is present), GET /auth/config,
and the GET /setup/status + POST /setup wizard. The mode-specific ValueError guards
that used to sit inline in build_app run here at registration time, preserving the
fail-fast-on-misconfiguration behavior."""

import logging
import secrets
from urllib.parse import urlencode

from fastapi import HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from starlette.responses import RedirectResponse

from ..auth import (AuthError, check_domain, hash_password, safe_log,
                    validate_google_id_token, verify_password)
from .auth import require_mode_prereqs, user_for
from .models import MIN_PASSWORD_LEN

log = logging.getLogger("hippo.auth")
audit = logging.getLogger("hippo.audit")


def _exchange_code_with_google(code: str, settings, *, client_id: str, public_url: str) -> dict:
    import httpx

    # client_id / public_url are the EFFECTIVE (DB-overlay-aware) values so the
    # token exchange matches the redirect_uri/client used at /auth/login. The
    # client_secret stays env-only (never DB-overridable).
    r = httpx.post("https://oauth2.googleapis.com/token", data={
        "code": code, "client_id": client_id,
        "client_secret": settings.oidc_client_secret,
        "redirect_uri": f"{public_url}/auth/callback",
        "grant_type": "authorization_code",
    }, timeout=10)
    r.raise_for_status()
    return r.json()


def register(app, ctx, auth) -> None:
    settings, store = ctx.settings, ctx.store
    auth_mode = ctx.auth_mode

    if auth_mode == "oidc":
        if not settings.secret_key:
            raise ValueError("HIPPO_SECRET_KEY is required when HIPPO_AUTH_MODE=oidc")
        # SessionMiddleware is added once up front (build_app) when secret_key is set.
        exchange = ctx.code_exchanger or _exchange_code_with_google

        @app.get("/auth/login")
        async def auth_login(request: Request):
            state = secrets.token_urlsafe(16)
            request.session["oauth_state"] = state
            params = {
                "client_id": ctx.oidc_client_id,
                "redirect_uri": f"{ctx.public_url}/auth/callback",
                "response_type": "code", "scope": "openid email", "state": state,
            }
            if ctx.allowed_domain:
                params["hd"] = ctx.allowed_domain  # UX hint; check_domain enforces
            return RedirectResponse("https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params))

        @app.get("/auth/callback")
        async def auth_callback(request: Request, code: str, state: str):
            if state != request.session.pop("oauth_state", None):
                raise HTTPException(status_code=400, detail="state mismatch")
            tokens = await run_in_threadpool(
                exchange, code, settings, client_id=ctx.oidc_client_id, public_url=ctx.public_url)
            try:
                email = validate_google_id_token(
                    tokens.get("id_token", ""), ctx.oidc_client_id,
                    key_fetcher=ctx.google_key_fetcher,
                )
                check_domain(email, ctx.allowed_domain)
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
            # Decay an elapsed lockout so the counter restarts (LOW-15): otherwise a
            # once-locked account re-locks on its very first post-expiry attempt.
            store.clear_lock_if_expired(email)
            creds = store.get_credentials(email)
            # Generic failure for missing user / no local password / bad password.
            generic = HTTPException(status_code=401, detail="invalid email or password")
            # Failed-login telemetry: password mode is the surface most exposed to
            # online brute-force/credential-stuffing, so an operator needs a signal
            # to alert on. email is caller-controlled — sanitize before logging.
            if store.is_locked(email):
                log.warning("auth denied: account locked %s", safe_log(email))
                raise HTTPException(status_code=401,
                    detail=f"account locked — try again in up to {store.LOCKOUT_MINUTES} minutes")
            if creds is None or not creds["password_hash"]:
                # still do nothing leak-y; no counter to bump on a non-user
                log.warning("auth denied: no local credential for %s", safe_log(email))
                raise generic
            if not verify_password(creds["password_hash"], password):
                store.record_failed_login(email)
                if store.is_locked(email):
                    log.warning("auth denied: account locked after repeated failures %s",
                                safe_log(email))
                else:
                    log.warning("auth denied: bad password for %s", safe_log(email))
                raise generic
            store.reset_login_state(email)
            request.session["user_id"] = creds["user_id"]
            u = user_for(ctx, email)
            return {"email": u.email, "role": u.role}

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
        if not secrets.compare_digest(str(body.get("token", "")), ctx.effective_setup_token):
            raise HTTPException(status_code=403, detail="invalid setup token")
        mode = body.get("auth_mode")
        if mode not in ("password", "oidc", "iap"):
            raise HTTPException(status_code=400, detail="auth_mode must be password|oidc|iap")
        owner_email = (body.get("owner_email") or "").strip().lower()
        if not owner_email:
            raise HTTPException(status_code=400, detail="owner_email is required")
        oidc = body.get("oidc") or {}
        models = body.get("models") or {}
        # validate the chosen mode's required SECRETS + effective prereqs are present.
        # For oidc/iap the client_id/audience come from the request body (they'll be
        # persisted below), so validate those body values now — you can't enable a
        # mode that would be unusable / brick on the next restart.
        require_mode_prereqs(ctx, mode, oidc_client_id=oidc.get("client_id"),
                             iap_audience=body.get("iap_audience"))
        # embedding_model/dim are env-only (the vector space + chunk_vec width are
        # fixed at table creation; changing them needs `hippo reindex`). The wizard
        # may still send them, but we never persist them to the overlay — the env-built
        # embedder is the single source of truth, so the config can never go stale (MED-07).
        # atomic claim: only the first concurrent request proceeds; a racing second
        # valid request loses here and gets a 409 (no double-owner creation).
        if not store.claim_setup():
            raise HTTPException(status_code=409, detail="setup already complete")
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
        # persist operational config (DB-overridable keys only). The setup_complete
        # flag was already set atomically by claim_setup() above.
        store.set_config("auth_mode", mode)
        # Only DB-overridable model keys are persisted. embedding_model/dim are
        # env-only (see MED-07) and deliberately NOT written, even if the wizard sent them.
        for k in ("chat_model", "enrich_model"):
            if k in models and models[k] not in (None, ""):
                store.set_config(k, str(models[k]))
        for k_body, k_cfg in (("client_id", "oidc_client_id"), ("public_url", "public_url")):
            if oidc.get(k_body):
                store.set_config(k_cfg, oidc[k_body])
        if body.get("iap_audience"):
            store.set_config("iap_audience", body["iap_audience"])
        if body.get("allowed_domain"):
            store.set_config("allowed_domain", body["allowed_domain"])
        # for password mode, log the owner in immediately
        if mode == "password":
            request.session["user_id"] = store.get_credentials(owner_email)["user_id"]
        audit.info("setup completed: owner=%s auth_mode=%s", safe_log(owner_email), mode)
        return {"ok": True, "auth_mode": mode}
