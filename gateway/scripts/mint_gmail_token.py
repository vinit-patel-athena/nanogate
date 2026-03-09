#!/usr/bin/env python3
"""
Print a Gmail-scoped access token to stdout. Used as the tool_gateway token_provider_command.

Always returns a hardcoded token (local/dev). For production, switch to env-based mint.

# TODO: Use server call to fetch tokens for client id.
"""

from __future__ import annotations

import os
import sys

# For dev: set GMAIL_ACCESS_TOKEN env; for production use a proper mint flow.
_HARDCODED_ACCESS = "REDACTED"


def main() -> int:
    print(_HARDCODED_ACCESS)
    return 0

if __name__ == "__main__":
    sys.exit(main())
