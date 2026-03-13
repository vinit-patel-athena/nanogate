from __future__ import annotations

import json
import logging
import os
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

try:
    import redis
except ImportError:
    redis = None

# Redis key prefix for container state (persisted so we can resume after stop/restart)
CONTAINER_STATE_KEY_PREFIX = "nanogate:containers:state:"


class TenantContainer(BaseModel):
    tenant_id: str
    container_id: str
    port: int
    config_dir: str
    last_activity: float = Field(default_factory=time.time)


class TenantState(BaseModel):
    """Persisted state for a tenant (in Redis). Used to resume container after stop or gateway restart."""
    tenant_id: str
    container_id: str = ""
    port: int = 0
    config_dir: str = ""
    config_data: dict[str, Any] = Field(default_factory=dict)
    last_activity: float = Field(default_factory=time.time)
    status: str = "stopped"  # "running" | "stopped"


def find_free_port() -> int:
    """Find a random free port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


class DockerManager:
    """Orchestrates Nanobot containers per tenant. State is persisted in Redis for resume after stop/restart."""

    DEFAULT_IMAGE = "nanogate:latest"

    def __init__(
        self,
        base_dir: str = "/tmp/nanogate/tenants",
        redis_url: str | None = None,
    ) -> None:
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._client = docker.from_env() if docker else None
        if not self._client:
            logger.warning("Docker SDK is missing or Docker is not running. Using mock manager.")
        self._tenants: dict[str, TenantContainer] = {}
        self._redis_url = redis_url or os.environ.get("NANOGATE_REDIS_URL", "redis://localhost:6379")
        self._redis: redis.Redis | None = None
        if redis:
            try:
                self._redis = redis.from_url(self._redis_url, decode_responses=True)
                self._redis.ping()
                self._reload_running_from_redis()
            except Exception as e:
                logger.warning("Redis not available for container state: %s. State will not persist.", e)
                self._redis = None
        self.ensure_image()

        # Background pruning loop for idle containers
        self._stop_event = threading.Event()
        self._prune_thread = threading.Thread(target=self._prune_loop, name="container-pruner", daemon=True)
        self._prune_thread.start()

    def _state_key(self, tenant_id: str) -> str:
        return f"{CONTAINER_STATE_KEY_PREFIX}{tenant_id}"

    def _save_state(self, state: TenantState) -> None:
        """Persist tenant state to Redis so it can be resumed after stop or gateway restart."""
        if not self._redis:
            return
        try:
            key = self._state_key(state.tenant_id)
            self._redis.set(key, state.model_dump_json())
        except Exception as e:
            logger.warning("Failed to save container state to Redis for %s: %s", state.tenant_id, e)

    def _load_state(self, tenant_id: str) -> TenantState | None:
        """Load persisted tenant state from Redis."""
        if not self._redis:
            return None
        try:
            key = self._state_key(tenant_id)
            data = self._redis.get(key)
            if data:
                return TenantState.model_validate_json(data)
        except Exception as e:
            logger.debug("Failed to load container state from Redis for %s: %s", tenant_id, e)
        return None

    def _reload_running_from_redis(self) -> None:
        """On startup, restore in-memory _tenants for any tenant that has status=running and container still exists."""
        if not self._redis or not self._client:
            return
        try:
            keys = self._redis.keys(f"{CONTAINER_STATE_KEY_PREFIX}*")
            for key in keys or []:
                tenant_id = key.replace(CONTAINER_STATE_KEY_PREFIX, "")
                data = self._redis.get(key)
                if not data:
                    continue
                state = TenantState.model_validate_json(data)
                if state.status != "running" or not state.container_id:
                    continue
                try:
                    self._client.containers.get(state.container_id)
                except Exception:
                    # Container no longer exists; mark as stopped in Redis
                    state.status = "stopped"
                    state.container_id = ""
                    self._save_state(state)
                    continue
                tc = TenantContainer(
                    tenant_id=state.tenant_id,
                    container_id=state.container_id,
                    port=state.port,
                    config_dir=state.config_dir,
                    last_activity=state.last_activity,
                )
                self._tenants[tenant_id] = tc
                logger.info("Restored tenant %s from Redis (container %s)", tenant_id, state.container_id[:12])
        except Exception as e:
            logger.warning("Failed to reload running containers from Redis: %s", e)

    def get_saved_state(self, tenant_id: str) -> dict[str, Any] | None:
        """Return saved config_data for a tenant if any (for resuming). Returns None if no saved state or no config."""
        state = self._load_state(tenant_id)
        if state and state.config_data:
            return state.config_data
        return None

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
        """Resolve tenant_id to container. Redis is the source of truth; _tenants is cache."""
        tc = self._tenants.get(tenant_id)
        if tc:
            tc.last_activity = time.time()
            return tc
        # Cache miss: try Redis (tenant may have been started by another process or before restart)
        state = self._load_state(tenant_id)
        if not state or state.status != "running" or not state.container_id:
            return None
        if not self._client:
            return None
        try:
            self._client.containers.get(state.container_id)
        except Exception:
            # Container no longer exists; mark stopped in Redis
            state.status = "stopped"
            state.container_id = ""
            self._save_state(state)
            return None
        tc = TenantContainer(
            tenant_id=state.tenant_id,
            container_id=state.container_id,
            port=state.port,
            config_dir=state.config_dir,
            last_activity=state.last_activity,
        )
        self._tenants[tenant_id] = tc
        return tc

    def touch(self, tenant_id: str) -> None:
        """Update last activity timestamp for a tenant (in memory and in Redis)."""
        if tc := self._tenants.get(tenant_id):
            tc.last_activity = time.time()
            if self._redis:
                state = self._load_state(tenant_id)
                if state and state.status == "running":
                    state.last_activity = tc.last_activity
                    self._save_state(state)

    def _prune_loop(self) -> None:
        """Periodically remove idle containers. Uses Redis as source of running tenants and last_activity."""
        idle_timeout = 1800  # 30 minutes
        while not self._stop_event.wait(timeout=60):
            try:
                now = time.time()
                to_prune: list[str] = []
                if self._redis:
                    keys = self._redis.keys(f"{CONTAINER_STATE_KEY_PREFIX}*")
                    for key in keys or []:
                        tenant_id = key.replace(CONTAINER_STATE_KEY_PREFIX, "")
                        state = self._load_state(tenant_id)
                        if state and state.status == "running" and (now - state.last_activity > idle_timeout):
                            to_prune.append(tenant_id)
                else:
                    for tid in list(self._tenants.keys()):
                        tc = self._tenants.get(tid)
                        if tc and (now - tc.last_activity > idle_timeout):
                            to_prune.append(tid)
                for tid in to_prune:
                    logger.info("Pruning idle tenant container: %s", tid)
                    self.stop_tenant(tid)
            except Exception as e:
                logger.error("Error in container pruning loop: %s", e)

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
        # Use config workspace only if it is a host path (~ or not the container path /root/...)
        if user_workspace and not str(user_workspace).strip().startswith("/root/"):
            host_workspace = Path(user_workspace).expanduser().resolve()
        else:
            host_workspace = tenant_dir / "workspace"
            
        host_workspace.mkdir(parents=True, exist_ok=True)
        
        # Setting workspace path to a mounted location inside the container
        config_data["agents"]["defaults"]["workspace"] = "/root/.nanobot/workspace"

        config_path.write_text(json.dumps(config_data, indent=2))
        
        return tenant_dir, host_workspace

    def stop_tenant(self, tenant_id: str) -> None:
        """Stop and remove a tenant's container; persist state to Redis before bringing down so we can resume later.
        Works whether the tenant is in _tenants (this process) or only in Redis (e.g. another gateway or after restart).
        """
        tc = self._tenants.pop(tenant_id, None)
        loaded_state: TenantState | None = None
        if tc is None:
            loaded_state = self._load_state(tenant_id)
            if not loaded_state or loaded_state.status != "running" or not loaded_state.container_id:
                return
            tc = TenantContainer(
                tenant_id=loaded_state.tenant_id,
                container_id=loaded_state.container_id,
                port=loaded_state.port,
                config_dir=loaded_state.config_dir or "",
                last_activity=loaded_state.last_activity,
            )
        config_data: dict[str, Any] = {}
        if tc.config_dir:
            config_path = Path(tc.config_dir) / "config.json"
            if config_path.exists():
                try:
                    config_data = json.loads(config_path.read_text())
                except Exception as e:
                    logger.warning("Could not read config for state save: %s", e)
        if not config_data and loaded_state:
            config_data = loaded_state.config_data
        if not config_data:
            s = self._load_state(tenant_id)
            if s:
                config_data = s.config_data
        state = TenantState(
            tenant_id=tenant_id,
            container_id="",
            port=tc.port,
            config_dir=tc.config_dir,
            config_data=config_data,
            last_activity=tc.last_activity,
            status="stopped",
        )
        self._save_state(state)
        if self._client:
            try:
                container = self._client.containers.get(tc.container_id)
                container.stop(timeout=5)
                container.remove()
                logger.info(
                    "Stopped and removed container %s for tenant %s (state saved to Redis)",
                    tc.container_id[:12], tenant_id,
                )
            except docker.errors.NotFound:
                logger.info("Container %s for tenant %s already gone", tc.container_id[:12], tenant_id)
            except Exception as e:
                logger.error("Error removing container for tenant %s: %s", tenant_id, e)

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
        container_name = f"nanogate-tenant-{tenant_id}"
        
        # Remove old container if conflicting name
        try:
            old_c = self._client.containers.get(container_name)
            old_c.remove(force=True)
        except docker.errors.NotFound:
            pass

        logger.info(f"Starting {container_name} in bus mode...")
        
        # Extract any custom environment variables mapped for the gateway
        gateway_config = config_data.get("gateway", {})
        custom_env = gateway_config.get("env", {}) or {}
        
        # Inject bus settings
        custom_env["TENANT_ID"] = tenant_id
        if "REDIS_URL" not in custom_env:
            # Try to resolve host IP for gateway-to-host-redis communication from within container
            custom_env["REDIS_URL"] = os.environ.get("NANOGATE_REDIS_URL", "redis://host.docker.internal:6379")
        
        # OpenAI key: prefer tenant config (config.providers.openai.apiKey), then gateway env
        if "OPENAI_API_KEY" not in custom_env:
            openai_key = (
                (config_data.get("providers") or {}).get("openai") or {}
            ).get("apiKey") or os.environ.get("OPENAI_API_KEY")
            if openai_key:
                custom_env["OPENAI_API_KEY"] = openai_key
        
        # Build volume mounts
        volumes = {
            str(tenant_dir): {"bind": "/root/.nanobot", "mode": "rw"},
            str(host_workspace): {"bind": "/root/.nanobot/workspace", "mode": "rw"},
        }
        
        # ... (rest of volume mounting logic)
        tools_dir = gateway_config.get("toolsDir")
        if tools_dir:
            host_tools = Path(tools_dir).expanduser().resolve()
            if host_tools.is_dir():
                volumes[str(host_tools)] = {"bind": "/app/tenant_tools", "mode": "ro"}

        scripts_dir = gateway_config.get("scriptsDir")
        if scripts_dir:
            host_scripts = Path(scripts_dir).expanduser().resolve()
            if host_scripts.is_dir():
                volumes[str(host_scripts)] = {"bind": "/app/tenant_scripts", "mode": "ro"}
        
        # Publish container 8765 to host so approval and tenant proxy can reach the agent
        container = self._client.containers.run(
            image,
            name=container_name,
            detach=True,
            ports={"8765/tcp": port},
            environment=custom_env,
            volumes=volumes,
            command="python -m agent.server",
        )

        tc = TenantContainer(
            tenant_id=tenant_id,
            container_id=container.id,
            port=port,
            config_dir=str(tenant_dir),
        )
        self._tenants[tenant_id] = tc
        state = TenantState(
            tenant_id=tenant_id,
            container_id=container.id,
            port=port,
            config_dir=str(tenant_dir),
            config_data=config_data,
            last_activity=time.time(),
            status="running",
        )
        self._save_state(state)

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
