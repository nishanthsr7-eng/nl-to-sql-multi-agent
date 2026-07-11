"""Application-wide logging setup.

Call :func:`configure_logging` once at process start (UI boot, script entry,
test session). Modules elsewhere just do ``logging.getLogger(__name__)``.
"""

from __future__ import annotations

import logging
import os
import sys

_CONFIGURED = False


def configure_logging(level: str | None = None) -> None:
    """Idempotently configure the root ``semantic_query_engine`` logger.

    Safe to call multiple times (e.g. once from the Streamlit entrypoint and
    once from a script) — only the first call has an effect.
    """
    global _CONFIGURED
    if _CONFIGURED:
        return

    resolved_level = (level or os.getenv("SQE_LOG_LEVEL") or "INFO").upper()

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )

    logger = logging.getLogger("semantic_query_engine")
    logger.setLevel(resolved_level)
    logger.addHandler(handler)
    logger.propagate = False

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Return a child logger under the ``semantic_query_engine`` namespace, configuring on first use."""
    configure_logging()
    return logging.getLogger(name)
