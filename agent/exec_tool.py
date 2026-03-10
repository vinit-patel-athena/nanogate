"""Gateway exec tool wrapper: intercepts shell commands for approval.

Wraps the nanobot ExecTool so that blocked commands appear as tool results
in session history, preserving the natural tool-call → result → response
pattern.
"""

from __future__ import annotations

import os
from contextvars import ContextVar, Token
from typing import Any, Awaitable, Callable

from nanobot.agent.tools.base import Tool

# (command, cwd, context) -> (proceed, message, extra_env)
ExecApprovalHook = Callable[
    [str, str, dict[str, str]],
    Awaitable[tuple[bool, str, dict[str, str]]],
]

EXEC_CONTEXT: ContextVar[dict[str, str] | None] = ContextVar("agent_exec_context", default=None)
APPROVAL_REQUEST_ID: ContextVar[str | None] = ContextVar("agent_approval_request_id", default=None)


def set_exec_context(context: dict[str, str] | None) -> Token:
    return EXEC_CONTEXT.set(context)


def reset_exec_context(token: Token) -> None:
    EXEC_CONTEXT.reset(token)


class GatewayExecTool(Tool):
    """Drop-in replacement for ExecTool that runs an approval hook before
    executing.  When the hook blocks a command the tool returns an error-style
    result (visible to the LLM as a normal tool result), keeping the session
    history clean.
    """

    def __init__(self, inner: Tool, approval_hook: ExecApprovalHook):
        self._inner = inner
        self._approval_hook = approval_hook

    @property
    def name(self) -> str:
        return "exec"

    @property
    def description(self) -> str:
        return self._inner.description

    @property
    def parameters(self) -> dict[str, Any]:
        return self._inner.parameters

    async def execute(self, command: str = "", working_dir: str | None = None, **kwargs: Any) -> str:
        cwd = working_dir or getattr(self._inner, "working_dir", None) or os.getcwd()
        ctx = EXEC_CONTEXT.get() or {}
        proceed, message, _extra_env = await self._approval_hook(command, cwd, ctx)
        if not proceed:
            return message
        return await self._inner.execute(command=command, working_dir=working_dir, **kwargs)

    def get_default_model(self) -> str:  # type: ignore[override]
        return getattr(self._inner, "get_default_model", lambda: "")()
