import importlib.util
import inspect
import logging
import os
import sys
from pathlib import Path
from typing import Iterator

from nanobot.agent.tools.base import Tool

logger = logging.getLogger(__name__)

def discover_tools(tools_dir: str | Path | None = None) -> Iterator[Tool]:
    """Scans the provided directory for Python files and yields instantiated `Tool` subclasses."""
    if tools_dir is None:
        tools_dir = os.environ.get("NANOGATE_TOOLS_DIR", "/app/tenant_tools")
        
    tools_path = Path(tools_dir).resolve()
    
    if not tools_path.is_dir():
        logger.debug(f"Tools directory {tools_path} not found or is not a directory. Skipping tool auto-discovery.")
        return

    logger.info(f"Scanning for custom tools in {tools_path}")
    
    sys.path.insert(0, str(tools_path))

    for py_file in tools_path.rglob("*.py"):
        if py_file.name.startswith("__") or py_file.name.startswith("."):
            continue
            
        module_name = py_file.stem
        try:
            spec = importlib.util.spec_from_file_location(module_name, py_file)
            if spec is None or spec.loader is None:
                continue
                
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            
            for name, obj in inspect.getmembers(module):
                if inspect.isclass(obj) and issubclass(obj, Tool) and obj is not Tool:
                    # Ignore imported classes from other modules to prevent re-registering
                    if obj.__module__ == module_name:
                        logger.info(f"Discovered custom tool: {obj.__name__} in {py_file.name}")
                        try:
                            # Instantiate without arguments; custom tools must support this 
                            # or handle their own setup internally.
                            yield obj()
                        except Exception as e:
                            logger.error(f"Failed to instantiate custom tool {obj.__name__}: {e}")
                            
        except Exception as e:
            logger.error(f"Failed to load script {py_file}: {e}")
            
    # Clean up sys.path
    if str(tools_path) in sys.path:
        sys.path.remove(str(tools_path))
