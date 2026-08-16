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

EXEMPT_PATHS = frozenset({"/healthz"})

# TODO(phase 6): JWKS fetch + cache, signature/claims validation, middleware.
