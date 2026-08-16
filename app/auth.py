"""Entra ID JWT bearer validation.

Same shape as the middleware in riskguard-ai, and the same lesson applied
earlier: the surface is protected from the first commit rather than retrofitted
once it is already internet-reachable.

Pure-ASGI rather than BaseHTTPMiddleware so streaming responses are never
wrapped, and so it sits in front of routing and covers every path.

Inert until both entra_tenant_id and entra_audience are set: local runs and
tests stay frictionless, and app/main.py logs a warning when enforcement is
off. /healthz is exempt because the platform probe cannot present a token.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import httpx
import jwt
from jwt.algorithms import RSAAlgorithm

logger = logging.getLogger(__name__)

EXEMPT_PATHS = frozenset({"/healthz"})

# Entra signs with RS256, and pinning the allowlist is the whole defence against
# algorithm confusion. Without it a token declaring alg=HS256 would be verified
# with the RSA *public* key as an HMAC secret — a value anyone can fetch from
# the JWKS endpoint — and forging a token becomes arithmetic.
ALLOWED_ALGORITHMS = ("RS256",)

AUTHORITY = "https://login.microsoftonline.com"


def jwks_uri(tenant_id: str) -> str:
    return f"{AUTHORITY}/{tenant_id}/discovery/v2.0/keys"


def accepted_issuers(tenant_id: str) -> tuple[str, ...]:
    """Both issuer forms, because which one arrives is not ours to decide.

    A v2.0 token carries the login.microsoftonline.com issuer; a v1 token — what
    an app registration still emits when `accessTokenAcceptedVersion` is unset —
    carries sts.windows.net. That is a property of the app manifest, so pinning
    one form here produces an auth failure whose cause lives in a different
    system entirely.
    """
    return (f"{AUTHORITY}/{tenant_id}/v2.0", f"https://sts.windows.net/{tenant_id}/")


class InvalidToken(Exception):
    """Raised for anything that makes a token unacceptable. The reason is for
    the log; the caller gets a flat 401 either way."""


class JwksCache:
    """The tenant's signing keys, fetched once and held.

    Entra rotates keys, so an unrecognised `kid` has to be able to force a
    refetch. It also has to be rate-limited, because an unrecognised `kid` is
    exactly what a forged token looks like — without a floor, unauthenticated
    requests could drive unbounded outbound fetches, which is a denial-of-service
    against the login endpoint with this service as the amplifier.
    """

    def __init__(
        self,
        uri: str,
        *,
        ttl: float = 3600.0,
        min_refetch_interval: float = 300.0,
        client: httpx.AsyncClient | None = None,
    ):
        self._uri = uri
        self._ttl = ttl
        self._min_refetch_interval = min_refetch_interval
        self._client = client
        self._keys: dict[str, Any] = {}
        self._last_success = 0.0
        self._last_attempt = 0.0
        self._lock = asyncio.Lock()

    def _expired(self) -> bool:
        return (time.monotonic() - self._last_success) > self._ttl

    def _refetch_allowed(self) -> bool:
        return (time.monotonic() - self._last_attempt) > self._min_refetch_interval

    async def get(self, kid: str) -> Any | None:
        hit = self._keys.get(kid)
        if hit is not None and not self._expired():
            return hit

        async with self._lock:
            # Another request may have refreshed while this one waited on the
            # lock; re-checking here is what keeps a burst to a single fetch.
            hit = self._keys.get(kid)
            if hit is not None and not self._expired():
                return hit
            if self._expired() or self._refetch_allowed():
                await self._refresh()
            return self._keys.get(kid)

    async def _refresh(self) -> None:
        # Stamped before the request, not after: a failing endpoint must still
        # advance the rate limit, or every request retries it.
        self._last_attempt = time.monotonic()

        client = self._client or httpx.AsyncClient(timeout=5.0)
        try:
            response = await client.get(self._uri)
            response.raise_for_status()
            document = response.json()
        finally:
            if self._client is None:
                await client.aclose()

        self._keys = {
            key["kid"]: RSAAlgorithm.from_jwk(json.dumps(key))
            for key in document.get("keys", [])
            if key.get("kty") == "RSA" and "kid" in key
        }
        self._last_success = time.monotonic()


class EntraIdAuthMiddleware:
    """Rejects any request without a valid Entra ID bearer token."""

    def __init__(
        self,
        app: Any,
        *,
        tenant_id: str,
        audience: str,
        jwks: JwksCache | None = None,
        exempt_paths: frozenset[str] = EXEMPT_PATHS,
    ):
        self._app = app
        self._audience = audience
        self._issuers = accepted_issuers(tenant_id)
        self._jwks = jwks if jwks is not None else JwksCache(jwks_uri(tenant_id))
        self._exempt = frozenset(exempt_paths)

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        # Websocket and lifespan scopes pass through: there is no HTTP response
        # to reject them with, and neither carries an Authorization header.
        if scope["type"] != "http" or scope.get("path") in self._exempt:
            await self._app(scope, receive, send)
            return

        token = _bearer_token(scope)
        if token is None:
            await _reject(send)
            return

        try:
            claims = await self._verify(token)
        except InvalidToken as exc:
            # The reason goes to the log and never to the caller. "wrong
            # audience" or "expired" tells someone probing the surface exactly
            # what to change next, which is a free oracle.
            logger.warning("rejected bearer token: %s", exc)
            await _reject(send)
            return
        except httpx.HTTPError as exc:
            # Failing open here would mean an outage at the identity provider
            # silently unauthenticates the API. 503 is the honest answer.
            logger.error("JWKS endpoint unavailable, cannot validate: %s", exc)
            await _reject(send, status=503, detail="authentication backend unavailable")
            return

        # Namespaced scope key rather than scope["state"], which Starlette owns
        # and repopulates from the lifespan state during routing.
        scope["clausewatch.principal"] = claims
        await self._app(scope, receive, send)

    async def _verify(self, token: str) -> dict[str, Any]:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise InvalidToken(f"unreadable header: {exc}") from exc

        algorithm = header.get("alg")
        if algorithm not in ALLOWED_ALGORITHMS:
            raise InvalidToken(f"unacceptable algorithm {algorithm!r}")

        kid = header.get("kid")
        if not kid:
            raise InvalidToken("header carries no kid")

        key = await self._jwks.get(kid)
        if key is None:
            raise InvalidToken(f"no signing key for kid {kid!r}")

        try:
            claims = jwt.decode(
                token,
                key=key,
                algorithms=list(ALLOWED_ALGORITHMS),
                audience=self._audience,
                options={
                    "require": ["exp", "iat", "iss", "aud"],
                    # Checked below instead: PyJWT only accepts a single issuer
                    # string before 2.10, and two forms are legitimate here.
                    "verify_iss": False,
                },
            )
        except jwt.PyJWTError as exc:
            raise InvalidToken(str(exc)) from exc
        except Exception as exc:
            # Deliberately broad. PyJWT does not raise only PyJWTError: hand it
            # a key of a type an algorithm cannot use and it raises TypeError
            # out of prepare_key. Anything unexpected during verification has to
            # land as a 401, never as an unhandled 500 carrying a traceback.
            raise InvalidToken(f"verification failed: {exc!r}") from exc

        if claims.get("iss") not in self._issuers:
            raise InvalidToken(f"issuer {claims.get('iss')!r} is not this tenant")

        return claims


def principal(request: Any) -> dict[str, Any] | None:
    """The validated claims for this request, or None where enforcement is off.

    Route handlers take `request: Request` and call this rather than reaching
    into the scope key directly.
    """
    return request.scope.get("clausewatch.principal")


def _bearer_token(scope: dict) -> str | None:
    for name, value in scope.get("headers", []):
        if name.lower() != b"authorization":
            continue
        scheme, _, token = value.decode("latin-1").partition(" ")
        if scheme.lower() != "bearer":
            return None
        return token.strip() or None
    return None


async def _reject(
    send: Any,
    *,
    status: int = 401,
    detail: str = "invalid or missing bearer token",
) -> None:
    body = json.dumps({"detail": detail}).encode()
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
    ]
    if status == 401:
        headers.append((b"www-authenticate", b'Bearer realm="clausewatch"'))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})
