"""Entra ID bearer validation.

Real RS256 signing against a throwaway keypair rather than a mocked `jwt.decode`
— the point of these tests is the rejections, and a mock that returns claims
proves nothing about which tokens actually get in.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, Request
from jwt.algorithms import RSAAlgorithm

from app.auth import EntraIdAuthMiddleware, JwksCache, accepted_issuers, principal

TENANT = "11111111-2222-3333-4444-555555555555"
AUDIENCE = "api://clausewatch"
ISSUER_V2, ISSUER_V1 = accepted_issuers(TENANT)
KID = "test-signing-key"


@pytest.fixture(scope="module")
def keypair():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private, private.public_key()


@pytest.fixture(scope="module")
def jwk(keypair):
    _, public = keypair
    document = json.loads(RSAAlgorithm.to_jwk(public))
    document["kid"] = KID
    return document


def make_token(
    keypair,
    *,
    kid: str = KID,
    algorithm: str = "RS256",
    audience: str = AUDIENCE,
    issuer: str = ISSUER_V2,
    expires_in: int = 3600,
    key=None,
    **extra,
) -> str:
    private, _ = keypair
    now = int(time.time())
    claims = {
        "aud": audience,
        "iss": issuer,
        "iat": now,
        "exp": now + expires_in,
        "sub": "user-1",
        **extra,
    }
    return jwt.encode(claims, key or private, algorithm=algorithm, headers={"kid": kid})


class StubJwks:
    """A JwksCache with the network taken out."""

    def __init__(self, keys: dict):
        self._keys = keys
        self.lookups = 0

    async def get(self, kid: str):
        self.lookups += 1
        return self._keys.get(kid)


@pytest.fixture
def client(keypair, jwk):
    _, public = keypair
    app = FastAPI()

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.get("/obligations")
    async def obligations(request: Request):
        claims = principal(request)
        return {"sub": claims["sub"] if claims else None}

    app.add_middleware(
        EntraIdAuthMiddleware,
        tenant_id=TENANT,
        audience=AUDIENCE,
        jwks=StubJwks({KID: public}),
    )
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _hs256(header: dict, claims: dict, *, secret: bytes) -> str:
    signing_input = f"{_b64(json.dumps(header).encode())}.{_b64(json.dumps(claims).encode())}"
    digest = hmac.new(secret, signing_input.encode(), hashlib.sha256).digest()
    return f"{signing_input}.{_b64(digest)}"


# --------------------------------------------------------------- pass-through


async def test_healthz_needs_no_token(client):
    # The Container Apps probe cannot present a credential, so this must stay
    # reachable or the platform recycles every replica.
    async with client:
        assert (await client.get("/healthz")).status_code == 200


async def test_valid_token_reaches_the_route_with_its_claims(client, keypair):
    async with client:
        response = await client.get("/obligations", headers=auth(make_token(keypair)))
    assert response.status_code == 200
    assert response.json() == {"sub": "user-1"}


async def test_v1_issuer_is_accepted(client, keypair):
    # Which issuer form arrives depends on the app registration manifest, not on
    # anything this service controls.
    async with client:
        token = make_token(keypair, issuer=ISSUER_V1)
        assert (await client.get("/obligations", headers=auth(token))).status_code == 200


# ------------------------------------------------------------------ rejections


async def test_no_header_is_rejected_with_a_challenge(client):
    async with client:
        response = await client.get("/obligations")
    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Bearer")


@pytest.mark.parametrize(
    "header",
    ["", "Bearer", "Bearer ", "Basic abc123", "bearer", "Token xyz"],
)
async def test_malformed_authorization_headers_are_rejected(client, header):
    async with client:
        response = await client.get("/obligations", headers={"Authorization": header})
    assert response.status_code == 401


async def test_expired_token_is_rejected(client, keypair):
    async with client:
        token = make_token(keypair, expires_in=-60)
        assert (await client.get("/obligations", headers=auth(token))).status_code == 401


async def test_wrong_audience_is_rejected(client, keypair):
    async with client:
        token = make_token(keypair, audience="api://something-else")
        assert (await client.get("/obligations", headers=auth(token))).status_code == 401


async def test_foreign_issuer_is_rejected(client, keypair):
    # A token from another tenant is signed correctly and means nothing here.
    async with client:
        token = make_token(keypair, issuer="https://login.microsoftonline.com/other/v2.0")
        assert (await client.get("/obligations", headers=auth(token))).status_code == 401


async def test_unknown_kid_is_rejected(client, keypair):
    async with client:
        token = make_token(keypair, kid="a-key-we-have-never-seen")
        assert (await client.get("/obligations", headers=auth(token))).status_code == 401


async def test_token_signed_by_the_wrong_key_is_rejected(client, keypair):
    # Right kid, right claims, attacker's key.
    impostor = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    async with client:
        token = make_token(keypair, key=impostor)
        assert (await client.get("/obligations", headers=auth(token))).status_code == 401


async def test_tampered_payload_is_rejected(client, keypair):
    async with client:
        header, payload, signature = make_token(keypair).split(".")
        forged = f"{header}.{payload[:-4]}AAAA.{signature}"
        assert (await client.get("/obligations", headers=auth(forged))).status_code == 401


async def test_algorithm_confusion_is_rejected(client, keypair, jwk):
    """The attack the algorithm allowlist exists for.

    An HS256 token signed with the RSA *public key* — a value published at the
    JWKS endpoint — verifies as valid HMAC if the library is allowed to pick the
    algorithm from the token header. Pinning RS256 is what stops it.
    """
    _, public = keypair
    pem = public.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    now = int(time.time())
    # Assembled by hand: PyJWT refuses to *sign* with a PEM as an HMAC secret,
    # and an attacker has no reason to use PyJWT. Skipping the library here is
    # what makes this a test of the middleware rather than of PyJWT's encoder.
    forged = _hs256(
        {"alg": "HS256", "typ": "JWT", "kid": KID},
        {"aud": AUDIENCE, "iss": ISSUER_V2, "iat": now, "exp": now + 3600, "sub": "attacker"},
        secret=pem,
    )
    async with client:
        assert (await client.get("/obligations", headers=auth(forged))).status_code == 401


async def test_unsigned_token_is_rejected(client):
    now = int(time.time())
    forged = jwt.encode(
        {"aud": AUDIENCE, "iss": ISSUER_V2, "iat": now, "exp": now + 3600, "sub": "attacker"},
        key="",
        algorithm="none",
        headers={"kid": KID},
    )
    async with client:
        assert (await client.get("/obligations", headers=auth(forged))).status_code == 401


async def test_token_missing_required_claims_is_rejected(client, keypair):
    private, _ = keypair
    # No exp: a token that never expires is not one this accepts.
    token = jwt.encode(
        {"aud": AUDIENCE, "iss": ISSUER_V2, "iat": int(time.time()), "sub": "x"},
        private,
        algorithm="RS256",
        headers={"kid": KID},
    )
    async with client:
        assert (await client.get("/obligations", headers=auth(token))).status_code == 401


async def test_unusable_signing_key_fails_closed(keypair):
    """A key the algorithm cannot use must be a 401, not a 500.

    PyJWT does not confine itself to PyJWTError — prepare_key raises TypeError
    on a key of the wrong type — so a bare `except PyJWTError` would let that
    escape as an unhandled server error with a traceback attached.
    """
    app = FastAPI()

    @app.get("/obligations")
    async def obligations():
        return {}

    app.add_middleware(
        EntraIdAuthMiddleware,
        tenant_id=TENANT,
        audience=AUDIENCE,
        jwks=StubJwks({KID: object()}),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/obligations", headers=auth(make_token(keypair)))

    assert response.status_code == 401


# ------------------------------------------------------------------ jwks cache


async def test_jwks_is_fetched_once_and_cached(jwk):
    calls = []

    async def handler(request):
        calls.append(request.url)
        return httpx.Response(200, json={"keys": [jwk]})

    cache = JwksCache(
        "https://example.test/keys",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    assert await cache.get(KID) is not None
    assert await cache.get(KID) is not None
    assert len(calls) == 1


async def test_unknown_kid_refetch_is_rate_limited(jwk):
    """An unknown kid must be able to pull rotated keys, but not on every request.

    An unrecognised kid is also what a forged token looks like, so without the
    floor an unauthenticated caller can drive unbounded outbound fetches.
    """
    calls = []

    async def handler(request):
        calls.append(request.url)
        return httpx.Response(200, json={"keys": [jwk]})

    cache = JwksCache(
        "https://example.test/keys",
        min_refetch_interval=300.0,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    for _ in range(5):
        assert await cache.get("rotated-in-key") is None
    assert len(calls) == 1


async def test_jwks_http_failure_surfaces_as_503(keypair, jwk):
    async def handler(request):
        return httpx.Response(500)

    app = FastAPI()

    @app.get("/obligations")
    async def obligations():
        return {}

    app.add_middleware(
        EntraIdAuthMiddleware,
        tenant_id=TENANT,
        audience=AUDIENCE,
        jwks=JwksCache(
            "https://example.test/keys",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        ),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/obligations", headers=auth(make_token(keypair)))

    # Not 401: an identity-provider outage must not read as "no credentials",
    # and must never fail open.
    assert response.status_code == 503
