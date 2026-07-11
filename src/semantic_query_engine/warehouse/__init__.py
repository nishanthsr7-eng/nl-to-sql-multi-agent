"""DuckDB warehouse access — the swappable data-platform boundary.

Everything upstream (agents, pipeline, UI) talks to the warehouse only through
this package's connection interface, never through a raw file path. That is
the seam the MVP report's Phase 2 ("govern data access") replaces with a
managed warehouse client.
"""
