"""Portable asyncio runtime selection.

uvloop delivers the lowest event-loop overhead on Linux and macOS but it
publishes no Windows wheels and does not support Windows at all. ``winloop`` is
its Windows port and exposes the same ``run`` entry point. Both are treated as
optional at import time so the CLI keeps working on any platform, falling back
to the standard library loop rather than failing to start.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
from collections.abc import Callable, Coroutine
from typing import Any, TypeVar

_T = TypeVar("_T")

ACCELERATED_LOOP_MODULE = "winloop" if sys.platform == "win32" else "uvloop"


def accelerated_runner() -> Callable[..., Any] | None:
    """Return the platform's accelerated ``run`` callable, if importable."""
    try:
        module = importlib.import_module(ACCELERATED_LOOP_MODULE)
    except ImportError:
        return None
    runner = getattr(module, "run", None)
    return runner if callable(runner) else None


def run(main: Coroutine[Any, Any, _T]) -> _T:
    """Run ``main`` on the fastest event loop available on this platform."""
    runner = accelerated_runner()
    if runner is None:
        return asyncio.run(main)
    return runner(main)
