from __future__ import annotations

import os
from contextvars import ContextVar, Token
from typing import Any

from gateway.docker_manager import DockerManager, TenantContainer

ACTIVE_TENANT: ContextVar[str | None] = ContextVar("active_tenant", default=None)


def set_active_tenant(tenant_id: str) -> Token:
    return ACTIVE_TENANT.set(tenant_id)


def reset_active_tenant(token: Token) -> None:
    ACTIVE_TENANT.reset(token)


DEFAULT_TENANT_ID = os.environ.get("TENANT_ID", "default")


class AgentRegistry:
    """Manages Docker container instances keyed by tenant_id."""

    def __init__(self) -> None:
        self.manager = DockerManager()

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
        # Backward compatibility for any straggling references, 
        # though the gateway shouldn't dynamically create blank environments 
        # without a config payload anymore.
        tc = self.get_tenant_container(tenant_id)
        if not tc:
            # Create with an empty dictionary just to spin up default nanobot
            tc = self.provision_tenant(tenant_id, {})
        return tc

    async def shutdown_all(self) -> None:
        self.manager.shutdown_all()
