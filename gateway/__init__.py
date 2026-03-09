"""API gateway package — multi-agent with per-tenant isolation."""

from .server import app, main

__all__ = ["app", "main"]
