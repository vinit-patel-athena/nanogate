"""
Multi-agent HTTP gateway: chat API + tool gateway (approval + token injection).

Run with: uv run -m gateway.server
Or: uv run uvicorn gateway.server:app --host 0.0.0.0 --port 8765

Requires: uv pip install -e ".[api]"
"""

from __future__ import annotations

from contextlib import asynccontextmanager

try:
    from fastapi import FastAPI
except ImportError as e:
    raise ImportError(
        "API gateway requires optional dependencies. Install with: pip install -e '.[api]'"
    ) from e

from gateway.registry import AgentRegistry, DEFAULT_TENANT_ID
from gateway.routes.approval import build_approval_router
from gateway.routes.chat import build_chat_router
from gateway.routes.tenant import build_tenant_router

registry = AgentRegistry()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Install dispatcher hook and pre-create the default agent."""
    registry.install_hook()
    await registry.get_or_create(DEFAULT_TENANT_ID)
    yield
    await registry.shutdown_all()


app = FastAPI(
    title="Nanobot API Gateway",
    description="Multi-agent HTTP gateway with tool approval and token injection.",
    lifespan=lifespan,
)
chat_router = build_chat_router(registry)
approval_router = build_approval_router(registry)
tenant_router = build_tenant_router(registry)
app.include_router(chat_router, prefix="/api", tags=["chat"])
app.include_router(approval_router, prefix="/api", tags=["approval"])
app.include_router(tenant_router, prefix="/api", tags=["tenant"])


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


def main() -> None:
    import uvicorn

    uvicorn.run(
        "gateway.server:app",
        host="0.0.0.0",
        port=8765,
        reload=False,
    )


if __name__ == "__main__":
    main()
