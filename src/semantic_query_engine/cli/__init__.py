"""Command-line surface for the analytics pipeline.

``main`` is the entry point registered as the ``sqe`` console script; ``render``
holds every human-readable formatter, and is deliberately the only module here
that knows what a result *looks like*.
"""

from __future__ import annotations

from semantic_query_engine.cli.exit_codes import ExitCode

__all__ = ["ExitCode"]
