"""Identity layer. Every mode converges on AuthenticatedUser(email, role); the
rest of the codebase never knows how the email was established (spec §1)."""

import time
from dataclasses import dataclass

import jwt


class AuthError(Exception):
    """Identity could not be established or is not allowed (-> 401/403)."""


@dataclass
class AuthenticatedUser:
    email: str
    role: str  # developer | manager | admin


def check_domain(email: str, allowed_domain: str) -> None:
    if allowed_domain and not email.lower().endswith("@" + allowed_domain.lower()):
        raise AuthError(f"only {allowed_domain} accounts are allowed")


class IapVerifier:
    """Verifies GCP Identity-Aware Proxy assertions (ES256 JWTs signed by Google).

    key_fetcher is injectable so tests supply a local key; production lazily
    fetches Google's JWKS once per process and caches it."""

    KEYS_URL = "https://www.gstatic.com/iap/verify/public_key-jwk"

    def __init__(self, audience: str, key_fetcher=None):
        self.audience = audience
        self._fetch = key_fetcher or self._fetch_google_keys
        self._keys: dict | None = None

    def _fetch_google_keys(self) -> dict:
        import httpx

        jwks = httpx.get(self.KEYS_URL, timeout=10).json()
        return {k["kid"]: jwt.PyJWK(k).key for k in jwks["keys"]}

    def verify(self, assertion: str) -> str:
        if self._keys is None:
            self._keys = self._fetch()
        try:
            kid = jwt.get_unverified_header(assertion).get("kid")
        except jwt.PyJWTError as e:
            raise AuthError(f"malformed IAP assertion: {e}") from e
        key = self._keys.get(kid)
        if key is None:
            raise AuthError("unknown IAP signing key")
        try:
            claims = jwt.decode(
                assertion, key=key, algorithms=["ES256"],
                audience=self.audience, issuer="https://cloud.google.com/iap",
            )
        except jwt.PyJWTError as e:
            raise AuthError(f"invalid IAP assertion: {e}") from e
        email = claims.get("email", "")
        if not email:
            raise AuthError("IAP assertion has no email claim")
        return email


def validate_google_id_token(id_token: str, client_id: str) -> str:
    """Claims-validate a Google ID token received directly from Google's token
    endpoint over TLS (OIDC code flow). Signature verification is intentionally
    skipped — the OIDC spec permits it for tokens obtained straight from the
    issuer, and we never accept ID tokens from any other channel."""
    try:
        claims = jwt.decode(id_token, options={"verify_signature": False})
    except jwt.PyJWTError as e:
        raise AuthError(f"malformed ID token: {e}") from e
    if claims.get("iss") not in ("https://accounts.google.com", "accounts.google.com"):
        raise AuthError("ID token has the wrong issuer")
    if claims.get("aud") != client_id:
        raise AuthError("ID token has the wrong audience")
    if claims.get("exp", 0) < time.time():
        raise AuthError("ID token is expired")
    email = claims.get("email", "")
    if not email or not claims.get("email_verified", False):
        raise AuthError("ID token has no verified email")
    return email
