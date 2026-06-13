import base64
import json
import sqlite3
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric import rsa

from hippo.auth import AuthError, IapVerifier, check_domain, resolve_role, validate_google_id_token
from hippo.config import Settings
from hippo.db import connect
from hippo.embeddings import FakeEmbedder
from hippo.storage import Storage


def _store(tmp_path):
    con = connect(tmp_path / "h.db", embedding_dim=8)
    return Storage(con, FakeEmbedder(dim=8))


def test_resolve_role_first_timer_is_user(tmp_path):
    store = _store(tmp_path)
    settings = Settings(allowed_domain="example.com")
    assert resolve_role(store, settings, "new.person@example.com") == "user"


def test_resolve_role_admin_bootstrap_wins(tmp_path):
    store = _store(tmp_path)
    settings = Settings(allowed_domain="example.com", admin_emails="boss@example.com")
    assert resolve_role(store, settings, "Boss@Example.com") == "owner"


def test_resolve_role_defaults_to_user_and_bootstraps_owner(tmp_path):
    from hippo.config import Settings
    from hippo.db import connect
    from hippo.embeddings import FakeEmbedder
    from hippo.storage import Storage
    from hippo.auth import resolve_role

    con = connect(tmp_path / "t.db", embedding_dim=32)
    store = Storage(con, FakeEmbedder(dim=32))
    s = Settings(_env_file=None, admin_emails="boss@x.com")
    assert resolve_role(store, s, "newbie@x.com") == "user"
    assert resolve_role(store, s, "boss@x.com") == "owner"


def test_resolve_role_out_of_domain_raises(tmp_path):
    store = _store(tmp_path)
    settings = Settings(allowed_domain="example.com")
    with pytest.raises(AuthError):
        resolve_role(store, settings, "outsider@gmail.com")

AUD = "/projects/1/global/backendServices/2"

# Module-level RSA key — generated once (~100ms), reused by all google-id-token tests.
RSA_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def test_check_domain():
    check_domain("a@example.com", "example.com")  # ok
    check_domain("a@anywhere.com", "")  # empty = any domain
    with pytest.raises(AuthError):
        check_domain("a@gmail.com", "example.com")
    with pytest.raises(AuthError):
        check_domain("a@notexample.com.evil.com", "example.com")


@pytest.fixture
def ec_key():
    return ec.generate_private_key(ec.SECP256R1())


def _assertion(key, *, aud=AUD, email="a@x.com", kid="k1", exp_offset=600):
    claims = {"aud": aud, "iss": "https://cloud.google.com/iap",
              "exp": int(time.time()) + exp_offset, "email": email}
    return jwt.encode(claims, key, algorithm="ES256", headers={"kid": kid})


def test_iap_verifier_accepts_valid_assertion(ec_key):
    v = IapVerifier(AUD, key_fetcher=lambda: {"k1": ec_key.public_key()})
    assert v.verify(_assertion(ec_key)) == "a@x.com"


@pytest.mark.parametrize("kwargs", [
    {"aud": "/projects/9/global/backendServices/9"},  # wrong audience
    {"exp_offset": -600},                              # expired
    {"kid": "unknown"},                                # unknown signing key
    {"email": ""},                                     # no email claim
])
def test_iap_verifier_rejects(ec_key, kwargs):
    v = IapVerifier(AUD, key_fetcher=lambda: {"k1": ec_key.public_key()})
    with pytest.raises(AuthError):
        v.verify(_assertion(ec_key, **kwargs))


def test_iap_verifier_rejects_garbage(ec_key):
    v = IapVerifier(AUD, key_fetcher=lambda: {"k1": ec_key.public_key()})
    with pytest.raises(AuthError):
        v.verify("not-a-jwt")


def test_iap_verifier_refetches_keys_on_unknown_kid(ec_key):
    rotated = {"k1": ec_key.public_key()}
    fetches = []

    def fetcher():
        fetches.append(1)
        return dict(rotated)

    v = IapVerifier(AUD, key_fetcher=fetcher)
    assert v.verify(_assertion(ec_key)) == "a@x.com"   # fetch #1, kid k1
    from cryptography.hazmat.primitives.asymmetric import ec as ec_mod
    new_key = ec_mod.generate_private_key(ec_mod.SECP256R1())
    rotated.clear(); rotated["k2"] = new_key.public_key()  # Google rotates
    assert v.verify(_assertion(new_key, kid="k2")) == "a@x.com"  # refetch on unknown kid
    assert len(fetches) == 2


# ---------------------------------------------------------------------------
# Google ID token helpers
# ---------------------------------------------------------------------------

def _claims(**over):
    c = {"iss": "https://accounts.google.com", "aud": "client-1",
         "exp": int(time.time()) + 600, "email": "a@x.com", "email_verified": True}
    c.update(over)
    return c


def _id_token(**over):
    return jwt.encode(_claims(**over), RSA_KEY, algorithm="RS256", headers={"kid": "g1"})


def _key_fetcher():
    return {"g1": RSA_KEY.public_key()}


def test_google_id_token_valid():
    assert validate_google_id_token(_id_token(), "client-1", key_fetcher=_key_fetcher) == "a@x.com"


@pytest.mark.parametrize("over", [
    {"iss": "https://evil.example"},
    {"aud": "other-client"},
    {"exp": int(time.time()) - 10},
    {"email_verified": False},
    {"email": ""},
])
def test_google_id_token_rejects(over):
    with pytest.raises(AuthError):
        validate_google_id_token(_id_token(**over), "client-1", key_fetcher=_key_fetcher)


def test_google_id_token_rejects_garbage():
    with pytest.raises(AuthError):
        validate_google_id_token("not-a-jwt", "client-1", key_fetcher=_key_fetcher)


def test_google_id_token_rejects_wrong_signature():
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = jwt.encode(_claims(), other, algorithm="RS256", headers={"kid": "g1"})
    with pytest.raises(AuthError):
        validate_google_id_token(token, "client-1", key_fetcher=_key_fetcher)


def test_google_id_token_rejects_unknown_kid_after_refetch():
    calls = []

    def fetcher():
        calls.append(1)
        return {"g1": RSA_KEY.public_key()}

    token = jwt.encode(_claims(), RSA_KEY, algorithm="RS256", headers={"kid": "g2"})
    with pytest.raises(AuthError):
        validate_google_id_token(token, "client-1", key_fetcher=fetcher)
    assert len(calls) == 2  # refetched once on unknown kid (rotation), then failed


def test_google_id_token_rejects_none_algorithm():
    header = base64.urlsafe_b64encode(json.dumps({"alg": "none", "kid": "g1"}).encode()).rstrip(b"=")
    payload = base64.urlsafe_b64encode(json.dumps(_claims()).encode()).rstrip(b"=")
    with pytest.raises(AuthError):
        validate_google_id_token(
            (header + b"." + payload + b".").decode(), "client-1",
            key_fetcher=lambda: {"g1": RSA_KEY.public_key()},
        )
