"""Gateway-only exec approval as a custom tool plugin.
When the hook blocks a command the tool returns an error-style
result (visible to the LLM as a normal tool result), keeping the session
history clean.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from typing import Any

from nanobot.agent.tools.shell import ExecTool


class GatewayExecTool(ExecTool):
    """Custom ExecTool plugin that requires human approval for certain commands."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.pending: dict[str, dict[str, Any]] = {}
        self.token_env_var = "GOOGLE_WORKSPACE_CLI_TOKEN"

        # Read config directly inside the tool to ensure autonomy
        import json
        from pathlib import Path

        self.config: dict[str, Any] = {
            "enabled": os.environ.get("GATEWAY_ENABLED", "").lower() in ("1", "true", "yes"),
            "tokenProviderCommand": os.environ.get("GATEWAY_TOKEN_PROVIDER_CMD", "").strip(),
            "requireApprovalForApi": os.environ.get("GATEWAY_REQUIRE_APPROVAL_FOR_API", "").lower() in ("1", "true", "yes"),
        }
        config_path = Path.home() / ".nanobot" / "config.json"
        
        # Determine defaults for exec tool
        self.timeout = 60
        self.path_append = ""
        
        if config_path.is_file():
            try:
                data = json.loads(config_path.read_text())
                tg = (data.get("tools") or {}).get("toolGateway") or {}
                if isinstance(tg, dict):
                    self.config["enabled"] = self.config["enabled"] or tg.get("enabled", False)
                    self.config["tokenProviderCommand"] = self.config["tokenProviderCommand"] or tg.get("tokenProviderCommand", "")
                    self.config["requireApprovalForApi"] = self.config["requireApprovalForApi"] or tg.get("requireApprovalForApi", False)
                    
                exec_cfg = (data.get("tools") or {}).get("exec") or {}
                self.timeout = int(exec_cfg.get("timeout", 60))
                self.path_append = str(exec_cfg.get("pathAppend", ""))
            except Exception:
                pass

    @property
    def name(self) -> str:
        # Override the default "exec" tool
        return "exec"

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

    async def execute(self, command: str, working_dir: str | None = None, **kwargs: Any) -> str:
        cwd = working_dir or self.working_dir or os.getcwd()
        
        if self.config.get("enabled") and self.config.get("requireApprovalForApi"):
            # Block and request approval
            request_id = str(uuid.uuid4())
            
            # Extract session key from the active execution context loop
            try:
                from agent.context import ACTIVE_SESSION, APPROVAL_REQUEST_ID, APPROVAL_CONTEXT
                session_id = kwargs.get("session_key", ACTIVE_SESSION.get("api:direct"))
                APPROVAL_REQUEST_ID.set(request_id)
                APPROVAL_CONTEXT.set({
                    "tool": self.name,
                    "command": command,
                    "cwd": cwd or None,
                    "description": f"Execute shell command: `{command}`",
                })
            except (ImportError, LookupError):
                session_id = kwargs.get("session_key", "api:direct")
                
            print(f"DEBUG: execute() setting request_id {request_id} for session {session_id}")
            self.pending[request_id] = {
                "command": command,
                "cwd": cwd,
                "session_key": session_id,
                "channel": "api", 
                "chat_id": "direct",
            }
            
            return (
                f"Error: This command requires user approval before it can run. "
                f"The approval request has been forwarded to the client with request_id {request_id}. "
                "Do not retry — wait for the user to approve or give further instructions."
            )
            
        # Otherwise inject token if command is allowed directly
        env = os.environ.copy()
        if self.path_append:
            env["PATH"] = env.get("PATH", "") + os.pathsep + self.path_append
            
        token_cmd = self.config.get("tokenProviderCommand")
        if token_cmd:
            token = await self._run_token_provider(token_cmd)
            if token:
                env[self.token_env_var] = token

        return await self._run_subprocess(command, cwd, env)


    async def run_approved(
        self,
        request_id: str,
        timeout: int = 60,
        path_append: str = "",
    ) -> tuple[bool, str, int, dict[str, Any] | None]:
        # Called by the approval router
        pending = self.pending.pop(request_id, None)
        if not pending:
            return False, f"No pending approval for request_id: {request_id}", -1, None

        command = str(pending.get("command", ""))
        cwd = str(pending.get("cwd", ""))

        env = os.environ.copy()
        if path_append or self.path_append:
            env["PATH"] = env.get("PATH", "") + os.pathsep + (path_append or self.path_append)
            
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
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout or self.timeout)
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

    async def _run_subprocess(self, command: str, cwd: str, env: dict[str, str]) -> str:
        try:
            process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
            )
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=self.timeout)
            except asyncio.TimeoutError:
                process.kill()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    pass
                return f"Error: Command timed out after {self.timeout} seconds"

            output_parts = []
            if stdout:
                output_parts.append(stdout.decode("utf-8", errors="replace"))
            if stderr:
                stderr_text = stderr.decode("utf-8", errors="replace")
                if stderr_text.strip():
                    output_parts.append(f"STDERR:\n{stderr_text}")
            if process.returncode != 0:
                output_parts.append(f"\nExit code: {process.returncode}")

            result = "\n".join(output_parts) if output_parts else "(no output)"
            max_len = 10000
            if len(result) > max_len:
                result = result[:max_len] + f"\n... (truncated, {len(result) - max_len} more chars)"
            return result
        except Exception as e:
            return f"Error executing command: {str(e)}"
