from __future__ import annotations

import json
import logging
import socket
import time
import threading
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

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
    last_activity: float = Field(default_factory=time.time)


def find_free_port() -> int:
    """Find a random free port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


class DockerManager:
    """Orchestrates Nanobot containers per tenant."""

    DEFAULT_IMAGE = "nanogate:latest"

    def __init__(self, base_dir: str = "/tmp/nanogate/tenants") -> None:
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._client = docker.from_env() if docker else None
        if not self._client:
            logger.warning("Docker SDK is missing or Docker is not running. Using mock manager.")
        self._tenants: dict[str, TenantContainer] = {}
        self.ensure_image()

        # Background pruning loop for idle containers
        self._stop_event = threading.Event()
        self._prune_thread = threading.Thread(target=self._prune_loop, name="container-pruner", daemon=True)
        self._prune_thread.start()

    def ensure_image(self, image: str | None = None) -> None:
        """Build the nanogate Docker image if it doesn't exist."""
        if not self._client:
            return
        image = image or self.DEFAULT_IMAGE
        try:
            self._client.images.get(image)
            logger.info(f"Docker image '{image}' found.")
        except docker.errors.ImageNotFound:
            project_root = Path(__file__).parent.parent
            dockerfile = project_root / "Dockerfile"
            if not dockerfile.is_file():
                logger.warning(f"Image '{image}' not found and no Dockerfile at {dockerfile}.")
                return
            logger.info(f"Image '{image}' not found. Building from {project_root}...")
            self._client.images.build(path=str(project_root), tag=image, rm=True)
            logger.info(f"Image '{image}' built successfully.")

    def get_tenant(self, tenant_id: str) -> TenantContainer | None:
        tc = self._tenants.get(tenant_id)
        if tc:
            tc.last_activity = time.time()
        return tc

    def touch(self, tenant_id: str) -> None:
        """Update last activity timestamp for a tenant."""
        if tc := self._tenants.get(tenant_id):
            tc.last_activity = time.time()

    def _prune_loop(self) -> None:
        """Periodically remove containers that haven't been touched in a while (e.g., 30 mins)."""
        idle_timeout = 1800 # 30 minutes
        while not self._stop_event.wait(timeout=60):
            try:
                now = time.time()
                to_prune = []
                
                # Use local copy of keys to avoid concurrent modification issues
                for tid in list(self._tenants.keys()):
                    tc = self._tenants.get(tid)
                    if tc and (now - tc.last_activity > idle_timeout):
                        to_prune.append(tid)
                
                for tid in to_prune:
                    logger.info(f"Pruning idle tenant container: {tid}")
                    self.stop_tenant(tid)
            except Exception as e:
                logger.error(f"Error in container pruning loop: {e}")

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
        self, tenant_id: str, config_data: dict[str, Any], image: str = "nanogate:latest"
    ) -> TenantContainer:
        """Provision a new Docker container for the given tenant config."""
        if not self._client:
            raise RuntimeError("Docker SDK is not initialized")
            
        self.stop_tenant(tenant_id)

        tenant_dir, host_workspace = self.write_config(tenant_id, config_data)
        port = find_free_port()
        
        # Ensure image exists (built at startup, but check again as fallback)
        self.ensure_image(image)

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
        
        # Extract any custom environment variables mapped for the gateway
        gateway_config = config_data.get("gateway", {})
        custom_env = gateway_config.get("env", {})
        
        # Build volume mounts
        volumes = {
            str(tenant_dir): {"bind": "/root/.nanobot", "mode": "rw"},
            str(host_workspace): {"bind": "/root/.nanobot/workspace", "mode": "rw"},
        }
        
        # Mount tenant-provided tools directory if specified
        tools_dir = gateway_config.get("toolsDir")
        if tools_dir:
            host_tools = Path(tools_dir).expanduser().resolve()
            if not host_tools.is_dir():
                raise RuntimeError(
                    f"gateway.toolsDir '{tools_dir}' does not exist or is not a directory."
                )
            volumes[str(host_tools)] = {"bind": "/app/tenant_tools", "mode": "ro"}
            logger.info(f"[{tenant_id}] Mounting toolsDir: {host_tools} -> /app/tenant_tools")

        # Mount tenant-provided scripts directory if specified
        scripts_dir = gateway_config.get("scriptsDir")
        if scripts_dir:
            host_scripts = Path(scripts_dir).expanduser().resolve()
            if not host_scripts.is_dir():
                raise RuntimeError(
                    f"gateway.scriptsDir '{scripts_dir}' does not exist or is not a directory."
                )
            volumes[str(host_scripts)] = {"bind": "/app/tenant_scripts", "mode": "ro"}
            logger.info(f"[{tenant_id}] Mounting scriptsDir: {host_scripts} -> /app/tenant_scripts")
        
        container = self._client.containers.run(
            image,
            name=container_name,
            detach=True,
            ports={"8765/tcp": port},
            environment=custom_env,
            volumes=volumes,
            command="python -m agent.server" # Starts the single-tenant agent server inside the container
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
        """Stop all managed containers and pruning thread."""
        self._stop_event.set()
        for tenant_id in list(self._tenants.keys()):
            self.stop_tenant(tenant_id)
