"""Application-wide logging setup.

Call :func:`configure_logging` once at process start (CLI or API boot, script
entry, test session). Modules elsewhere just do ``logging.getLogger(__name__)``.

Everything goes to **stderr**, deliberately: it is what lets ``sqe ask --json``
promise that stdout carries nothing but the result payload.
"""

from __future__ import annotations

import logging
import os
import sys

_CONFIGURED = False


def configure_logging(level: str | None = None) -> None:
    """Idempotently configure the root ``semantic_query_engine`` logger.

    Safe to call multiple times (e.g. once from the CLI entrypoint and once from
    a script it imports) — only the first call has an effect.
    """
    global _CONFIGURED
    if _CONFIGURED:
        return

    from semantic_query_engine.governance.telemetry import JsonFormatter, RunIdFilter

    resolved_level = (level or os.getenv("SQE_LOG_LEVEL") or "INFO").upper()

    handler = logging.StreamHandler(sys.stderr)
    # The human format stays the default. A developer reading a terminal is the
    # common case and JSON is worse for them; a log shipper is the case that
    # needs structure, and it asks for it.
    if (os.getenv("SQE_LOG_FORMAT") or "").strip().lower() == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter(
                fmt="%(asctime)s %(levelname)-8s [%(run_id)s] %(name)s: %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
    # On the handler rather than the logger: a filter on a logger does not run
    # for records that propagate up from a child, and every module here logs
    # through a child of ``semantic_query_engine``.
    handler.addFilter(RunIdFilter())

    logger = logging.getLogger("semantic_query_engine")
    logger.setLevel(resolved_level)
    logger.addHandler(handler)
    logger.propagate = False

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Return a child logger under the ``semantic_query_engine`` namespace, configuring on first use."""
    configure_logging()
    return logging.getLogger(name)
