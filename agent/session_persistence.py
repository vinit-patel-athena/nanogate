"""Persist nanobot session state to Redis so conversations can resume when container restarts."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nanogate.bus import RedisMessageBus

logger = logging.getLogger(__name__)

# Nanobot typically stores sessions under workspace/sessions/; try common patterns
SESSIONS_DIR = "sessions"


def _safe_session_filename(session_key: str) -> str:
    return (session_key or "default").replace(":", "_").replace("/", "_") + ".json"


async def save_session_state_to_redis(
    bus: RedisMessageBus,
    tenant_id: str,
    session_key: str,
    workspace_path: str | Path,
) -> None:
    """Read session file(s) from nanobot workspace and persist to Redis for resume after container restart."""
    try:
        base = Path(workspace_path)
        sessions_dir = base / SESSIONS_DIR
        if not sessions_dir.is_dir():
            return
        safe_name = _safe_session_filename(session_key)
        session_file = sessions_dir / safe_name
        if not session_file.is_file():
            # Try without .json or with different sanitization
            for f in sessions_dir.iterdir():
                if f.suffix in (".json", "") and session_key.replace(":", "_") in f.name:
                    session_file = f
                    break
            else:
                return
        raw = session_file.read_text()
        try:
            state = json.loads(raw)
        except json.JSONDecodeError:
            state = {"raw": raw}
        await bus.set_session_state(tenant_id, session_key, state)
        logger.debug("Saved session state to Redis for %s %s", tenant_id, session_key)
    except Exception as e:
        logger.debug("Could not save session state to Redis: %s", e)


async def load_session_state_from_redis(
    bus: RedisMessageBus,
    tenant_id: str,
    session_key: str,
    workspace_path: str | Path,
) -> bool:
    """Load session state from Redis and write to workspace so nanobot can resume the conversation."""
    try:
        state = await bus.get_session_state(tenant_id, session_key)
        if not state:
            return False
        base = Path(workspace_path)
        sessions_dir = base / SESSIONS_DIR
        sessions_dir.mkdir(parents=True, exist_ok=True)
        safe_name = _safe_session_filename(session_key)
        session_file = sessions_dir / safe_name
        if "raw" in state and isinstance(state["raw"], str):
            session_file.write_text(state["raw"])
        else:
            session_file.write_text(json.dumps(state, indent=2))
        logger.debug("Restored session state from Redis for %s %s", tenant_id, session_key)
        return True
    except Exception as e:
        logger.debug("Could not load session state from Redis: %s", e)
        return False
