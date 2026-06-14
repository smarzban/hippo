"""User + role + local-credential persistence: surrogate-id resolution, role
management, display name, password hashes, and login lockout state. The lockout
policy thresholds are class attributes so `Storage.LOCKOUT_MINUTES` keeps
resolving for callers that read them off the facade (e.g. the API login path)."""

from ..roles import DEFAULT_ROLE, VALID_ROLES
from ._common import _norm_email


class _UsersMixin:
    # -- users / roles -------------------------------------------------------

    LOCKOUT_MAX_FAILURES = 5
    LOCKOUT_MINUTES = 15

    def _user_id_for(self, email: str) -> int:
        """Resolve email → user_id, creating the user (DEFAULT_ROLE) on first sight.
        Caller holds the lock."""
        email = _norm_email(email)
        row = self.con.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
        if row:
            return row[0]
        cur = self.con.execute("INSERT INTO users(email) VALUES (?)", (email,))
        return cur.lastrowid

    def ensure_user(self, email: str) -> str:
        """Create on first sight with the default role; return the current role."""
        email = _norm_email(email)
        with self._lock:
            row = self.con.execute("SELECT role FROM users WHERE email=?", (email,)).fetchone()
            if row:
                return row[0]
            with self.con:
                self.con.execute("INSERT INTO users(email) VALUES (?)", (email,))
            return DEFAULT_ROLE

    def set_role(self, email: str, role: str) -> None:
        email = _norm_email(email)
        if role not in VALID_ROLES:
            raise ValueError(f"invalid role {role!r}; expected one of {VALID_ROLES}")
        with self._lock, self.con:
            self.con.execute(
                "INSERT INTO users(email, role) VALUES (?,?) "
                "ON CONFLICT(email) DO UPDATE SET role=excluded.role",
                (email, role),
            )

    def list_users(self) -> list[tuple[str, str]]:
        with self._lock:
            return list(self.con.execute("SELECT email, role FROM users ORDER BY email"))

    def get_profile(self, email: str) -> dict | None:
        """{email, name, role} for a user, or None. Used by /me and PATCH /me."""
        email = _norm_email(email)
        with self._lock:
            row = self.con.execute(
                "SELECT email, name, role FROM users WHERE email=?", (email,)).fetchone()
        return {"email": row[0], "name": row[1], "role": row[2]} if row else None

    def set_name(self, email: str, name: str) -> None:
        """Update a user's display name. No-op if the user does not exist."""
        email = _norm_email(email)
        with self._lock, self.con:
            self.con.execute("UPDATE users SET name=? WHERE email=?", (name, email))

    def set_password(self, email: str, password_hash: str, *, role: str | None = None) -> None:
        """Create-or-update a local credential. Creates the user (with `role` or
        the default) if absent; on an existing user updates the hash and (only if
        `role` is given) the role. Clears any lockout state. The caller hashes."""
        email = _norm_email(email)
        if role is not None and role not in VALID_ROLES:
            raise ValueError(f"invalid role {role!r}; expected one of {VALID_ROLES}")
        with self._lock, self.con:
            row = self.con.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
            if row is None:
                self.con.execute(
                    "INSERT INTO users(email, role, password_hash) VALUES (?,?,?)",
                    (email, role or DEFAULT_ROLE, password_hash),
                )
            elif role is not None:
                self.con.execute(
                    "UPDATE users SET password_hash=?, role=?, failed_logins=0, "
                    "locked_until=NULL WHERE id=?",
                    (password_hash, role, row[0]),
                )
            else:
                self.con.execute(
                    "UPDATE users SET password_hash=?, failed_logins=0, "
                    "locked_until=NULL WHERE id=?",
                    (password_hash, row[0]),
                )

    def create_user(self, email: str, *, role: str, password_hash: str | None = None) -> bool:
        """Atomically create a user, insert-only. Returns True iff THIS call created
        the row; False if the email already existed (no overwrite). Race-safe: the
        ON CONFLICT DO NOTHING + rowcount check happens inside the lock, so concurrent
        creates can't both succeed (callers map False -> 409)."""
        email = _norm_email(email)
        if role not in VALID_ROLES:
            raise ValueError(f"invalid role {role!r}; expected one of {VALID_ROLES}")
        with self._lock, self.con:
            cur = self.con.execute(
                "INSERT INTO users(email, role, password_hash) VALUES (?,?,?) "
                "ON CONFLICT(email) DO NOTHING",
                (email, role, password_hash),
            )
            return cur.rowcount > 0

    def get_credentials(self, email: str) -> dict | None:
        """Return {user_id, email, role, password_hash, failed_logins, locked_until}
        for an email, or None if no such user. Used only by the login path."""
        email = _norm_email(email)
        with self._lock:
            row = self.con.execute(
                "SELECT id, email, role, password_hash, failed_logins, locked_until "
                "FROM users WHERE email=?", (email,),
            ).fetchone()
        if row is None:
            return None
        return {"user_id": row[0], "email": row[1], "role": row[2],
                "password_hash": row[3], "failed_logins": row[4], "locked_until": row[5]}

    def get_user_by_id(self, user_id: int) -> tuple[str, str] | None:
        """(email, role) for a surrogate id, or None. Used by the session auth path."""
        with self._lock:
            row = self.con.execute(
                "SELECT email, role FROM users WHERE id=?", (user_id,)).fetchone()
        return (row[0], row[1]) if row else None

    def record_failed_login(self, email: str) -> None:
        """Increment the failure counter; lock for LOCKOUT_MINUTES once it reaches
        LOCKOUT_MAX_FAILURES. Lock timestamp is DB-clock based for testability."""
        email = _norm_email(email)
        with self._lock, self.con:
            self.con.execute(
                "UPDATE users SET failed_logins = failed_logins + 1 WHERE email=?", (email,))
            self.con.execute(
                f"UPDATE users SET locked_until = datetime('now', '+{self.LOCKOUT_MINUTES} minutes') "
                "WHERE email=? AND failed_logins >= ?",
                (email, self.LOCKOUT_MAX_FAILURES),
            )

    def reset_login_state(self, email: str) -> None:
        """Clear the failure counter + lock (called on a successful login)."""
        email = _norm_email(email)
        with self._lock, self.con:
            self.con.execute(
                "UPDATE users SET failed_logins=0, locked_until=NULL WHERE email=?", (email,))

    def is_locked(self, email: str) -> bool:
        """True iff the account is currently within its lockout window."""
        email = _norm_email(email)
        with self._lock:
            row = self.con.execute(
                "SELECT locked_until > datetime('now') FROM users WHERE email=?", (email,)
            ).fetchone()
        return bool(row and row[0])

    def clear_lock_if_expired(self, email: str) -> None:
        """If a lockout window has elapsed, reset the failure counter so the next
        attempt starts fresh. Without this the counter never decays: a once-locked
        account stays at failed_logins=5, so the very first post-expiry failure
        re-locks it immediately — effectively a permanent soft-lock (LOW-15)."""
        email = _norm_email(email)
        with self._lock, self.con:
            self.con.execute(
                "UPDATE users SET failed_logins=0, locked_until=NULL "
                "WHERE email=? AND locked_until IS NOT NULL AND locked_until <= datetime('now')",
                (email,),
            )
