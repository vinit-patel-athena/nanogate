from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from gateway.registry import AgentRegistry, DEFAULT_TENANT_ID

logger = logging.getLogger(__name__)


def _normalize_session(raw: str | None) -> str:
    """Match agent's normalization so gateway and agent use the same Redis stream key."""
    session_id = (raw.strip() if isinstance(raw, str) and raw else None) or "api:direct"
    if ":" not in session_id:
        session_id = f"api:{session_id}"
    return session_id


class ChatRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    message: str = Field(..., min_length=1)
    session_id: str | None = Field(default=None, alias="sessionId")
    tenant_id: str | None = Field(default=None, alias="tenantId")
    callback_url: str | None = Field(default=None, alias="callbackUrl")


def build_chat_router(registry: AgentRegistry) -> APIRouter:
    router = APIRouter()

    async def _get_tenant(tenant_id: str | None, request: Request):
        tid = (tenant_id or "").strip() or DEFAULT_TENANT_ID
        tc = await registry.get_or_create(tid)
        if not tc:
            raise HTTPException(status_code=503, detail="Tenant container not initialized")
        return tc

    @router.post("/chat")
    async def chat(body: ChatRequest) -> dict[str, Any]:
        """Publishes a chat request to the tenant's inbound bus. Use callbackUrl for offline delivery."""
        if not (body.message or "").strip():
            raise HTTPException(status_code=400, detail="message must be non-empty")
        tenant_id = (body.tenant_id or "").strip() or DEFAULT_TENANT_ID
        await registry.get_or_create(tenant_id)

        payload = body.model_dump(by_alias=True, exclude_none=True)
        # Normalize session_id so gateway and agent use the same Redis stream key
        session_id = _normalize_session(payload.get("sessionId") or payload.get("session_id"))
        payload["sessionId"] = payload["session_id"] = session_id
        if body.callback_url:
            payload["callbackUrl"] = body.callback_url
        payload.setdefault("message", body.message)

        await registry.message_bus.publish_request(tenant_id, payload)

        return {
            "status": "accepted",
            "session_id": session_id,
            "chat_id": session_id,  # alias for compatibility
            "stream_url": f"/api/chat/stream/{session_id}",
        }

    @router.post("/chat/async")
    async def chat_async(body: ChatRequest) -> dict[str, Any]:
        """Fire-and-forget via Redis bus: same as POST /chat but requires callbackUrl. Final result is POSTed to callbackUrl by the gateway (WebhookDispatcher)."""
        if not body.callback_url:
            raise HTTPException(status_code=400, detail="callbackUrl required for /api/chat/async")
        return await chat(body)

    @router.get("/chat/stream/{session_id}")
    async def stream_chat(
        session_id: str,
        request: Request,
        last_event_id: str = "0",
    ):
        """Streams agent events (SSE). Use last_event_id to resume from a previous event (sync when back online)."""
        async def event_generator():
            try:
                async for event in registry.message_bus.subscribe_events(
                    session_id, last_id=last_event_id
                ):
                    if await request.is_disconnected():
                        break
                    yield f"data: {json.dumps(event)}\n\n"
                    if event.get("status") in ("done", "error"):
                        break
            except Exception as e:
                logger.error("Error streaming events for %s: %s", session_id, e)
                yield f"data: {json.dumps({'status': 'error', 'error': str(e)})}\n\n"

        return StreamingResponse(event_generator(), media_type="text/event-stream")

    return router
