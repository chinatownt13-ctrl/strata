"""Shared fixtures for the test suite.

Importing this module puts ``src/`` on ``sys.path`` as a side effect, so every
test file can start with ``from support import LedgerTestCase`` and be runnable
with nothing more than::

    python -m unittest discover -s tests -t tests

No pytest, no plugins — the library has no dependencies and neither do its
tests.
"""

import os
import shutil
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_HERE), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from strata.model import Evidence, Falsifier  # noqa: E402
from strata.store import Store  # noqa: E402


class LedgerTestCase(unittest.TestCase):
    """A throwaway ledger per test."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="strata-test-")
        self.path = os.path.join(self.tmp, ".strata")
        self.store = Store.create(self.path)
        self.addCleanup(self._teardown)

    def _teardown(self):
        try:
            self.store.close()
        finally:
            shutil.rmtree(self.tmp, ignore_errors=True)

    # helpers ------------------------------------------------------------

    def record(self, **kwargs):
        """Assert with sensible defaults; returns the Assertion (not the tuple)."""
        kwargs.setdefault("actor", "human:test")
        assertion, _ = self.store.assert_claim(**kwargs)
        return assertion

    def file_evidence(self, locator="src/app.py", excerpt="x = 1"):
        return Evidence(kind="file", locator=locator, excerpt=excerpt)

    def file_check(self, path="docker-compose.yml", pattern="postgres"):
        return Falsifier(kind="file_contains", spec={"path": path, "pattern": pattern})

    def write_file(self, name, content):
        target = os.path.join(self.tmp, name)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(content)
        return target
