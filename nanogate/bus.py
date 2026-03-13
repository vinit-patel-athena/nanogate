from __future__ import annotations

import json
import logging
import uuid
from typing import Any, AsyncGenerator

import redis.asyncio as redis

logger = logging.getLogger(__name__)

# Constants for stream keys and keys
GLOBAL_OUTBOUND_STREAM = "nanogate:outbound:all"
SESSION_STREAM_PREFIX = "nanogate:session:"
TENANT_INBOUND_PREFIX = "nanogate:tenant:"
SESSION_STATE_KEY_PREFIX = "nanogate:tenant:"  # nanogate:tenant:{id}:session_state:{session_key}
SESSION_STATE_SUFFIX = "session_state"

class RedisMessageBus:
    def __init__(self, redis_url: str = "redis://localhost:6379"):
        self.redis = redis.from_url(redis_url, decode_responses=True)

    async def publish_request(self, tenant_id: str, payload: dict[str, Any]) -> str:
        """
        Publish a request to a specific tenant's inbound queue.
        Uses a List (LPUSH) for task queuing.
        """
        if "session_id" not in payload and "sessionId" not in payload:
            payload["session_id"] = str(uuid.uuid4())
            
        queue_key = f"{TENANT_INBOUND_PREFIX}{tenant_id}:inbound"
        await self.redis.lpush(queue_key, json.dumps(payload))
        return payload.get("session_id") or payload.get("sessionId")

    async def consume_request(self, tenant_id: str) -> dict[str, Any]:
        """
        Consume a request for a specific tenant.
        """
        queue_key = f"{TENANT_INBOUND_PREFIX}{tenant_id}:inbound"
        _, data = await self.redis.brpop(queue_key)
        return json.loads(data)

    async def publish_event(self, session_id: str, payload: dict[str, Any], global_event: bool = False) -> None:
        """
        Publish an event to a session's outbound stream.
        If global_event is True, also publish to the global completion stream.
        """
        # 1. Publish to session-specific stream (for real-time tokens/SSE)
        stream_key = f"{SESSION_STREAM_PREFIX}{session_id}:outbound"
        await self.redis.xadd(stream_key, {"data": json.dumps(payload)}, maxlen=1000, approximate=True)
        
        # 2. Optionally publish to global stream (for scalable webhook dispatching)
        if global_event:
            # We include session_id in the payload for the global stream
            payload["_session_id"] = session_id
            await self.redis.xadd(GLOBAL_OUTBOUND_STREAM, {"data": json.dumps(payload)}, maxlen=10000, approximate=True)

    async def subscribe_events(self, session_id: str, last_id: str = "0") -> AsyncGenerator[dict[str, Any], None]:
        """
        Subscribe to events for a specific session starting from last_id.
        """
        stream_key = f"{SESSION_STREAM_PREFIX}{session_id}:outbound"
        
        while True:
            events = await self.redis.xread({stream_key: last_id}, block=0)
            if not events:
                continue
                
            for _, stream_events in events:
                for event_id, event_data in stream_events:
                    last_id = event_id
                    data = json.loads(event_data["data"])
                    data["event_id"] = event_id  # for resume/sync (Last-Event-ID or ?last_event_id=)
                    yield data
                    if data.get("status") in ("done", "error"):
                        return

    async def init_consumer_group(self, group_name: str):
        """Create the consumer group for global events if it doesn't exist."""
        try:
            await self.redis.xgroup_create(GLOBAL_OUTBOUND_STREAM, group_name, id="0", mkstream=True)
        except redis.ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise

    async def consume_global_events(self, group_name: str, consumer_name: str) -> AsyncGenerator[tuple[str, dict[str, Any]], None]:
        """
        Consume events from the global stream using a consumer group.
        Yields (message_id, data).
        """
        while True:
            # Read new messages (">")
            response = await self.redis.xreadgroup(group_name, consumer_name, {GLOBAL_OUTBOUND_STREAM: ">"}, count=1, block=0)
            if not response:
                continue
                
            for _, messages in response:
                for message_id, message_data in messages:
                    data = json.loads(message_data["data"])
                    yield message_id, data

    async def ack_global_event(self, group_name: str, message_id: str):
        """Acknowledge a message in the consumer group."""
        await self.redis.xack(GLOBAL_OUTBOUND_STREAM, group_name, message_id)

    def _session_state_key(self, tenant_id: str, session_key: str) -> str:
        """Redis key for nanobot session state (conversation resume when container restarts)."""
        # Normalize session_key for use as key part (replace ':' with '_')
        safe_key = (session_key or "default").replace(":", "_")
        return f"{TENANT_INBOUND_PREFIX}{tenant_id}:{SESSION_STATE_SUFFIX}:{safe_key}"

    async def set_session_state(self, tenant_id: str, session_key: str, state: dict[str, Any]) -> None:
        """Persist nanobot session state to Redis so conversations can resume when container comes back up."""
        key = self._session_state_key(tenant_id, session_key)
        await self.redis.set(key, json.dumps(state))

    async def get_session_state(self, tenant_id: str, session_key: str) -> dict[str, Any] | None:
        """Load nanobot session state from Redis (e.g. when container starts or for conversation resume)."""
        key = self._session_state_key(tenant_id, session_key)
        data = await self.redis.get(key)
        if data is None:
            return None
        try:
            return json.loads(data)
        except json.JSONDecodeError:
            return None

    async def close(self):
        await self.redis.close()
