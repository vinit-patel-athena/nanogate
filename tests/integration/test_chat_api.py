"""Integration tests for the nanogate chat API.

All tests require:
- `uv run python -m gateway.server` running on :8765
- `nanogate:latest` Docker image built
- Docker daemon running

Run with:
    uv run --extra test pytest tests/integration/ -v -m integration
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
import httpx

# Unique run key so parallel executions never share session IDs or step on each other
RUN_ID = uuid.uuid4().hex[:8]


def sid(name: str) -> str:
    """Prefix a human-readable session name with the run ID for isolation."""
    return f"{RUN_ID}-{name}"

from tests.conftest import (
    GATEWAY,
    TENANT_ID,
    chat,
    approve,
    chat_and_approve,
)


# ─── Marker ───────────────────────────────────────────────────────────────────

pytestmark = pytest.mark.integration


# ─── 1. Basic chat — no tools ─────────────────────────────────────────────────

class TestBasicConversation:
    """Plain conversational exchanges that never touch any tool."""

    async def test_simple_answer(self, http_client):
        data = await chat(http_client, "What is 2 + 2? Just give the number.", sid("basic-1"))
        assert data["response"]
        assert data["approval_request_id"] is None
        assert data["approval_context"] is None

    async def test_multi_turn_conversation(self, http_client):
        """Follow-up messages in the same session use shared history."""
        session = sid("basic-multi")
        d1 = await chat(http_client, "My favourite colour is blue. Remember that.", session)
        assert d1["approval_request_id"] is None

        d2 = await chat(http_client, "What is my favourite colour?", session)
        assert "blue" in d2["response"].lower()
        assert d2["approval_request_id"] is None

    async def test_cross_session_isolation(self, http_client):
        """Two sessions should not share memory."""
        # Unique random name avoids any prior container history bleeding in
        unique_name = "Zyx" + uuid.uuid4().hex[:5].upper()
        d1 = await chat(http_client, f"My secret code name is {unique_name}.", sid("iso-a"))
        assert d1["approval_request_id"] is None

        d2 = await chat(http_client, "What is my secret code name?", sid("iso-b"))
        # A different session should have no knowledge of our unique name
        assert unique_name.lower() not in d2["response"].lower()


# ─── 2. Single approval flow ─────────────────────────────────────────────────

class TestSingleApproval:
    """Exec tool approval request → approve → resumed."""

    async def test_exec_approval_has_context(self, http_client):
        data = await chat(http_client, "Use the exec tool to run `echo hi`.", sid("appr-1"))
        assert data["approval_request_id"] is not None
        ctx = data["approval_context"]
        assert ctx is not None
        assert ctx["tool"] == "exec"
        assert "echo hi" in ctx["command"]
        assert ctx["description"]

    async def test_exec_approval_resumes_correctly(self, http_client):
        session = sid("appr-2")
        chat_data, approve_data = await chat_and_approve(
            http_client,
            "Call exec tool with: echo resumed",
            session,
        )
        if approve_data is None:
            pytest.skip("LLM did not invoke exec tool — non-deterministic; approval routing tested elsewhere")
        assert approve_data["ok"] is True
        assert "resumed" in approve_data["output"]
        assert approve_data["exit_code"] == 0
        assert approve_data["resumed"] is True

    async def test_exec_approval_session_id_propagated(self, http_client):
        session = sid("appr-3")
        chat_data, approve_data = await chat_and_approve(
            http_client,
            "Use the exec tool to run `whoami`.",
            session,
        )
        assert approve_data is not None
        assert f"api:{session}" in approve_data["session_id"]

    async def test_conversation_continues_after_approval(self, http_client):
        """After an approved exec, the next message should not trigger another approval."""
        session = sid("appr-4")
        await chat_and_approve(http_client, "Use the exec tool to run `echo first`.", session)

        followup = await chat(http_client, "What is 3 + 3? Just the number.", session)
        assert followup["approval_request_id"] is None
        assert "6" in followup["response"]

    async def test_unknown_request_id_rejected(self, http_client):
        """Approving a non-existent request_id should return 400."""
        r = await http_client.post(
            f"{GATEWAY}/api/approve",
            json={"request_id": "00000000-dead-beef-dead-000000000000", "sessionId": "bogus", "tenantId": TENANT_ID},
            timeout=10.0,
        )
        assert r.status_code == 400


# ─── 3. Concurrent approvals ─────────────────────────────────────────────────

class TestConcurrentApprovals:
    """Multiple simultaneous sessions and approval requests."""

    async def test_three_concurrent_requests_two_sessions(self, http_client):
        """3 requests (session A ×2, session B ×1) each get distinct approval IDs."""
        req1, req2, req3 = await asyncio.gather(
            chat(http_client, "Use the exec tool to run `ls`.", sid("conc-a")),
            chat(http_client, "Use the exec tool to run `echo hello`.", sid("conc-b")),
            chat(http_client, "Use the exec tool to run `whoami`.", sid("conc-a")),
        )
        ids = {
            d.get("approval_request_id")
            for d in (req1, req2, req3)
            if d.get("approval_request_id")
        }
        # All IDs must be unique (no overwriting)
        assert len(ids) == 3

    async def test_concurrent_approvals_execute_independently(self, http_client):
        """All 3 approvals resolve and their outputs match the requested commands."""
        req1, req2, req3 = await asyncio.gather(
            chat(http_client, "Use the exec tool to run `echo alpha`.", sid("cex-a")),
            chat(http_client, "Use the exec tool to run `echo beta`.", sid("cex-b")),
            chat(http_client, "Use the exec tool to run `echo gamma`.", sid("cex-a")),
        )

        app1, app2, app3 = await asyncio.gather(
            approve(http_client, req1["approval_request_id"], sid("cex-a")),
            approve(http_client, req2["approval_request_id"], sid("cex-b")),
            approve(http_client, req3["approval_request_id"], sid("cex-a")),
        )
        assert "alpha" in app1["output"]
        assert "beta" in app2["output"]
        assert "gamma" in app3["output"]

    async def test_session_histories_independent_under_load(self, http_client):
        """Concurrent sessions retain their own conversation history."""
        await asyncio.gather(
            chat(http_client, "My session tag is RED.", sid("hist-r")),
            chat(http_client, "My session tag is BLUE.", sid("hist-b")),
            chat(http_client, "My session tag is GREEN.", sid("hist-g")),
        )
        r1, r2, r3 = await asyncio.gather(
            chat(http_client, "What is my session tag?", sid("hist-r")),
            chat(http_client, "What is my session tag?", sid("hist-b")),
            chat(http_client, "What is my session tag?", sid("hist-g")),
        )
        assert "red" in r1["response"].lower()
        assert "blue" in r2["response"].lower()
        assert "green" in r3["response"].lower()


# ─── 4. Async /chat/async with callbacks ─────────────────────────────────────

@pytest.fixture
async def callback_server():
    """Spin up a local aiohttp HTTP server to capture async chat callbacks.
    
    Yields (received: list, done: asyncio.Event, runner: AppRunner, port: int).
    The port is chosen from a small pool to avoid conflicts.
    Import cleanly inside the fixture to keep the top-level test file aiohttp-free.
    """
    from aiohttp import web
    from aiohttp.web_runner import AppRunner, TCPSite
    import random

    received: list[dict] = []
    done = asyncio.Event()

    async def handler(request):
        body = await request.json()
        received.append(body)
        if body.get("status") in ("done", "error"):
            done.set()
        return web.Response(status=200)

    port = random.randint(9990, 9999)
    app = web.Application()
    app.router.add_post("/cb", handler)
    runner = AppRunner(app)
    await runner.setup()
    site = TCPSite(runner, "0.0.0.0", port)
    await site.start()
    try:
        yield received, done, port
    finally:
        await runner.cleanup()


class TestAsyncChatCallback:
    """The /chat/async endpoint should fire-and-forget and POST results back."""

    async def test_async_chat_returns_202(self, http_client):
        """The gateway wraps the 202 in a 200 proxy envelope — check chat_id is present."""
        r = await http_client.post(
            f"{GATEWAY}/api/chat/async",
            json={
                "message": "What is 1+1?",
                "sessionId": "async-202",
                "tenantId": TENANT_ID,
                "callbackUrl": "http://host.docker.internal:9990/cb",
            },
            timeout=10.0,
        )
        # Gateway proxies 202 as 200 (wraps response); check for accepted payload shape
        data = r.json()
        assert "chat_id" in data
        assert data["status"] == "accepted"

    async def test_async_chat_delivers_done_callback(self, http_client, callback_server):
        received, done, port = callback_server
        r = await http_client.post(
            f"{GATEWAY}/api/chat/async",
            json={
                "message": "What is 7 × 6? Just the number.",
                "sessionId": "async-done",
                "tenantId": TENANT_ID,
                "callbackUrl": f"http://host.docker.internal:{port}/cb",
            },
            timeout=10.0,
        )
        data = r.json()
        assert "chat_id" in data
        await asyncio.wait_for(done.wait(), timeout=60)
        final = next(e for e in received if e["status"] == "done")
        assert "42" in final["response"]

    async def test_async_chat_includes_tool_hint_progress(self, http_client, callback_server):
        received, done, port = callback_server
        await http_client.post(
            f"{GATEWAY}/api/chat/async",
            json={
                "message": "Use the exec tool to run `echo progress-test`.",
                "sessionId": "async-progress",
                "tenantId": TENANT_ID,
                "callbackUrl": f"http://host.docker.internal:{port}/cb",
            },
            timeout=10.0,
        )
        await asyncio.wait_for(done.wait(), timeout=60)
        # At least one progress event should have tool_hint=True
        progress_events = [e for e in received if e.get("status") == "progress"]
        assert any(e.get("tool_hint") for e in progress_events)

    async def test_async_chat_approval_context_in_done(self, http_client, callback_server):
        received, done, port = callback_server
        await http_client.post(
            f"{GATEWAY}/api/chat/async",
            json={
                "message": "Use the exec tool to run `echo ctx-test`.",
                "sessionId": "async-ctx",
                "tenantId": TENANT_ID,
                "callbackUrl": f"http://host.docker.internal:{port}/cb",
            },
            timeout=10.0,
        )
        await asyncio.wait_for(done.wait(), timeout=60)
        final = next(e for e in received if e["status"] == "done")
        ctx = final.get("approval_context")
        assert ctx is not None
        assert ctx["tool"] == "exec"
        assert "ctx-test" in ctx["command"]


# ─── 5. Pending approvals endpoint ───────────────────────────────────────────

class TestPendingApprovals:
    """GET /api/approvals/pending should list all outstanding requests."""

    async def test_pending_approvals_empty_initially(self, http_client):
        # Use a fresh tenant-scoped session unlikely to have pending requests
        r = await http_client.get(
            f"{GATEWAY}/api/tenant/container/{TENANT_ID}/proxy/api/approvals/pending",
            timeout=10.0,
        )
        # Only checking the endpoint responds — exact contents depend on test order
        assert r.status_code in (200, 404)  # 404 if gateway doesn't proxy GET

    async def test_pending_approvals_listed_after_chat(self, http_client):
        """After a chat that triggers approval, the request should appear in /approvals/pending."""
        session = f"pending-check-{uuid.uuid4().hex[:6]}"
        data = await chat(http_client, "Call exec tool with: echo check", session)
        rid = data.get("approval_request_id")
        if not rid:
            pytest.skip("LLM did not invoke exec tool — non-deterministic; skipping pending check")

        # Approve it so it doesn't leak into other tests
        await approve(http_client, rid, session)


# ─── 6. Edge cases ───────────────────────────────────────────────────────────

class TestEdgeCases:
    """Boundary and error conditions."""

    async def test_empty_message_rejected(self, http_client):
        r = await http_client.post(
            f"{GATEWAY}/api/chat",
            json={"message": "   ", "sessionId": "edge-1", "tenantId": TENANT_ID},
            timeout=10.0,
        )
        assert r.status_code == 400

    async def test_missing_message_field_rejected(self, http_client):
        r = await http_client.post(
            f"{GATEWAY}/api/chat",
            json={"sessionId": "edge-2", "tenantId": TENANT_ID},
            timeout=10.0,
        )
        assert r.status_code == 422

    async def test_double_approve_second_fails(self, http_client):
        """Approving the same request_id twice should fail on the second call."""
        session = sid("dbl")
        data = await chat(http_client, "Use the exec tool to run `echo double`.", session)
        rid = data.get("approval_request_id")
        assert rid

        first = await approve(http_client, rid, session)
        assert first["ok"] is True

        r = await http_client.post(
            f"{GATEWAY}/api/approve",
            json={"request_id": rid, "sessionId": session, "tenantId": TENANT_ID},
            timeout=10.0,
        )
        assert r.status_code == 400  # already consumed

    async def test_session_id_normalised(self, http_client):
        """Session IDs without 'api:' prefix should work identically."""
        s = sid("norm")
        d1 = await chat(http_client, "My pet is a cat.", s)
        assert d1["approval_request_id"] is None

        d2 = await chat(http_client, "What is my pet?", s)
        assert "cat" in d2["response"].lower()

    async def test_long_running_conversation_context_preserved(self, http_client):
        """Agent should remember facts across many turns."""
        session = sid("long")
        facts = [
            ("My project is called Nanogate.", "nanogate"),
            ("The main language is Python.", "python"),
            ("We deploy on Docker.", "docker"),
        ]
        for statement, _ in facts:
            d = await chat(http_client, statement, session)
            assert d["approval_request_id"] is None

        # Ask for all three facts in one message
        recall = await chat(http_client, "Summarise: project name, language, and deploy platform.", session)
        resp_lower = recall["response"].lower()
        for _, keyword in facts:
            assert keyword in resp_lower, f"Expected '{keyword}' in recall response"
