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


class ChatResponse(BaseModel):
    response: str
    approval_request_id: str | None = None


def build_chat_router(registry: AgentRegistry) -> APIRouter:
    router = APIRouter()

    @router.post("/chat")
    async def chat(request: Request, body: ChatRequest) -> dict[str, Any]:
        tenant_id = (body.tenant_id or "").strip() or DEFAULT_TENANT_ID
        
        # Ensure container is running for this tenant
        tc = await registry.get_or_create(tenant_id)
        if not tc:
            raise HTTPException(status_code=503, detail="Tenant container not initialized")

        # Forward the raw payload to the internal container's 8765 port
        url = f"http://localhost:{tc.port}/api/chat"
        payload = await request.json()
        
        async with httpx.AsyncClient() as client:
            try:
                # Post direct to the internal nanobot server
                # Timeout is generous to allow LLM Generation
                resp = await client.post(url, json=payload, timeout=300.0)
                if resp.status_code >= 400:
                    raise HTTPException(status_code=resp.status_code, detail=resp.text)
                return resp.json()
            except httpx.RequestError as e:
                raise HTTPException(status_code=502, detail=f"Proxy error communicating with tenant container: {e}") 

    return router
