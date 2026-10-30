"""One lock for every matplotlib figure body.

pyplot keeps a process-global "current figure". Tools now run in worker threads, and two
plot calls interleaving let ``plt.savefig`` write one basin's figure into another's PNG (silent
wrong evidence). The plotting code addresses its own ``fig`` object, and each plotting body also
runs under this re-entrant lock because figure creation/registration in pyplot is global state too.
"""
from __future__ import annotations

import functools
import threading

PLOT_LOCK = threading.RLock()


def plot_serialized(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with PLOT_LOCK:
            return fn(*args, **kwargs)

    return wrapper
