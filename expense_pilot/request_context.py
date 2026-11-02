"""Request Context for the expense pilot."""
from __future__ import annotations

import threading

REQUEST_CONTEXT = threading.local()


