"""Request and response models for the HTTP layer.

Only the *request* side is modelled here. The response body is the pipeline's
own ``to_dict()`` payload, passed through untouched, for the same reason the CLI
renders from that payload: a second schema describing the same result is a
second thing to keep in sync, and the discriminated union in ``core.results`` is
already the schema.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class QueryRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=2_000, description="The question, in plain English.")
    # Prior turns, oldest first. The pipeline only consults them when the current
    # question is ambiguous on its own, so sending a whole transcript is safe.
    context: list[str] = Field(default_factory=list, max_length=20)
    # Who the request runs as. A name, resolved against the domain's declared
    # principals, rather than the grants themselves: a caller that could send
    # its own grant list would be granting itself access, which is not an
    # access-control system. A real deployment resolves this from the bearer
    # token instead and ignores the field; it is here so the governance path is
    # reachable over HTTP without standing up an identity provider first.
    principal: str | None = Field(
        default=None,
        max_length=128,
        description="Declared principal to run as. See GET /principals. Defaults to the unrestricted steward.",
    )
    explain: bool = Field(
        default=False,
        description="Reserved: the payload always carries the SQL and agent trace; a caller may ignore them.",
    )


class HealthResponse(BaseModel):
    status: str
    warehouse: str
    tables: list[str]
    llm_provider: str
    llm_enabled: bool
    version: str
