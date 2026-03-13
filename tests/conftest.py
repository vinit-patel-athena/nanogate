"""Shared fixtures for the nanogate integration test suite."""

from __future__ import annotations

import json
import os
import random
import asyncio
from pathlib import Path
from typing import AsyncGenerator

import httpx
import pytest

# ─── Constants ────────────────────────────────────────────────────────────────

GATEWAY = os.getenv("NANOGATE_TEST_URL", "http://localhost:8765")
# Host the gateway can use to reach the test callback server (same machine → 127.0.0.1)
CALLBACK_HOST = "127.0.0.1" if ("localhost" in GATEWAY or "127.0.0.1" in GATEWAY) else "host.docker.internal"
TENANT_CONFIG_PATH = Path(__file__).parent.parent / "sample" / "tenant_config.json"
TENANT_ID = "pytest-tenant"
DEFAULT_TIMEOUT = 120.0
# Stream wait: fail before pytest-timeout so we can raise a clear error
STREAM_WAIT_TIMEOUT = 75.0


# ─── Helpers ─────────────────────────────────────────────────────────────────

async def provision_tenant(client: httpx.AsyncClient, tenant_id: str = TENANT_ID) -> int:
    # Add jitter to avoid thundering herd in xdist workers
    await asyncio.sleep(random.uniform(0.1, 2.0))
    
    config = json.loads(TENANT_CONFIG_PATH.read_text())
    config["tenant_id"] = tenant_id
    
    # Increase timeout for provisioning as it might involve container startup/checks
    r = await client.post(f"{GATEWAY}/api/tenant/config", json=config, timeout=60.0)
    if r.status_code >= 400:
        try:
            body = r.json()
            raise RuntimeError(f"Provisioning failed: {r.status_code} {body}")
        except Exception as e:
            if "Provisioning failed" not in str(e):
                raise RuntimeError(f"Provisioning failed: {r.status_code} {r.text}") from e
            raise
    return r.status_code


async def chat(
    client: httpx.AsyncClient,
    message: str,
    session_id: str,
    tenant_id: str = TENANT_ID,
    last_event_id: str | None = None,
) -> dict:
    """Initiates a chat and collects results via SSE stream for test compatibility.
    Pass last_event_id from a previous chat in the same session to avoid re-reading old events."""
    # 1. POST request
    r = await client.post(
        f"{GATEWAY}/api/chat",
        json={"message": message, "sessionId": session_id, "tenantId": tenant_id},
        timeout=DEFAULT_TIMEOUT,
    )
    r.raise_for_status()
    initial_data = r.json()
    actual_session_id = initial_data["session_id"]
    stream_url = f"{GATEWAY}/api/chat/stream/{actual_session_id}"
    if last_event_id:
        stream_url = f"{stream_url}?last_event_id={last_event_id}"
    # 2. Consume SSE stream
    full_response = ""
    last_event = {}

    async with client.stream(
        "GET",
        stream_url,
        timeout=DEFAULT_TIMEOUT,
    ) as response:
        async for line in response.aiter_lines():
            if line.startswith("data: "):
                event = json.loads(line[6:])
                last_event = event
                if event.get("status") == "progress":
                    # Collecting response tokens
                    full_response += event.get("response", "")
                elif event.get("status") in ("done", "error"):
                    # Use final response if provided, otherwise keep progress accumulation
                    if event.get("response"):
                        full_response = event["response"]
                    break
                    
    # Return a merged dict that matches the old ChatResponse shape
    status = last_event.get("status", "unknown")
    if status == "error":
        err = last_event.get("error", "unknown error")
        raise AssertionError(f"Chat returned error event: {err}")
    return {
        "response": full_response,
        "approval_request_id": last_event.get("approval_request_id"),
        "approval_context": last_event.get("approval_context"),
        "status": status,
        "session_id": actual_session_id,
        "event_id": last_event.get("event_id"),
    }


async def approve(
    client: httpx.AsyncClient,
    request_id: str,
    session_id: str,
    tenant_id: str = TENANT_ID,
) -> dict:
    r = await client.post(
        f"{GATEWAY}/api/approve",
        json={"request_id": request_id, "sessionId": session_id, "tenantId": tenant_id},
        timeout=DEFAULT_TIMEOUT,
    )
    r.raise_for_status()
    return r.json()


async def chat_and_approve(
    client: httpx.AsyncClient,
    message: str,
    session_id: str,
    tenant_id: str = TENANT_ID,
) -> tuple[dict, dict | None]:
    data = await chat(client, message, session_id, tenant_id)
    if rid := data.get("approval_request_id"):
        approve_data = await approve(client, rid, session_id, tenant_id)
        return data, approve_data
    return data, None


# ─── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
async def http_client() -> AsyncGenerator[httpx.AsyncClient, None]:
    async with httpx.AsyncClient() as client:
        yield client


@pytest.fixture(autouse=True)
async def ensure_tenant(http_client: httpx.AsyncClient):
    """Idempotent tenant provisioning before every test block."""
    try:
        status = await provision_tenant(http_client)
        assert status in (200, 409), f"Provisioning failed with status {status}"
    except AssertionError:
        raise
    except Exception as e:
        # Retry once if it was a network glitch or timeout
        await asyncio.sleep(2)
        status = await provision_tenant(http_client)
        assert status in (200, 409), f"Provisioning failed after retry: {e}"
