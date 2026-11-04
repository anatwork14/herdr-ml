"""Single check that production dispatch is still disabled.

The flag lives here: dispatch_guard.PRODUCTION_DISPATCH_ENABLED (queue_adapter re-exports it
by import). It must stay False; any other value (True, or a non-bool) makes every caller of
require_disabled() refuse. This module imports nothing from the queue adapter, so researchctl's
import chain never reaches queue_adapter.
"""
# Production dispatch through any shared/default daemon is not implemented.
PRODUCTION_DISPATCH_ENABLED = False


class DispatchEnabled(Exception):
    pass


def require_disabled():
    if PRODUCTION_DISPATCH_ENABLED is not False:
        raise DispatchEnabled('production dispatch must remain disabled')


# Import-time enforcement.
require_disabled()
