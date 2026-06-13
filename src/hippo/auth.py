"""Identity layer. Every mode converges on AuthenticatedUser(email, role); the
rest of the codebase never knows how the email was established (spec §1)."""

from dataclasses import dataclass
from typing import TYPE_CHECKING

import jwt

if TYPE_CHECKING:
    from .config import Settings
    from .storage import Storage


class AuthError(Exception):
    """Identity could not be established or is not allowed (-> 401/403)."""


@dataclass
class AuthenticatedUser:
    email: str
    role: str  # user | admin | owner


def check_domain(email: str, allowed_domain: str) -> None:
    if allowed_domain and not email.lower().endswith("@" + allowed_domain.lower()):
        raise AuthError(f"only {allowed_domain} accounts are allowed")


def resolve_role(store: "Storage", settings: "Settings", email: str) -> str:
    """Canonical identity → role: normalize, enforce the domain gate, ensure the
    user row (first-timers default to 'user'), then apply the admin-email
    bootstrap. Raises AuthError if the email is out of the allowed domain. Shared
    by the HTTP bearer path (api.py) and the Slack bot."""
    email = email.strip().lower()
    check_domain(email, settings.allowed_domain)  # raises AuthError
    role = store.ensure_user(email)
    if email in settings.admin_email_list:
        role = "owner"  # env bootstrap is the top tier (spec §3)
    return role


class _KeyCache:
    """JWKS cache that refetches ONCE when an unknown kid arrives (key rotation)."""

    def __init__(self, fetcher):
        self._fetch = fetcher
        self._keys: dict | None = None

    def get(self, kid):
        if self._keys is None:
            self._keys = self._fetch()
        if kid not in self._keys:
            self._keys = self._fetch()  # rotation: one refetch, then fail
        return self._keys.get(kid)


def _fetch_jwks(url: str) -> dict:
    import httpx

    jwks = httpx.get(url, timeout=10).json()
    return {k["kid"]: jwt.PyJWK(k).key for k in jwks["keys"]}


class IapVerifier:
    """Verifies GCP Identity-Aware Proxy assertions (ES256 JWTs signed by Google).

    key_fetcher is injectable so tests supply a local key; production lazily
    fetches Google's JWKS once per process and caches it.  An unknown kid
    triggers one JWKS refetch (key rotation) before failing."""

    KEYS_URL = "https://www.gstatic.com/iap/verify/public_key-jwk"

    def __init__(self, audience: str, key_fetcher=None):
        self.audience = audience
        fetcher = key_fetcher or (lambda: _fetch_jwks(self.KEYS_URL))
        self._cache = _KeyCache(fetcher)

    def verify(self, assertion: str) -> str:
        try:
            kid = jwt.get_unverified_header(assertion).get("kid")
        except jwt.PyJWTError as e:
            raise AuthError(f"malformed IAP assertion: {e}") from e
        key = self._cache.get(kid)
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


GOOGLE_OIDC_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"

# Module-level cache for the default (production) key fetcher.  Tests always
# inject their own key_fetcher, so this is never exercised in the test suite.
_google_keys: _KeyCache | None = None


def validate_google_id_token(id_token: str, client_id: str, *, key_fetcher=None) -> str:
    """Verify a Google ID token: signature against Google's OIDC JWKS (RS256),
    plus issuer / audience / expiry / verified-email claims.

    key_fetcher is injectable for tests; unknown kids trigger one JWKS refetch
    (key rotation) before failing."""
    global _google_keys
    if key_fetcher is None:
        # Production path: share one module-level cache across requests.
        if _google_keys is None:
            _google_keys = _KeyCache(lambda: _fetch_jwks(GOOGLE_OIDC_JWKS_URL))
        cache = _google_keys
    else:
        # Test/injected path: fresh cache per call so tests are isolated.
        cache = _KeyCache(key_fetcher)

    try:
        kid = jwt.get_unverified_header(id_token).get("kid")
    except jwt.PyJWTError as e:
        raise AuthError(f"malformed ID token: {e}") from e

    key = cache.get(kid)
    if key is None:
        raise AuthError("unknown Google signing key")

    try:
        claims = jwt.decode(id_token, key=key, algorithms=["RS256"], audience=client_id)
    except jwt.PyJWTError as e:
        raise AuthError(f"invalid ID token: {e}") from e

    if claims.get("iss") not in ("https://accounts.google.com", "accounts.google.com"):
        raise AuthError("ID token has the wrong issuer")

    email = claims.get("email", "")
    if not email or not claims.get("email_verified", False):
        raise AuthError("ID token has no verified email")

    return email


# --- password hashing (SP2) ---
# argon2id (argon2-cffi default type). One module-level hasher; tests swap it for
# a reduced-cost profile via set_password_hasher. Never log or return a hash.
from argon2 import PasswordHasher as _PasswordHasher
from argon2.exceptions import Argon2Error, InvalidHashError as _InvalidHashError

_HASHER = _PasswordHasher()


def set_password_hasher(hasher) -> None:
    """Swap the argon2 hasher (tests use a reduced-cost profile)."""
    global _HASHER
    _HASHER = hasher


def hash_password(password: str) -> str:
    """Return an argon2id encoded hash (includes the per-hash salt + params)."""
    return _HASHER.hash(password)


def verify_password(hashed: str, password: str) -> bool:
    """Constant-time verify. False on mismatch OR a malformed/foreign hash —
    never raises, so callers get a clean boolean and no enumeration signal."""
    try:
        return _HASHER.verify(hashed, password)
    except (Argon2Error, _InvalidHashError):
        return False
