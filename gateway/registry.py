from __future__ import annotations

import os
from contextvars import ContextVar, Token
from typing import Any

from gateway.docker_manager import DockerManager, TenantContainer
from nanogate.bus import RedisMessageBus

ACTIVE_TENANT: ContextVar[str | None] = ContextVar("active_tenant", default=None)


def set_active_tenant(tenant_id: str) -> Token:
    return ACTIVE_TENANT.set(tenant_id)


def reset_active_tenant(token: Token) -> None:
    ACTIVE_TENANT.reset(token)


DEFAULT_TENANT_ID = os.environ.get("TENANT_ID", "default")


class AgentRegistry:
    """Manages Docker container instances and Message Bus keyed by tenant_id."""

    def __init__(self) -> None:
        redis_url = os.environ.get("NANOGATE_REDIS_URL", "redis://localhost:6379")
        self.manager = DockerManager(redis_url=redis_url)
        self.message_bus = RedisMessageBus(redis_url)

    def get_tenant_container(self, tenant_id: str) -> TenantContainer | None:
        """Get the container mapping for a tenant."""
        return self.manager.get_tenant(tenant_id)

    def provision_tenant(self, tenant_id: str, config_data: dict[str, Any]) -> TenantContainer:
        """Provision a new Docker container for the given tenant config."""
        return self.manager.provision_tenant(tenant_id=tenant_id, config_data=config_data)

    def install_hook(self) -> None:
        """No-op for the gateway: hooking happens inside the isolated Docker container."""
        pass
        
    async def get_or_create(self, tenant_id: str) -> Any:
        # Check if tenant exists (in memory or still running); if not, resume from Redis or provision with default config
        tc = self.get_tenant_container(tenant_id)
        if not tc:
            saved_config = self.manager.get_saved_state(tenant_id)
            config = saved_config if saved_config else {}
            tc = self.provision_tenant(tenant_id, config)
        return tc

    async def shutdown_all(self) -> None:
        await self.message_bus.close()
        self.manager.shutdown_all()
