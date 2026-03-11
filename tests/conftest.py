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
TENANT_CONFIG_PATH = Path(__file__).parent.parent / "sample" / "tenant_config.json"
TENANT_ID = "pytest-tenant"
DEFAULT_TIMEOUT = 120.0


# ─── Helpers ─────────────────────────────────────────────────────────────────

async def provision_tenant(client: httpx.AsyncClient, tenant_id: str = TENANT_ID) -> int:
    # Add jitter to avoid thundering herd in xdist workers
    await asyncio.sleep(random.uniform(0.1, 2.0))
    
    config = json.loads(TENANT_CONFIG_PATH.read_text())
    config["tenant_id"] = tenant_id
    
    # Increase timeout for provisioning as it might involve container startup/checks
    r = await client.post(f"{GATEWAY}/api/tenant/config", json=config, timeout=60.0)
    return r.status_code


async def chat(
    client: httpx.AsyncClient,
    message: str,
    session_id: str,
    tenant_id: str = TENANT_ID,
) -> dict:
    r = await client.post(
        f"{GATEWAY}/api/chat",
        json={"message": message, "sessionId": session_id, "tenantId": tenant_id},
        timeout=DEFAULT_TIMEOUT,
    )
    r.raise_for_status()
    return r.json()


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
    except Exception as e:
        # Retry once if it was a network glitch or timeout
        await asyncio.sleep(2)
        status = await provision_tenant(http_client)
        assert status in (200, 409), f"Provisioning failed after retry: {e}"
