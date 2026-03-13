"""
Single-tenant agent server with Redis Message Bus support.

Runs inside a Docker container to serve one nanobot agent instance.
Listens to a Redis queue for incoming requests and publishes events (tokens, progress)
to a Redis stream for real-time delivery to the gateway/user.

Run with: python -m agent.server
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from agent.agent_loop import create_agent_loop
from agent.routes.chat import build_chat_router, _run_chat, _normalize_session
from agent.routes.approval import build_approval_router
from agent.session_persistence import load_session_state_from_redis, save_session_state_to_redis
from nanogate.bus import RedisMessageBus

logger = logging.getLogger(__name__)

# Single agent for this container
agent_loop = None
message_bus = None
tenant_id = os.environ.get("TENANT_ID", "default")
redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379")

async def bus_listener_loop():
    """Background task to consume requests from the message bus."""
    global agent_loop, message_bus
    
    logger.info(f"Starting message bus listener for tenant: {tenant_id}")
    while True:
        try:
            if not message_bus:
                await asyncio.sleep(1)
                continue
                
            payload = await message_bus.consume_request(tenant_id)
            logger.info(f"Received request from bus: {payload}")
            
            message = payload.get("message")
            session_id = _normalize_session(payload.get("session_id") or payload.get("sessionId"))
            
            if not message or not agent_loop:
                continue

            # Restore session state from Redis so conversation can resume after container restart
            workspace_path = getattr(agent_loop, "workspace", None)
            if message_bus and workspace_path:
                await load_session_state_from_redis(message_bus, tenant_id, session_id, workspace_path)

            async def _on_progress(content: str, *, tool_hint: bool = False) -> None:
                """Stream intermediate progress events back to the bus."""
                try:
                    await message_bus.publish_event(session_id, {
                        "status": "progress",
                        "response": content,
                        "tool_hint": tool_hint,
                    })
                except Exception as exc:
                    logger.debug(f"Bus progress publish failed: {exc}")

            try:
                result = await _run_chat(
                    agent_loop, 
                    session_id, 
                    message,
                    on_progress=_on_progress
                )
                
                final_payload = {
                    "status": "done",
                    "response": result.response,
                    "approval_request_id": result.approval_request_id,
                    "approval_context": result.approval_context,
                    "request_payload": payload, # Preserve original request context (like callback_url)
                }
                
                # Publish to session stream AND global stream for webhooks
                await message_bus.publish_event(session_id, final_payload, global_event=True)

                # Persist session state to Redis so conversation can resume when container comes back up
                if workspace_path:
                    await save_session_state_to_redis(message_bus, tenant_id, session_id, workspace_path)

            except Exception as e:
                logger.error(f"Error processing bus request: {e}")
                err_payload = {
                    "status": "error",
                    "error": str(e),
                    "request_payload": payload,
                }
                await message_bus.publish_event(session_id, err_payload, global_event=True)
                
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error in bus listener loop: {e}")
            await asyncio.sleep(5)

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Create the single agent loop and start the bus listener on startup."""
    global agent_loop, message_bus
    
    logger.info("Initializing Agent Loop...")
    agent_loop = create_agent_loop()
    
    logger.info("Initializing Message Bus...")
    message_bus = RedisMessageBus(redis_url)
    
    listener_task = asyncio.create_task(bus_listener_loop())
    
    yield
    
    listener_task.cancel()
    try:
        await listener_task
    except asyncio.CancelledError:
        pass
        
    if agent_loop is not None:
        await agent_loop.close_mcp()
        agent_loop = None
        
    if message_bus:
        await message_bus.close()

app = FastAPI(
    title="Nanogate Agent Server",
    description="Single-tenant agent server with Redis Message Bus.",
    lifespan=lifespan,
)

# Still expose HTTP routes for backward compatibility/local testing
chat_router = build_chat_router(lambda: agent_loop)
approval_router = build_approval_router(lambda: agent_loop)
app.include_router(chat_router, prefix="/api", tags=["chat"])
app.include_router(approval_router, prefix="/api", tags=["approval"])

@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "tenant": tenant_id, "bus": "active" if message_bus else "inactive"}

def main() -> None:
    import uvicorn
    import logging
    
    logging.basicConfig(level=logging.INFO)
    
    uvicorn.run(
        "agent.server:app",
        host="0.0.0.0",
        port=8765,
        reload=False,
    )

if __name__ == "__main__":
    main()
