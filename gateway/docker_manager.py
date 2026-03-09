from __future__ import annotations

import json
import logging
import socket
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel

logger = logging.getLogger(__name__)

try:
    import docker
except ImportError:
    docker = None


class TenantContainer(BaseModel):
    tenant_id: str
    container_id: str
    port: int
    config_dir: str


def find_free_port() -> int:
    """Find a random free port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


class DockerManager:
    """Orchestrates Nanobot containers per tenant."""

    def __init__(self, base_dir: str = "/tmp/nanogate/tenants") -> None:
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._client = docker.from_env() if docker else None
        if not self._client:
            logger.warning("Docker SDK is missing or Docker is not running. Using mock manager.")
        self._tenants: dict[str, TenantContainer] = {}

    def get_tenant(self, tenant_id: str) -> TenantContainer | None:
        return self._tenants.get(tenant_id)

    def write_config(self, tenant_id: str, config_data: dict[str, Any]) -> tuple[Path, Path]:
        """Write a nanobot config.json for a specific tenant."""
        tenant_dir = self.base_dir / tenant_id
        tenant_dir.mkdir(parents=True, exist_ok=True)
        config_path = tenant_dir / "config.json"
        
        # Merge basic settings ensuring it acts as a server
        if "gateway" not in config_data:
            config_data["gateway"] = {}
        config_data["gateway"]["port"] = 8765
        
        # Override workspace specifically into the mounted path
        if "agents" not in config_data:
            config_data["agents"] = {}
        if "defaults" not in config_data["agents"]:
            config_data["agents"]["defaults"] = {}
            
        user_workspace = config_data["agents"]["defaults"].get("workspace")
        if user_workspace:
            host_workspace = Path(user_workspace).expanduser().resolve()
        else:
            host_workspace = tenant_dir / "workspace"
            
        host_workspace.mkdir(parents=True, exist_ok=True)
        
        # Setting workspace path to a mounted location inside the container
        config_data["agents"]["defaults"]["workspace"] = "/root/.nanobot/workspace"

        config_path.write_text(json.dumps(config_data, indent=2))
        
        return tenant_dir, host_workspace

    def stop_tenant(self, tenant_id: str) -> None:
        """Stop and remove a tenant's container."""
        if tenant_id in self._tenants:
            tc = self._tenants.pop(tenant_id)
            if self._client:
                try:
                    container = self._client.containers.get(tc.container_id)
                    container.stop(timeout=5)
                    container.remove()
                    logger.info(f"Stopped and removed container {tc.container_id} for tenant {tenant_id}")
                except Exception as e:
                    logger.error(f"Error removing container for tenant {tenant_id}: {e}")

    def provision_tenant(
        self, tenant_id: str, config_data: dict[str, Any], image: str = "hkuds/nanobot:latest"
    ) -> TenantContainer:
        """Provision a new Docker container for the given tenant config."""
        if not self._client:
            raise RuntimeError("Docker SDK is not initialized")
            
        self.stop_tenant(tenant_id)

        tenant_dir, host_workspace = self.write_config(tenant_id, config_data)
        port = find_free_port()
        
        # Ensure image exists locally
        try:
            self._client.images.get(image)
        except docker.errors.ImageNotFound:
            logger.info(f"Image {image} not found locally. Attempting to pull...")
            try:
                self._client.images.pull(image)
            except docker.errors.ImageNotFound:
                raise RuntimeError(
                    f"Docker image '{image}' could not be found locally or pulled from the registry. "
                    f"Please build it locally (e.g., `docker build -t {image} .`) or check the image name."
                )

        # Run container
        # Note: the container expects config at ~ /root/.nanobot/config.json 
        # based on nanobot default paths, or injected via NANOBOT_CONFIG_PATH. 
        container_name = f"nanogate-tenant-{tenant_id}"
        
        # Remove old container if conflicting name
        try:
            old_c = self._client.containers.get(container_name)
            old_c.remove(force=True)
        except docker.errors.NotFound:
            pass

        logger.info(f"Starting {container_name} on port {port}...")
        
        # Mount gateway scripts with the EXACT same absolute path as the host
        gateway_scripts = Path(__file__).parent / "scripts"
        host_scripts_path = str(gateway_scripts.resolve())
        container_scripts_path = host_scripts_path
        
        # Extract any custom environment variables mapped for the gateway
        gateway_config = config_data.get("gateway", {})
        custom_env = gateway_config.get("env", {})
        
        container = self._client.containers.run(
            image,
            name=container_name,
            detach=True,
            ports={"8765/tcp": port},
            environment=custom_env,
            volumes={
                str(tenant_dir): {"bind": "/root/.nanobot", "mode": "rw"},
                str(host_workspace): {"bind": "/root/.nanobot/workspace", "mode": "rw"},
                host_scripts_path: {"bind": container_scripts_path, "mode": "ro"}
            },
            command="python -m nanobot.gateway.server" # Starts the internal API Gateway inside the container
        )

        tc = TenantContainer(
            tenant_id=tenant_id,
            container_id=container.id,
            port=port,
            config_dir=str(tenant_dir)
        )
        self._tenants[tenant_id] = tc
        
        # Run any setupCommands defined in the config (e.g., npm install -g @googleworkspace/cli)
        gateway_config = config_data.get("gateway", {})
        setup_commands = gateway_config.get("setupCommands", [])
        if setup_commands:
            for cmd in setup_commands:
                logger.info(f"[{tenant_id}] Running setup command: {cmd}")
                exit_code, output = container.exec_run(cmd)
                if exit_code != 0:
                    logger.error(f"[{tenant_id}] Setup command failed ({exit_code}):\n{output.decode('utf-8')}")
                else:
                    logger.info(f"[{tenant_id}] Setup command succeeded:\n{output.decode('utf-8')}")
        
        # Brief healthcheck wait
        time.sleep(1) 
        return tc

    def shutdown_all(self) -> None:
        """Stop all managed containers."""
        for tenant_id in list(self._tenants.keys()):
            self.stop_tenant(tenant_id)
