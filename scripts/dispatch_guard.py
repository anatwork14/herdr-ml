"""Single check that production dispatch is still disabled.

The flag is queue_adapter.PRODUCTION_DISPATCH_ENABLED. It must stay False; any other value
(True, or a non-bool) makes every caller of require_disabled() refuse. Callers use this
module rather than reading the flag themselves.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import queue_adapter  # noqa: E402


class DispatchEnabled(Exception):
    pass


def require_disabled():
    if queue_adapter.PRODUCTION_DISPATCH_ENABLED is not False:
        raise DispatchEnabled('production dispatch must remain disabled')


# Import-time enforcement.
require_disabled()
