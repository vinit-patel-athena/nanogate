"""Build an AgentLoop from nanobot config.

Imports only from nanobot core (agent, config, cli) — never from nanobot.gateway.
load_config() uses ~/.nanobot/config.json by default; the gateway mounts tenant
config at /root/.nanobot in the container, so the agent reads that file.
"""

from __future__ import annotations

from nanobot.config import get_data_dir, load_config
from nanobot.utils.helpers import sync_workspace_templates
from agent.plugin_loader import discover_tools


def create_agent_loop(client_id: str | None = None):
    """Build AgentLoop for the agent server from config."""
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
        max_iterations=config.agents.defaults.max_tool_iterations,
        context_window_tokens=config.agents.defaults.max_tokens, # Assuming this is the new equiv
        brave_api_key=config.tools.web.search.api_key or None,
        web_proxy=config.tools.web.proxy or None,
        exec_config=config.tools.exec,
        cron_service=cron,
        restrict_to_workspace=config.tools.restrict_to_workspace,
        session_manager=session_manager,
        mcp_servers=config.tools.mcp_servers,
        channels_config=config.channels,
    )

    # Auto-discover and register any custom python tools provided by the tenant
    for custom_tool in discover_tools():
        try:
            loop.tools.register(custom_tool)
        except Exception as e:
            # Continue loading other tools even if one fails
            import logging
            logging.getLogger(__name__).error(f"Failed to register custom tool {custom_tool}: {e}")

    return loop
