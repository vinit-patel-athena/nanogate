from nanobot.agent.tools.base import Tool
from typing import Any

class EchoTool(Tool):
    """An example custom tool that echoes a message."""
    @property
    def name(self) -> str:
        return "echo"
        
    @property
    def description(self) -> str:
        return "Echoes the provided message."
        
    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "The message to echo"
                }
            },
            "required": ["message"]
        }
        
    async def execute(self, message: str, **kwargs: Any) -> str:
        return f"Echoed: {message}"
