from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel, ConfigDict, Field

def truncate_text(text: str, max_chars: int = 4000) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n... (truncated {len(text) - max_chars} chars)"

class ApproveBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    request_id: str
    session_id: str | None = Field(default=None, alias="sessionId")
    auto_resume: bool = Field(default=True, alias="autoResume")
    resume_on_failure: bool = Field(default=False, alias="resumeOnFailure")


def build_approval_router(get_agent: Callable) -> APIRouter:
    router = APIRouter()

    @router.get("/approvals/pending")
    async def list_pending_approvals() -> list[dict[str, Any]]:
        """Return all pending approval requests across all registered tools.
        
        Each item includes the request_id, which tool owns it, and any context
        the tool provided (e.g. the command being requested).
        """
        agent_loop = get_agent()
        if not agent_loop:
            raise HTTPException(status_code=503, detail="Agent not initialized")

        pending_approvals: list[dict[str, Any]] = []
        for name in agent_loop.tools.tool_names:
            tool = agent_loop.tools.get(name)
            if hasattr(tool, "pending"):
                for request_id, info in getattr(tool, "pending", {}).items():
                    pending_approvals.append({
                        "request_id": request_id,
                        "tool": name,
                        "session_id": info.get("session_key"),
                        "command": info.get("command"),
                        "cwd": info.get("cwd"),
                        "description": f"Execute shell command: `{info.get('command')}`",
                    })
        return pending_approvals

    @router.post("/approve")

    async def approve(payload: ApproveBody = Body(...)) -> dict[str, Any]:
        agent_loop = get_agent()
        if not agent_loop:
            raise HTTPException(status_code=503, detail="Agent not initialized")

        # Dynamically find which custom tool plugin currently owns this pending approval request 
        approved_tool = None
        for name in agent_loop.tools.tool_names:
            tool = agent_loop.tools.get(name)
            if hasattr(tool, "run_approved") and hasattr(tool, "pending"):
                if payload.request_id in getattr(tool, "pending", {}):
                    approved_tool = tool
                    break

        if not approved_tool:
            raise HTTPException(
                status_code=400,
                detail=f"No pending approval found across any custom tools for request_id: {payload.request_id}",
            )

        # Resume the specific tool's execution natively
        success, output, exit_code, pending = await approved_tool.run_approved(payload.request_id)
        
        if not success:
            raise HTTPException(status_code=400, detail=output)

        response: dict[str, Any] = {
            "ok": True,
            "output": output,
            "exit_code": exit_code,
        }
        if pending:
            response["approved_command"] = str(pending.get("command", ""))

        should_resume = payload.auto_resume and (exit_code == 0 or payload.resume_on_failure)
        if should_resume and pending:
            command = str(pending.get("command", ""))
            session_key = str(pending.get("session_key", "api:direct"))
            channel = str(pending.get("channel", "api"))
            chat_id = str(pending.get("chat_id", "direct"))

            provided_session = (
                payload.session_id.strip()
                if isinstance(payload.session_id, str) and payload.session_id.strip()
                else None
            )
            if provided_session:
                normalized = provided_session if ":" in provided_session else f"api:{provided_session}"
                if normalized != session_key:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"sessionId mismatch for request_id {payload.request_id}: "
                            f"expected {session_key}, got {normalized}"
                        ),
                    )

            response["session_id"] = session_key
            resume_message = (
                "An approved command has been executed. Continue from where you paused.\n\n"
                f"Approved command:\n{command}\n\n"
                f"Exit code: {exit_code}\n"
                f"Command output:\n{truncate_text(output)}"
            )

            try:
                agent_response = await agent_loop.process_direct(
                    resume_message,
                    session_key=session_key,
                    channel=channel,
                    chat_id=chat_id,
                )
                response["agent_response"] = agent_response or ""
                response["resumed"] = True
            except Exception as e:
                response["resumed"] = False
                response["resume_error"] = str(e)
        else:
            response["resumed"] = False
            if pending:
                response["session_id"] = str(pending.get("session_key", "api:direct"))
            if payload.auto_resume and exit_code != 0 and not payload.resume_on_failure:
                response["resume_skipped"] = (
                    "Approved command failed; automatic resume skipped to avoid retry loops. "
                    "Set resumeOnFailure=true to force resume on failures."
                )

        return response

    return router
