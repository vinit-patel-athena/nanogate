from __future__ import annotations

import asyncio
import logging
import os
import httpx
from typing import Any

from nanogate.bus import RedisMessageBus

logger = logging.getLogger(__name__)

class WebhookDispatcher:
    """
    Gateway background worker that consumes global events from Redis 
    and POSTs final results to registered callback URLs.
    
    Uses Redis Consumer Groups for horizontal scalability and 
    guaranteed delivery.
    """
    
    def __init__(
        self,
        message_bus: RedisMessageBus,
        group_name: str = "gateway_webhooks",
        consumer_name: str | None = None,
        max_retries: int = 3,
        timeout: float = 30.0,
    ):
        self.bus = message_bus
        self.group_name = group_name
        self.consumer_name = consumer_name or f"gateway_worker_{os.uname().nodename}_{os.getpid()}"
        self.max_retries = max_retries
        self.timeout = timeout
        self._stop_event = asyncio.Event()

    async def start(self):
        """Main loop for the webhook dispatcher."""
        logger.info(f"Starting WebhookDispatcher (group: {self.group_name}, consumer: {self.consumer_name})")
        
        # Ensure the consumer group exists
        await self.bus.init_consumer_group(self.group_name)
        
        async with httpx.AsyncClient() as client:
            async for message_id, event in self.bus.consume_global_events(self.group_name, self.consumer_name):
                if self._stop_event.is_set():
                    break
                
                try:
                    await self._process_event(client, message_id, event)
                except Exception as e:
                    logger.error(f"Error processing webhook event {message_id}: {e}")
                
    async def _process_event(self, client: httpx.AsyncClient, message_id: str, event: dict[str, Any]):
        """Inspects an event and fires a webhook if a callbackUrl is present. Retries up to max_retries then ACKs to avoid blocking."""
        request_payload = event.get("request_payload") or {}
        callback_url = request_payload.get("callbackUrl") or request_payload.get("callback_url")
        session_id = event.get("_session_id")

        if not callback_url:
            await self.bus.ack_global_event(self.group_name, message_id)
            return

        final_payload = event.copy()
        final_payload.pop("_session_id", None)
        final_payload.pop("request_payload", None)
        final_payload["session_id"] = session_id

        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            if self._stop_event.is_set():
                return
            try:
                response = await client.post(
                    callback_url, json=final_payload, timeout=self.timeout
                )
                if 200 <= response.status_code < 300:
                    logger.info("Successfully delivered webhook for %s", session_id)
                    await self.bus.ack_global_event(self.group_name, message_id)
                    return
                last_error = None
                logger.warning(
                    "Webhook for %s failed with status %s (attempt %s/%s)",
                    session_id, response.status_code, attempt + 1, self.max_retries,
                )
            except Exception as e:
                last_error = e
                logger.warning(
                    "Webhook POST for %s failed (attempt %s/%s): %s",
                    session_id, attempt + 1, self.max_retries, e,
                )
            if attempt < self.max_retries - 1:
                await asyncio.sleep(2 ** attempt)  # 1s, 2s, 4s backoff

        logger.error(
            "Webhook for session %s exhausted retries; ACK to unblock consumer. Last error: %s",
            session_id, last_error,
        )
        await self.bus.ack_global_event(self.group_name, message_id)

    def stop(self):
        self._stop_event.set()
