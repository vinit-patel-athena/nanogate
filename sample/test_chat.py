"""End-to-end tests for nanogate:
1. Concurrent approval flow (3 requests x 2 sessions)
2. Async /chat/async with streaming progress callbacks
"""

import asyncio
import httpx
import json
from aiohttp import web

TENANT_ID = "tenant-final9"
GATEWAY = "http://localhost:8765"


# ─── Helpers ─────────────────────────────────────────────────────────────────

async def provision(client):
    print("Provisioning tenant...")
    with open("sample/tenant_config.json") as f:
        config = json.load(f)
    config["tenant_id"] = TENANT_ID
    r = await client.post(f"{GATEWAY}/api/tenant/config", json=config, timeout=20.0)
    print("Provision:", r.status_code)


async def send_chat(client, session_id, message):
    print(f"[{session_id}] → chat")
    r = await client.post(f"{GATEWAY}/api/chat",
                          json={"message": message, "sessionId": session_id, "tenantId": TENANT_ID},
                          timeout=120.0)
    data = r.json()
    print(f"[{session_id}] ← {json.dumps(data, indent=2)}")
    return data.get("approval_request_id")


async def approve(client, session_id, request_id):
    if not request_id:
        print(f"[{session_id}] no approval needed")
        return
    print(f"\n[{session_id}] approving {request_id}")
    r = await client.post(f"{GATEWAY}/api/approve",
                          json={"request_id": request_id, "sessionId": session_id, "tenantId": TENANT_ID},
                          timeout=120.0)
    print(f"[{session_id}] ✓ approve → {json.dumps(r.json(), indent=2)}")


# ─── Test 1: concurrent approvals ────────────────────────────────────────────

async def test_concurrent_approvals(client):
    print("\n" + "=" * 60)
    print("TEST 1: 3 concurrent requests (2 sessions), then 3 approvals")
    print("=" * 60)

    req1, req2, req3 = await asyncio.gather(
        send_chat(client, "s9-1", "Use the exec tool to run `ls`."),
        send_chat(client, "s9-2", "Use the exec tool to echo 'hello world'."),
        send_chat(client, "s9-1", "Use the exec tool to run `whoami`."),
    )

    await asyncio.gather(
        approve(client, "s9-1", req1),
        approve(client, "s9-2", req2),
        approve(client, "s9-1", req3),
    )


# ─── Test 3: post-approval conversational follow-up ──────────────────────────

async def test_post_approval_conversation(client):
    print("\n" + "=" * 60)
    print("TEST 3: follow-up conversation after approval (same session)")
    print("=" * 60)

    session = "s9-conv"

    # First turn: requires approval
    approval_id = await send_chat(client, session, "Use the exec tool to run `echo hello`.")
    await approve(client, session, approval_id)

    # Second turn: plain question — no exec tool, no approval should be needed
    print(f"\n[{session}] → follow-up conversation (no approval expected)")
    r = await client.post(
        f"{GATEWAY}/api/chat",
        json={"message": "What is 2 + 2? Just answer, no tools.", "sessionId": session, "tenantId": TENANT_ID},
        timeout=60.0,
    )
    data = r.json()
    print(f"[{session}] ← {json.dumps(data, indent=2)}")

    if data.get("approval_request_id"):
        print(f"  ⚠ Unexpected approval request in conversational follow-up!")
    else:
        print(f"  ✓ No approval needed — conversation continued normally")



# ─── Test 2: async chat with callback ────────────────────────────────────────

async def test_async_callback(client):
    print("\n" + "=" * 60)
    print("TEST 2: /chat/async with streaming progress callback")
    print("=" * 60)

    received: list[dict] = []
    done_event = asyncio.Event()

    async def _handle(request):
        body = await request.json()
        received.append(body)
        print(f"  [callback] {json.dumps(body, indent=4)}")
        if body.get("status") in ("done", "error"):
            done_event.set()
        return web.Response(status=200)

    app = web.Application()
    app.router.add_post("/callback", _handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 9999)
    await site.start()
    print("Local callback server listening on :9999")

    try:
        # Fire the async chat — returns 202 immediately
        r = await client.post(
            f"{GATEWAY}/api/chat/async",
            json={
                "message": "Use the exec tool to run `echo async-test`.",
                "sessionId": "s9-async",
                "tenantId": TENANT_ID,
                "callbackUrl": "http://host.docker.internal:9999/callback",
            },
            timeout=10.0,
        )
        accepted = r.json()
        print(f"  202 Accepted: {json.dumps(accepted, indent=2)}")

        # Wait for the final callback (up to 120s)
        try:
            await asyncio.wait_for(done_event.wait(), timeout=120)
        except asyncio.TimeoutError:
            print("  ⚠ Timed out waiting for callback!")

        # The last callback should contain the approval request ID
        final = next((e for e in reversed(received) if e.get("status") in ("done", "error")), None)
        if final:
            approval_id = final.get("approval_request_id")
            if approval_id:
                print(f"\n  Approving async request {approval_id}...")
                session_id = "api:s9-async"
                await approve(client, session_id, approval_id)
            else:
                print("  No approval needed in async flow.")
        else:
            print("  ⚠ No final callback received")
    finally:
        await runner.cleanup()


# ─── Main ─────────────────────────────────────────────────────────────────────

async def main():
    async with httpx.AsyncClient() as client:
        await provision(client)
        await test_concurrent_approvals(client)
        await test_post_approval_conversation(client)
        await test_async_callback(client)


if __name__ == "__main__":
    asyncio.run(main())
