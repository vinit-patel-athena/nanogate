from __future__ import annotations

from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from gateway.registry import AgentRegistry, DEFAULT_TENANT_ID


class ChatRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    message: str
    session_id: str | None = Field(default="api:direct", alias="sessionId")
    tenant_id: str | None = Field(default=None, alias="tenantId")


def build_chat_router(registry: AgentRegistry) -> APIRouter:
    router = APIRouter()

    async def _get_tenant(tenant_id: str | None, request: Request):
        tid = (tenant_id or "").strip() or DEFAULT_TENANT_ID
        tc = await registry.get_or_create(tid)
        if not tc:
            raise HTTPException(status_code=503, detail="Tenant container not initialized")
        return tc

    @router.post("/chat")
    async def chat(request: Request) -> dict[str, Any]:
        payload = await request.json()
        tenant_id = (payload.get("tenantId") or payload.get("tenant_id") or "").strip() or DEFAULT_TENANT_ID
        tc = await registry.get_or_create(tenant_id)
        if not tc:
            raise HTTPException(status_code=503, detail="Tenant container not initialized")
        url = f"http://localhost:{tc.port}/api/chat"
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.post(url, json=payload, timeout=300.0)
                if resp.status_code >= 400:
                    raise HTTPException(status_code=resp.status_code, detail=resp.text)
                return resp.json()
            except httpx.RequestError as e:
                raise HTTPException(status_code=502, detail=f"Proxy error communicating with tenant container: {e}")

    @router.post("/chat/{path:path}")
    async def chat_subpath(path: str, request: Request) -> dict[str, Any]:
        """Forwards any /chat/* variant (e.g. /chat/async) to the tenant container unchanged."""
        payload = await request.json()
        tenant_id = (payload.get("tenantId") or payload.get("tenant_id") or "").strip() or DEFAULT_TENANT_ID
        tc = await registry.get_or_create(tenant_id)
        if not tc:
            raise HTTPException(status_code=503, detail="Tenant container not initialized")
        url = f"http://localhost:{tc.port}/api/chat/{path}"
        async with httpx.AsyncClient() as client:
            try:
                # Async variants return 202 quickly; use short timeout for that ack
                timeout = 15.0 if path.endswith("async") else 300.0
                resp = await client.post(url, json=payload, timeout=timeout)
                if resp.status_code >= 400:
                    raise HTTPException(status_code=resp.status_code, detail=resp.text)
                return resp.json()
            except httpx.RequestError as e:
                raise HTTPException(status_code=502, detail=f"Proxy error communicating with tenant container: {e}")

    return router
