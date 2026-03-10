"""Chat route for the single-tenant agent server."""

from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from agent.exec_tool import (
    APPROVAL_REQUEST_ID,
    reset_exec_context,
    set_exec_context,
)
from agent.tool_gateway import ToolGateway


class ChatRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    message: str
    session_id: str | None = Field(default="api:direct", alias="sessionId")


class ChatResponse(BaseModel):
    response: str
    approval_request_id: str | None = None


def build_chat_router(get_agent: Callable, tool_gateway: ToolGateway) -> APIRouter:
    router = APIRouter()

    @router.post("/chat", response_model=ChatResponse)
    async def chat(body: ChatRequest) -> ChatResponse:
        agent_loop = get_agent()
        if agent_loop is None:
            raise HTTPException(status_code=503, detail="Agent not initialized")
        if not body.message or not body.message.strip():
            raise HTTPException(status_code=400, detail="message must be non-empty")

        raw = getattr(body, "session_id", None)
        session_id = (raw.strip() if isinstance(raw, str) and raw else None) or "api:direct"
        if ":" not in session_id:
            session_id = f"api:{session_id}"

        message = body.message.strip()
        cfg: dict[str, Any] = tool_gateway.config
        if cfg.get("enabled") and cfg.get("requireApprovalForApi"):
            message += (
                "\n\n[Proceed with the requested action now. "
                "Do not stop for prerequisite checklists first. "
                "If the action requires a shell command, use the exec tool.]"
            )

        chat_id = session_id.split(":", 1)[-1]
        exec_ctx = {
            "session_key": session_id,
            "channel": "api",
            "chat_id": chat_id,
        }
        token = set_exec_context(exec_ctx)
        try:
            response = await agent_loop.process_direct(
                message,
                session_key=session_id,
                channel="api",
                chat_id=chat_id,
            )
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e)) from e
        finally:
            reset_exec_context(token)

        approval_request_id: str | None = None
        try:
            approval_request_id = APPROVAL_REQUEST_ID.get()
            if approval_request_id is not None:
                APPROVAL_REQUEST_ID.set(None)
        except LookupError:
            pass

        if approval_request_id:
            response = (
                f"Approval required. To approve, send POST /api/approve with: "
                f"{{\"request_id\": \"{approval_request_id}\", \"sessionId\": \"{session_id}\"}}"
            )

        return ChatResponse(response=response or "", approval_request_id=approval_request_id)

    return router
