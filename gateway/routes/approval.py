from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Body, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from gateway.registry import AgentRegistry


def build_approval_router(registry: AgentRegistry) -> APIRouter:
    router = APIRouter()

    @router.post("/approve")
    async def approve(request: Request) -> dict[str, Any]:
        
        payload = await request.json()
        raw_request_id = payload.get("request_id", "")
        if not raw_request_id:
            raise HTTPException(status_code=400, detail="Missing request_id")

        # The proxy doesn't inherently know which tenant owns the approve payload
        # without examining all tenants, or requiring tenant_id in the payload payload.
        # We'll broadcast it to all valid tenants until one accepts it, or we assume tenant_id is passed.
        
        tenant_id = payload.get("tenant_id") or payload.get("tenantId")
        if tenant_id:
            targets = [registry.get_tenant_container(tenant_id)]
            targets = [t for t in targets if t is not None]
        else:
            # Broadcast hunt
            targets = list(registry.manager._tenants.values())
        
        if not targets:
            raise HTTPException(status_code=400, detail="No active tenants to route approval to.")

        async with httpx.AsyncClient() as client:
            for tc in targets:
                url = f"http://localhost:{tc.port}/api/approve"
                try:
                    resp = await client.post(url, json=payload, timeout=120.0)
                    if resp.status_code == 200:
                        return resp.json()
                    elif resp.status_code != 400: # Could be standard bad request if not their pending id
                        pass
                except httpx.RequestError:
                    pass

        raise HTTPException(
            status_code=400,
            detail=f"No pending approval found for request_id: {raw_request_id} across any active tenants.",
        )

    return router
