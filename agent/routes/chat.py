"""Chat route for the single-tenant agent server."""

from __future__ import annotations

import asyncio
import uuid
import logging
from typing import Any, Callable

import httpx
from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)


class ChatRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    message: str
    session_id: str | None = Field(default="api:direct", alias="sessionId")


class AsyncChatRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    message: str
    session_id: str | None = Field(default="api:direct", alias="sessionId")
    callback_url: str = Field(alias="callbackUrl")


class ChatResponse(BaseModel):
    response: str
    approval_request_id: str | None = None
    approval_context: dict[str, Any] | None = None


class AsyncChatAccepted(BaseModel):
    """Returned immediately from /chat/async — the actual result is POSTed to callbackUrl."""
    chat_id: str
    status: str = "accepted"


def _normalize_session(raw: str | None) -> str:
    session_id = (raw.strip() if isinstance(raw, str) and raw else None) or "api:direct"
    if ":" not in session_id:
        session_id = f"api:{session_id}"
    return session_id


async def _run_chat(
    agent_loop,
    session_id: str,
    message: str,
    on_progress: Any | None = None,
) -> ChatResponse:
    """Core chat logic shared by the sync and async endpoints."""
    from agent.context import ACTIVE_SESSION, APPROVAL_REQUEST_ID, APPROVAL_CONTEXT

    ACTIVE_SESSION.set(session_id)
    APPROVAL_REQUEST_ID.set(None)
    APPROVAL_CONTEXT.set(None)

    chat_id = session_id.split(":", 1)[-1]

    response = await agent_loop.process_direct(
        message,
        session_key=session_id,
        channel="api",
        chat_id=chat_id,
        on_progress=on_progress,
    )

    approval_request_id = APPROVAL_REQUEST_ID.get()
    approval_context = APPROVAL_CONTEXT.get()

    if approval_request_id:
        response = (
            f"Approval required. To approve, send POST /api/approve with: "
            f'{{"request_id": "{approval_request_id}", "sessionId": "{session_id}"}}'
        )

    return ChatResponse(
        response=response or "",
        approval_request_id=approval_request_id,
        approval_context=approval_context,
    )


def build_chat_router(get_agent: Callable) -> APIRouter:
    router = APIRouter()

    @router.post("/chat", response_model=ChatResponse)
    async def chat(body: ChatRequest) -> ChatResponse:
        agent_loop = get_agent()
        if agent_loop is None:
            raise HTTPException(status_code=503, detail="Agent not initialized")
        if not body.message or not body.message.strip():
            raise HTTPException(status_code=400, detail="message must be non-empty")

        session_id = _normalize_session(body.session_id)

        try:
            return await _run_chat(agent_loop, session_id, body.message.strip())
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.post("/chat/async", response_model=AsyncChatAccepted, status_code=202)
    async def chat_async(body: AsyncChatRequest, background_tasks: BackgroundTasks) -> AsyncChatAccepted:
        """Fire-and-forget chat. Returns 202 immediately; POSTs the result to callbackUrl when done.
        
        Callback payload matches ChatResponse:
        {
            "chat_id": "...",
            "response": "...",
            "approval_request_id": "...",   # null if no approval needed
            "approval_context": {...}        # null if no approval needed
        }
        """
        agent_loop = get_agent()
        if agent_loop is None:
            raise HTTPException(status_code=503, detail="Agent not initialized")
        if not body.message or not body.message.strip():
            raise HTTPException(status_code=400, detail="message must be non-empty")

        session_id = _normalize_session(body.session_id)
        chat_id = str(uuid.uuid4())
        callback_url = body.callback_url

        async def _run_and_callback() -> None:
            try:
                async with httpx.AsyncClient() as client:
                    async def _on_progress(content: str, *, tool_hint: bool = False) -> None:
                        """Stream intermediate progress events to the callback URL."""
                        try:
                            await client.post(callback_url, json={
                                "chat_id": chat_id,
                                "status": "progress",
                                "response": content,
                                "tool_hint": tool_hint,
                            }, timeout=10.0)
                        except Exception as exc:
                            logger.debug(f"[chat/async] Progress callback failed: {exc}")

                    result = await _run_chat(
                        agent_loop, session_id, body.message.strip(),
                        on_progress=_on_progress,
                    )
                    payload: dict[str, Any] = {
                        "chat_id": chat_id,
                        "status": "done",
                        **result.model_dump(),
                    }
                    try:
                        await client.post(callback_url, json=payload, timeout=30.0)
                    except Exception as exc:
                        logger.warning(f"[chat/async] Final callback to {callback_url} failed: {exc}")

            except Exception as exc:
                try:
                    async with httpx.AsyncClient() as client:
                        await client.post(callback_url, json={
                            "chat_id": chat_id,
                            "error": str(exc),
                            "status": "error",
                        }, timeout=10.0)
                except Exception:
                    logger.warning(f"[chat/async] Error callback to {callback_url} also failed")

        background_tasks.add_task(_run_and_callback)
        return AsyncChatAccepted(chat_id=chat_id)

    return router
