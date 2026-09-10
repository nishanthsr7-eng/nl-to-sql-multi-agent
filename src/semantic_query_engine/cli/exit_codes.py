"""Process exit codes for ``sqe``.

A CLI that returns 0 for "I could not answer that" is not scriptable: a shell
loop or a CI step cannot tell an answer from a guardrail rejection without
parsing prose. The three pipeline outcomes therefore map onto three distinct
codes, and they are part of the CLI's contract:

* ``0`` -- an answer was produced
* ``1`` -- the run failed (validation, execution, timeout, or no rows)
* ``2`` -- the question was ambiguous and a clarification is needed

2 is not an error in the usual sense, which is exactly why it is not 1: a caller
driving a conversation wants to branch on "ask the user something" separately
from "this went wrong".
"""

from __future__ import annotations

from enum import IntEnum


class ExitCode(IntEnum):
    ANSWER = 0
    FAILURE = 1
    CLARIFICATION = 2
    # Reserved for problems before the pipeline ran at all -- an unopenable
    # warehouse, a missing semantic layer. Distinct from FAILURE so "the engine
    # is broken" never looks like "the model wrote bad SQL".
    USAGE = 3


# The payload ``kind`` discriminant from ``core.results`` -> exit code. Keyed by
# the serialised string rather than the dataclass so that the renderer and the
# exit-code decision both consume the same ``to_dict()`` payload; see
# ``cli.render`` for why that matters.
EXIT_BY_KIND: dict[str, ExitCode] = {
    "answer": ExitCode.ANSWER,
    "clarification": ExitCode.CLARIFICATION,
    "failure": ExitCode.FAILURE,
}


def exit_code_for(payload: dict[str, object]) -> ExitCode:
    """Exit code for a serialised pipeline result."""
    kind = str(payload.get("kind", ""))
    return EXIT_BY_KIND.get(kind, ExitCode.FAILURE)
