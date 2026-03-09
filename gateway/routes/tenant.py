from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel

from gateway.registry import AgentRegistry


class TenantConfigBody(BaseModel):
    tenant_id: str
    config: dict[str, Any]


def build_tenant_router(registry: AgentRegistry) -> APIRouter:
    router = APIRouter()

    @router.post("/tenant/config")
    async def configure_tenant(payload: TenantConfigBody = Body(...)) -> dict[str, Any]:
        """Accepts a nanobot.config payload and spins up an isolated Docker container for the tenant."""
        try:
            tc = registry.provision_tenant(payload.tenant_id, payload.config)
            return {
                "ok": True,
                "tenant_id": tc.tenant_id,
                "container_id": tc.container_id[:12],
                "port": tc.port,
            }
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
            
    return router
