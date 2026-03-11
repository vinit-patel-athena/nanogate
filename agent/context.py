"""Dynamic context tracking for execution across the nanogate agent."""

from contextvars import ContextVar
from typing import Any

# Explicit tracking of the current active session ID making a request.
# This prevents race conditions during concurrent execution of custom tools hooked to a shared AgentLoop.
ACTIVE_SESSION: ContextVar[str | None] = ContextVar("nanogate_active_session", default=None)

# Thread-safe queueing to pass approval requests back up to the frontend API smoothly over concurrent LLM loops
APPROVAL_REQUEST_ID: ContextVar[str | None] = ContextVar("nanogate_approval_request_id", default=None)

# Human-readable context about the pending approval so the frontend can display what needs approval.
# Populated by custom tools before they pause execution.
# Example: {"tool": "exec", "command": "ls -la /etc", "description": "List files in /etc"}
APPROVAL_CONTEXT: ContextVar[dict[str, Any] | None] = ContextVar("nanogate_approval_context", default=None)
