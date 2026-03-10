"""Approval + token-injection gateway for intercepted shell commands."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path
from typing import Any


def truncate_text(text: str, max_chars: int = 4000) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n... (truncated {len(text) - max_chars} chars)"


class ToolGateway:
    """Approval + token-injection gateway for intercepted shell commands."""

    def __init__(self, *, command_prefix: str = "gws ", token_env_var: str = "GOOGLE_WORKSPACE_CLI_TOKEN") -> None:
        self.pending: dict[str, dict[str, Any]] = {}
        self.config: dict[str, Any] = self._load_config()
        self.command_prefix = command_prefix
        self.token_env_var = token_env_var

    @staticmethod
    def _load_config() -> dict[str, Any]:
        out: dict[str, Any] = {
            "enabled": os.environ.get("GATEWAY_ENABLED", "").lower() in ("1", "true", "yes"),
            "tokenProviderCommand": os.environ.get("GATEWAY_TOKEN_PROVIDER_CMD", "").strip(),
            "requireApprovalForApi": os.environ.get("GATEWAY_REQUIRE_APPROVAL_FOR_API", "").lower() in ("1", "true", "yes"),
        }
        config_path = Path.home() / ".nanobot" / "config.json"
        if config_path.is_file():
            try:
                data = json.loads(config_path.read_text())
                tg = (data.get("tools") or {}).get("toolGateway") or {}
                if isinstance(tg, dict):
                    out["enabled"] = out["enabled"] or tg.get("enabled", False)
                    out["tokenProviderCommand"] = out["tokenProviderCommand"] or tg.get("tokenProviderCommand", "")
                    out["requireApprovalForApi"] = out["requireApprovalForApi"] or tg.get("requireApprovalForApi", False)
            except Exception:
                pass
        return out

    async def _run_token_provider(self, cmd: str) -> str | None:
        try:
            process = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=15.0)
            if process.returncode == 0 and stdout:
                return stdout.decode("utf-8", errors="replace").strip() or None
        except Exception:
            pass
        return None

    async def hook(
        self,
        command: str,
        cwd: str,
        context: dict[str, str] | None = None,
    ) -> tuple[bool, str, dict[str, str]]:
        if not self.config.get("enabled") or not (command or "").strip().startswith(self.command_prefix):
            return True, "", {}

        if self.config.get("requireApprovalForApi"):
            request_id = str(uuid.uuid4())
            ctx = context or {}
            session_key = ctx.get("session_key") or "api:direct"
            from agent.exec_tool import APPROVAL_REQUEST_ID
            APPROVAL_REQUEST_ID.set(request_id)
            self.pending[request_id] = {
                "command": command,
                "cwd": cwd,
                "session_key": session_key,
                "channel": ctx.get("channel") or "api",
                "chat_id": ctx.get("chat_id") or "direct",
            }
            msg = (
                "Error: This command requires user approval before it can run. "
                "The approval request has been forwarded to the client. "
                "Do not retry — wait for the user to approve or give further instructions."
            )
            return False, msg, {}

        extra_env: dict[str, str] = {}
        token_cmd = (self.config.get("tokenProviderCommand") or "").strip()
        if token_cmd:
            token = await self._run_token_provider(token_cmd)
            if token:
                extra_env[self.token_env_var] = token
        return True, "", extra_env

    async def run_approved(
        self,
        request_id: str,
        timeout: int = 60,
        path_append: str = "",
    ) -> tuple[bool, str, int, dict[str, Any] | None]:
        pending = self.pending.pop(request_id, None)
        if not pending:
            return False, f"No pending approval for request_id: {request_id}", -1, None

        command = str(pending.get("command", ""))
        cwd = str(pending.get("cwd", ""))
        env = os.environ.copy()
        if path_append:
            env["PATH"] = env.get("PATH", "") + os.pathsep + path_append
        token_cmd = (self.config.get("tokenProviderCommand") or "").strip()
        if token_cmd:
            token = await self._run_token_provider(token_cmd)
            if token:
                env[self.token_env_var] = token

        try:
            process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
            out = stdout.decode("utf-8", errors="replace")
            if stderr:
                err = stderr.decode("utf-8", errors="replace")
                if err.strip():
                    out = out + "\nSTDERR:\n" + err
            if process.returncode != 0:
                out = out + f"\nExit code: {process.returncode}"
            return True, out or "(no output)", process.returncode, pending
        except asyncio.TimeoutError:
            return False, f"Command timed out after {timeout} seconds", -1, pending
        except Exception as e:
            return False, str(e), -1, pending
