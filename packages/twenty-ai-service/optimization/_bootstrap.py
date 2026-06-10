"""Put the service root on sys.path so `agent.*` and `optimization.*` import
cleanly regardless of the working directory the script is launched from."""

from __future__ import annotations

import sys
from pathlib import Path

_SERVICE_ROOT = Path(__file__).resolve().parent.parent  # packages/twenty-ai-service
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))
