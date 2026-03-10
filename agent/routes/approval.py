"""Approval route for the single-tenant agent server."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from agent.tool_gateway import ToolGateway, truncate_text


class ApproveBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    request_id: str
    session_id: str | None = Field(default=None, alias="sessionId")
    auto_resume: bool = Field(default=True, alias="autoResume")
    resume_on_failure: bool = Field(default=False, alias="resumeOnFailure")


def _load_exec_settings() -> tuple[int, str]:
    config_path = Path.home() / ".nanobot" / "config.json"
    timeout, path_append = 60, ""
    if config_path.is_file():
        try:
            data = json.loads(config_path.read_text())
            exec_cfg = (data.get("tools") or {}).get("exec") or {}
            timeout = int(exec_cfg.get("timeout", 60))
            path_append = str(exec_cfg.get("pathAppend", ""))
        except Exception:
            pass
    return timeout, path_append


def build_approval_router(get_agent: Callable, tool_gateway: ToolGateway) -> APIRouter:
    router = APIRouter()

    @router.post("/approve")
    async def approve(payload: ApproveBody = Body(...)) -> dict[str, Any]:
        if payload.request_id not in tool_gateway.pending:
            raise HTTPException(
                status_code=400,
                detail=f"No pending approval for request_id: {payload.request_id}",
            )

        timeout, path_append = _load_exec_settings()
        success, output, exit_code, pending = await tool_gateway.run_approved(
            payload.request_id, timeout=timeout, path_append=path_append
        )
        if not success:
            raise HTTPException(status_code=400, detail=output)

        response: dict[str, Any] = {
            "ok": True,
            "output": output,
            "exit_code": exit_code,
        }
        if pending:
            response["approved_command"] = str(pending.get("command", ""))

        agent_loop = get_agent()
        should_resume = payload.auto_resume and (exit_code == 0 or payload.resume_on_failure)
        if should_resume and agent_loop is not None and pending:
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
