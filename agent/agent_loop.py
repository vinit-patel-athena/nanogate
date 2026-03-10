"""Build an AgentLoop from nanobot config.

Imports only from nanobot core (agent, config, cli) — never from nanobot.gateway.
"""

from __future__ import annotations

from nanobot.config import get_data_dir, load_config
from nanobot.utils.helpers import sync_workspace_templates


def create_agent_loop(client_id: str | None = None, approval_hook=None):
    """Build AgentLoop for the agent server from config.

    When *approval_hook* is provided the built-in ``exec`` tool is replaced
    with a :class:`GatewayExecTool` that runs the hook **at the tool level**.
    This keeps tool-call → tool-result → assistant-response intact in session
    history so the LLM does not try to mimic approval messages on follow-ups.
    """
    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.queue import MessageBus
    from nanobot.cli.commands import _make_provider
    from nanobot.cron.service import CronService
    from nanobot.session.manager import SessionManager

    config = load_config()
    sync_workspace_templates(config.workspace_path)
    bus = MessageBus()
    provider = _make_provider(config)
    cron_store_path = get_data_dir() / "cron" / "jobs.json"
    cron = CronService(cron_store_path)
    session_manager = SessionManager(config.workspace_path)
    model = config.agents.defaults.model

    loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=config.workspace_path,
        model=model,
        temperature=config.agents.defaults.temperature,
        max_tokens=config.agents.defaults.max_tokens,
        max_iterations=config.agents.defaults.max_tool_iterations,
        memory_window=config.agents.defaults.memory_window,
        reasoning_effort=config.agents.defaults.reasoning_effort,
        brave_api_key=config.tools.web.search.api_key or None,
        web_proxy=config.tools.web.proxy or None,
        exec_config=config.tools.exec,
        cron_service=cron,
        restrict_to_workspace=config.tools.restrict_to_workspace,
        session_manager=session_manager,
        mcp_servers=config.tools.mcp_servers,
        channels_config=config.channels,
    )

    if approval_hook is not None:
        from agent.exec_tool import GatewayExecTool

        original_exec = loop.tools.get("exec")
        if original_exec is not None:
            loop.tools.register(GatewayExecTool(original_exec, approval_hook))

    return loop
