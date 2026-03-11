from __future__ import annotations

from typing import Any

import httpx
from fastapi import APIRouter, Body, HTTPException, Request
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

    @router.api_route("/tenant/container/{tenant_id}/proxy/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
    async def proxy_tenant(tenant_id: str, path: str, request: Request):
        """Generic proxy to a specific tenant's container for non-chat APIs (e.g. status, internal tools)."""
        tc = registry.get_tenant_container(tenant_id)
        if not tc:
            raise HTTPException(status_code=404, detail=f"Tenant {tenant_id} not found")

        url = f"http://localhost:{tc.port}/{path}"
        async with httpx.AsyncClient() as client:
            try:
                # Forward query params
                params = request.query_params
                
                # Forward body if present
                content = await request.body()
                
                resp = await client.request(
                    method=request.method,
                    url=url,
                    params=params,
                    content=content,
                    headers={"Content-Type": request.headers.get("Content-Type", "application/json")},
                    timeout=30.0
                )
                
                if resp.status_code >= 400:
                    try:
                        detail = resp.json()
                    except:
                        detail = resp.text
                    raise HTTPException(status_code=resp.status_code, detail=detail)
                
                # Some APIs might not return JSON (though nanogate/agent usually does)
                try:
                    return resp.json()
                except:
                    return resp.text
            except httpx.RequestError as e:
                raise HTTPException(status_code=502, detail=f"Proxy error: {e}")

    return router
