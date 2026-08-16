"""GET /healthz - unauthenticated liveness probe for the Container Apps probe.

Liveness only: touches neither Cosmos nor Foundry. A transient Cosmos blip
should surface as a 503 on the real endpoints, not cause the platform to
recycle otherwise-healthy replicas.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}
