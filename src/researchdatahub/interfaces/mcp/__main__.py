"""``python -m researchdatahub.interfaces.mcp`` 的入口（等价于 ``rdh-mcp``）。"""

from __future__ import annotations

import sys

from researchdatahub.interfaces.mcp.server import main

if __name__ == "__main__":
    sys.exit(main())
