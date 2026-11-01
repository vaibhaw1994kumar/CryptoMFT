from __future__ import annotations

import logging
import sys

import structlog


def configure_logging() -> None:
    """Send diagnostics to stderr so stdout stays machine readable.

    CLI commands print their JSON result to stdout. Interleaving log lines
    there would make the output impossible to pipe into ``jq`` or another
    process.
    """

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stderr,
        level=logging.INFO,
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
