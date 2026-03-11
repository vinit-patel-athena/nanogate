"""
Single-tenant agent HTTP server.

Runs inside a Docker container to serve one nanobot agent instance.
Exposes /api/chat and /api/approve endpoints.

Run with: python -m agent.server
"""

from __future__ import annotations

from contextlib import asynccontextmanager

try:
    from fastapi import FastAPI
except ImportError as e:
    raise ImportError(
        "Agent server requires FastAPI. Install with: pip install fastapi uvicorn"
    ) from e

from agent.agent_loop import create_agent_loop
from agent.routes.chat import build_chat_router
from agent.routes.approval import build_approval_router

# Single agent for this container
agent_loop = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Create the single agent loop on startup."""
    global agent_loop
    agent_loop = create_agent_loop()
    yield
    if agent_loop is not None:
        await agent_loop.close_mcp()
        agent_loop = None


app = FastAPI(
    title="Nanogate Agent Server",
    description="Single-tenant agent server with chat and tool approval.",
    lifespan=lifespan,
)

chat_router = build_chat_router(lambda: agent_loop)
approval_router = build_approval_router(lambda: agent_loop)
app.include_router(chat_router, prefix="/api", tags=["chat"])
app.include_router(approval_router, prefix="/api", tags=["approval"])


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


def main() -> None:
    import uvicorn

    uvicorn.run(
        "agent.server:app",
        host="0.0.0.0",
        port=8765,
        reload=False,
    )


if __name__ == "__main__":
    main()
