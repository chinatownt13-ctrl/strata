"""Falsifier evaluation, including the exec safety gate."""

import os
import sys
import unittest

from support import LedgerTestCase

from strata import falsify, model


class TestFileFalsifiers(LedgerTestCase):
    def setUp(self):
        super().setUp()
        self.write_file("docker-compose.yml", "services:\n  db:\n    image: postgres:16\n")

    def test_contains_holds(self):
        self.record(
            subject="a", predicate="b", text="c",
            falsifiers=[model.Falsifier("file_contains", {"path": "docker-compose.yml", "pattern": "postgres:16"})],
        )
        report = falsify.check(self.store, root=self.tmp)
        self.assertTrue(report.ok)
        self.assertEqual(len(report.holds), 1)

    def test_contains_falsified(self):
        self.record(
            subject="a", predicate="b", text="c",
            falsifiers=[model.Falsifier("file_contains", {"path": "docker-compose.yml", "pattern": "mysql:8"})],
        )
        report = falsify.check(self.store, root=self.tmp)
        self.assertFalse(report.ok)
        self.assertEqual(len(report.falsified), 1)

    def test_missing_file_is_falsified_not_an_error(self):
        self.record(
            subject="a", predicate="b", text="c",
            falsifiers=[model.Falsifier("file_exists", {"path": "nope.txt"})],
        )
        report = falsify.check(self.store, root=self.tmp)
        self.assertEqual(len(report.falsified), 1)

    def test_file_absent_holds(self):
        self.record(
            subject="a", predicate="b", text="c",
            falsifiers=[model.Falsifier("file_absent", {"path": "Gemfile"})],
        )
        report = falsify.check(self.store, root=self.tmp)
        self.assertTrue(report.ok)

    def test_regex_on_text(self):
        self.record(
            subject="a", predicate="b", text="uses postgres",
            falsifiers=[model.Falsifier("regex", {"pattern": "^uses", "target": "text"})],
        )
        report = falsify.check(self.store, root=self.tmp)
        self.assertTrue(report.ok)

    def test_unreadable_pattern_is_an_error(self):
        self.record(
            subject="a", predicate="b", text="c",
            falsifiers=[model.Falsifier("regex", {"pattern": "([unclosed"})],
        )
        report = falsify.check(self.store, root=self.tmp)
        self.assertFalse(report.ok)
        self.assertEqual(len(report.errors), 1)


class TestCommandFalsifiers(LedgerTestCase):
    def _claim(self, argv, kind="command_success"):
        self.record(
            subject="a", predicate="b", text="c",
            falsifiers=[model.Falsifier(kind, {"argv": argv})],
        )

    def test_disabled_by_default(self):
        self._claim([sys.executable, "-c", "print(1)"])
        report = falsify.check(self.store, root=self.tmp)
        self.assertEqual(len(report.skipped), 1)
        self.assertEqual(report.falsified, [])

    def test_runs_when_allowed(self):
        self._claim([sys.executable, "-c", "print(1)"])
        report = falsify.check(self.store, root=self.tmp, allow_exec=True)
        self.assertTrue(report.ok)
        self.assertEqual(len(report.holds), 1)

    def test_nonzero_exit_is_falsified(self):
        self._claim([sys.executable, "-c", "import sys; sys.exit(3)"])
        report = falsify.check(self.store, root=self.tmp, allow_exec=True)
        self.assertEqual(len(report.falsified), 1)

    def test_output_pattern(self):
        self.record(
            subject="a", predicate="b", text="c",
            falsifiers=[
                model.Falsifier(
                    "command_output",
                    {"argv": [sys.executable, "-c", "print('python3.13')"], "pattern": "python3"},
                )
            ],
        )
        report = falsify.check(self.store, root=self.tmp, allow_exec=True)
        self.assertTrue(report.ok)


class TestReportShape(LedgerTestCase):
    def test_assertions_without_falsifiers_are_ignored(self):
        self.record(subject="a", predicate="b", text="c")
        report = falsify.check(self.store, root=self.tmp)
        self.assertEqual(report.results, [])
        self.assertTrue(report.ok)

    def test_summary_counts(self):
        self.write_file("there.txt", "x")
        self.record(subject="a", predicate="b", text="passes",
                    falsifiers=[model.Falsifier("file_exists", {"path": "there.txt"})])
        self.record(subject="a", predicate="b", text="fails",
                    falsifiers=[model.Falsifier("file_exists", {"path": "missing.txt"})])
        payload = falsify.check(self.store, root=self.tmp).to_dict()
        self.assertEqual(payload["summary"]["total"], 2)
        self.assertEqual(payload["summary"]["holds"], 1)
        self.assertEqual(payload["summary"]["falsified"], 1)
        self.assertFalse(payload["ok"])

    def test_record_writes_last_checked(self):
        self.write_file("there.txt", "x")
        claim = self.record(subject="a", predicate="b", text="c",
                            falsifiers=[model.Falsifier("file_exists", {"path": "there.txt"})])
        falsify.check(self.store, root=self.tmp, record=True)
        row = self.store.connection().execute(
            "SELECT last_checked, last_result FROM falsifiers WHERE assertion_id = ?",
            (claim.id,),
        ).fetchone()
        self.assertIsNotNone(row["last_checked"])
        self.assertEqual(row["last_result"], "holds")


if __name__ == "__main__":
    unittest.main()
