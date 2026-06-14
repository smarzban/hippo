"""Personal access token persistence (surrogate-keyed). Only the sha256 of a
token is stored; the plaintext is returned exactly once at mint time. Token
creation resolves the owner via `_user_id_for` from the users mixin."""

import hashlib
import secrets

from ._common import _norm_email


class _TokensMixin:
    # -- personal access tokens ---------------------------------------------

    def create_token_returning_id(self, email: str, name: str = "") -> tuple[int, str]:
        """Mint a bearer token tied to a user_id; return (id, plaintext). Only the
        sha256 is stored. The id is the insert's lastrowid (same statement)."""
        token = "hk_" + secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self._lock, self.con:
            uid = self._user_id_for(email)
            cur = self.con.execute(
                "INSERT INTO tokens(token_hash, user_id, name) VALUES (?,?,?)",
                (digest, uid, name),
            )
        return cur.lastrowid, token

    def create_token(self, email: str, name: str = "") -> str:
        return self.create_token_returning_id(email, name)[1]

    def resolve_token(self, token: str) -> str | None:
        """Return the owning user's email for a valid token, else None."""
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self._lock:
            row = self.con.execute(
                "SELECT u.email FROM tokens t JOIN users u ON u.id = t.user_id "
                "WHERE t.token_hash=?", (digest,)
            ).fetchone()
            if row:
                with self.con:
                    self.con.execute(
                        "UPDATE tokens SET last_used_at=datetime('now') WHERE token_hash=?",
                        (digest,),
                    )
        return row[0] if row else None

    def list_tokens(self, email: str) -> list[tuple[int, str, str, str | None]]:
        """(id, name, created_at, last_used_at) for all tokens belonging to email."""
        email = _norm_email(email)
        with self._lock:
            return list(self.con.execute(
                "SELECT t.id, t.name, t.created_at, t.last_used_at FROM tokens t "
                "JOIN users u ON u.id = t.user_id WHERE u.email=? ORDER BY t.id",
                (email,),
            ))

    def revoke_token(self, token_id: int, email: str) -> bool:
        """Delete the token matching both id and owner-email."""
        email = _norm_email(email)
        with self._lock, self.con:
            cur = self.con.execute(
                "DELETE FROM tokens WHERE id=? AND user_id=(SELECT id FROM users WHERE email=?)",
                (token_id, email),
            )
        return cur.rowcount > 0

    def list_all_tokens(self) -> list[tuple[int, str, str, str, str | None]]:
        """All users' tokens (admin view): (id, email, name, created_at, last_used_at)."""
        with self._lock:
            return list(self.con.execute(
                "SELECT t.id, u.email, t.name, t.created_at, t.last_used_at "
                "FROM tokens t JOIN users u ON u.id = t.user_id ORDER BY u.email, t.id"
            ))

    def revoke_token_any(self, token_id: int) -> bool:
        with self._lock, self.con:
            cur = self.con.execute("DELETE FROM tokens WHERE id = ?", (token_id,))
        return cur.rowcount > 0

    def token_owner(self, token_id: int) -> tuple[str, str] | None:
        """(owner email, stored owner role) for a token id, or None if no such token.
        Lets the API tier-check a cross-user revoke against the token owner's role."""
        with self._lock:
            row = self.con.execute(
                "SELECT u.email, u.role FROM tokens t JOIN users u ON u.id = t.user_id "
                "WHERE t.id=?",
                (token_id,),
            ).fetchone()
        return (row[0], row[1]) if row else None
