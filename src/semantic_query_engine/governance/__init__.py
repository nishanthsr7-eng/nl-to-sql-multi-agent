"""Governance: who may see which rows, which columns are personal data, and what ran.

Four concerns, deliberately in four modules rather than one:

``policy``
    What the domain declares -- row policies and PII tags, read from the
    semantic layer.
``principals``
    Who is asking and what they hold, read from the deployment's principal file.
``row_security``
    Predicate injection into the parsed query, plus the re-derived check that it
    reached the SQL that actually ran.
``masking``
    Which output columns carry personal data, and what a caller without access
    sees instead.
``audit``
    The append-only, hash-chained record of every run.

The split between the first two is the one worth preserving: a policy is a fact
about the warehouse and is version-controlled with it, an identity is a fact
about a deployment and is not.
"""
