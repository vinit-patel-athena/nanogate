"""Single-tenant agent server package.

Provides a standalone FastAPI server that runs one nanobot agent
with chat, approval, and tool gateway functionality.
No multi-tenant registry — each container runs exactly one agent.
"""

from agent.server import app, main

__all__ = ["app", "main"]
