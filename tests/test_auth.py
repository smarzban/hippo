import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from hippo.auth import AuthError, IapVerifier, check_domain, validate_google_id_token

AUD = "/projects/1/global/backendServices/2"


def test_check_domain():
    check_domain("a@superbalist.com", "superbalist.com")  # ok
    check_domain("a@anywhere.com", "")  # empty = any domain
    with pytest.raises(AuthError):
        check_domain("a@gmail.com", "superbalist.com")
    with pytest.raises(AuthError):
        check_domain("a@notsuperbalist.com.evil.com", "superbalist.com")


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


def _id_token(**over):
    claims = {"iss": "https://accounts.google.com", "aud": "client-1",
              "exp": int(time.time()) + 600, "email": "a@x.com", "email_verified": True}
    claims.update(over)
    return jwt.encode(claims, "irrelevant", algorithm="HS256")


def test_google_id_token_valid():
    assert validate_google_id_token(_id_token(), "client-1") == "a@x.com"


@pytest.mark.parametrize("over", [
    {"iss": "https://evil.example"},
    {"aud": "other-client"},
    {"exp": int(time.time()) - 10},
    {"email_verified": False},
    {"email": ""},
])
def test_google_id_token_rejects(over):
    with pytest.raises(AuthError):
        validate_google_id_token(_id_token(**over), "client-1")


def test_google_id_token_rejects_garbage():
    with pytest.raises(AuthError):
        validate_google_id_token("not-a-jwt", "client-1")
