"""Identity + user-management routes: /health, GET+PATCH /me, POST /me/password,
GET+POST /users, PUT /users/{email}/role, POST /users/{email}/password (admin reset).
All the effective-role tier guards (a HIPPO_ADMIN_EMAILS address resolves to owner)
are preserved verbatim from the original handlers."""

import logging
import secrets

from fastapi import Depends, HTTPException, Request

from ..auth import (AuthError, AuthenticatedUser, check_domain, hash_password,
                    safe_log, verify_password)
from ..roles import VALID_ROLES, rank
from .models import (MAX_NAME_LEN, MIN_PASSWORD_LEN, _EMAIL_RE, CreateUserIn,
                     ProfileIn, RoleIn)

audit = logging.getLogger("hippo.audit")


def register(app, ctx, auth) -> None:
    store, settings = ctx.store, ctx.settings
    verify_request = auth.verify_request
    require_admin = auth.require_admin

    @app.get("/health")
    async def health(_=Depends(verify_request)):
        return {"status": "ok"}

    @app.get("/me")
    async def me(user: AuthenticatedUser = Depends(verify_request)):
        prof = store.get_profile(user.email)
        return {"email": user.email, "role": user.role, "auth_mode": ctx.auth_mode,
                "name": prof["name"] if prof else ""}

    @app.patch("/me")
    async def update_me(body: ProfileIn, user: AuthenticatedUser = Depends(verify_request)):
        # Only the display name is self-editable; email is the login identity and
        # stays read-only here.
        name = body.name.strip()
        if len(name) > MAX_NAME_LEN:
            raise HTTPException(status_code=400,
                detail=f"name must be at most {MAX_NAME_LEN} characters")
        # ensure a row exists first (covers none-mode / not-yet-persisted identities)
        # so set_name actually persists and we don't report a name we didn't store.
        store.ensure_user(user.email)
        store.set_name(user.email, name)
        return {"email": user.email, "role": user.role, "auth_mode": ctx.auth_mode, "name": name}

    @app.get("/users")
    async def list_users(user: AuthenticatedUser = Depends(require_admin)):
        # Show the EFFECTIVE role: HIPPO_ADMIN_EMAILS always resolve to owner at
        # request time (resolve_role), so reflect that here rather than the (possibly
        # stale stored value) — otherwise the list misrepresents power.
        admins = settings.admin_email_list
        return [{"email": e, "role": "owner" if e in admins else r}
                for e, r in store.list_users()]

    @app.post("/users")
    async def create_user(body: CreateUserIn, user: AuthenticatedUser = Depends(require_admin)):
        target = body.email.strip().lower()
        if not _EMAIL_RE.match(target):
            raise HTTPException(status_code=400, detail="a valid email is required")
        if body.role not in VALID_ROLES:
            raise HTTPException(status_code=400,
                detail=f"invalid role {body.role!r}; expected one of {list(VALID_ROLES)}")
        # Effective-role guard: a HIPPO_ADMIN_EMAILS address is force-promoted to owner
        # by resolve_role on every login, so creating ANY credential for it effectively
        # creates an owner. Compare the EFFECTIVE role against the caller's tier — else a
        # rank-1 admin could mint an admin-labelled login for a bootstrap-owner email and
        # then authenticate as owner (same hole that admin_reset_password already guards).
        effective_role = "owner" if target in settings.admin_email_list else body.role
        if rank(effective_role) > rank(user.role):
            raise HTTPException(status_code=403, detail="cannot create a user above your tier")
        if len(body.name.strip()) > MAX_NAME_LEN:
            raise HTTPException(status_code=400,
                detail=f"name must be at most {MAX_NAME_LEN} characters")
        # Domain gate: don't pre-create rows / mint passwords for accounts that
        # resolve_role would refuse at login anyway (leaves a usable-looking but dead
        # credential otherwise).
        try:
            check_domain(target, ctx.cfg.get("allowed_domain"))
        except AuthError as e:
            raise HTTPException(status_code=400, detail=str(e))
        # Echo the EFFECTIVE role (a HIPPO_ADMIN_EMAILS target resolves to owner), so the
        # create response can't disagree with what the next /users list shows (INF-03).
        resp: dict = {"email": target, "role": effective_role}
        # Atomic insert-only create: race-safe duplicate detection (409), no overwrite.
        if ctx.auth_mode == "password":
            new_pw = secrets.token_urlsafe(12)   # >= MIN_PASSWORD_LEN; shown once
            created = store.create_user(target, role=body.role, password_hash=hash_password(new_pw))
            if created:
                resp["password"] = new_pw
        else:
            # oidc/iap: the user becomes real on first sign-in; just pre-set the role
            created = store.create_user(target, role=body.role)
        if not created:
            raise HTTPException(status_code=409, detail="a user with that email already exists")
        if body.name.strip():
            store.set_name(target, body.name.strip())
        audit.info("user created: %s created %s (role=%s)",
                   safe_log(user.email), safe_log(target), effective_role)
        return resp

    @app.put("/users/{email}/role")
    async def set_user_role(email: str, body: RoleIn,
                            user: AuthenticatedUser = Depends(require_admin)):
        target = email.strip().lower()
        if body.role not in VALID_ROLES:
            raise HTTPException(status_code=400,
                detail=f"invalid role {body.role!r}; expected one of {list(VALID_ROLES)}")
        if rank(body.role) > rank(user.role):
            raise HTTPException(status_code=403,
                detail="you cannot grant a role above your own")
        if target == user.email and rank(body.role) < rank(user.role):
            raise HTTPException(status_code=400, detail="you can't lower your own role")
        # A HIPPO_ADMIN_EMAILS user is force-promoted by resolve_role every request,
        # so demoting them here is a no-op that would make /users lie. Refuse it.
        if target in settings.admin_email_list and body.role != "owner":
            raise HTTPException(status_code=400,
                detail="this user is a bootstrap admin (HIPPO_ADMIN_EMAILS); "
                       "remove them from that env var to change their role")
        store.set_role(target, body.role)
        audit.info("role change: %s set %s -> %s",
                   safe_log(user.email), safe_log(target), body.role)
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
        audit.info("password changed (self): %s", safe_log(user.email))
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
        # Strictly-greater: an admin may reset a SAME-tier peer admin's password by design
        # (admins are mutually trusted at their tier). The guard only blocks reaching ABOVE
        # one's tier (e.g. a rank-1 admin resetting a bootstrap owner) (INF-01).
        if rank(target_role) > rank(user.role):
            raise HTTPException(status_code=403, detail="cannot reset a user above your tier")
        new_pw = secrets.token_urlsafe(12)   # >= MIN_PASSWORD_LEN; shown once
        store.set_password(target, hash_password(new_pw))
        audit.info("password reset: %s reset %s", safe_log(user.email), safe_log(target))
        return {"email": target, "password": new_pw}
