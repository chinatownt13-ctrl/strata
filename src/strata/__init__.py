"""strata — a ledger of assertions.

Memory for machines that must be trusted: content-addressed beliefs, bitemporal
history, append-only events, dependency-tracked conclusions, and checkable
falsifiers. Standard library only, one SQLite file, no daemon.

    from strata.store import Store
    from strata.model import Evidence

    store = Store.open(".strata")
    assertion, created = store.assert_claim(
        subject="project:acme/db",
        predicate="engine",
        value="postgres",
        text="Acme's primary datastore is PostgreSQL 16.",
        status="observed",
        evidence=[Evidence("file", "docker-compose.yml", "image: postgres:16")],
    )
"""

__version__ = "0.1.0"
__all__ = ["__version__"]
