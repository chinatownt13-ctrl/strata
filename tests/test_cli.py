"""The machine contract: exit codes, and stdout being exactly one JSON object."""

import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout

from support import LedgerTestCase

from strata import cli


class CliTestCase(LedgerTestCase):
    def run_cli(self, *argv):
        """Invoke the CLI in-process; returns (exit_code, stdout, stderr)."""
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = cli.main([str(a) for a in argv])
            except SystemExit as exc:  # argparse usage errors
                code = exc.code if isinstance(exc.code, int) else 1
        return code, out.getvalue(), err.getvalue()

    def run_json(self, *argv):
        code, out, err = self.run_cli(*argv)
        try:
            payload = json.loads(out)
        except json.JSONDecodeError as exc:  # pragma: no cover - failure path
            self.fail(f"stdout was not a single JSON object: {exc}\n--- stdout ---\n{out}")
        return code, payload, err

    def seed(self):
        claim = self.record(
            subject="project:x/db", predicate="engine", value="postgres",
            text="X uses postgres.",
        )
        return claim


class TestExitCodes(CliTestCase):
    def test_success_is_zero(self):
        code, payload, _ = self.run_json("--db", self.path, "stats", "--json")
        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])

    def test_empty_result_is_one(self):
        code, _, _ = self.run_cli("--db", self.path, "query")
        self.assertEqual(code, 1)

    def test_usage_error_is_two(self):
        code, _, err = self.run_cli("--db", self.path, "assert", "-s", "a", "-p", "b")
        self.assertEqual(code, 2)
        self.assertIn("text", err)

    def test_missing_id_is_one(self):
        code, _, err = self.run_cli("--db", self.path, "get", "deadbeefdeadbeef")
        self.assertEqual(code, 1)
        self.assertIn("no assertion", err)

    def test_verify_failure_is_three(self):
        claim = self.seed()
        with self.store.transaction() as conn:
            conn.execute("UPDATE assertions SET text = 'rewritten' WHERE id = ?", (claim.id,))
        code, payload, _ = self.run_json("--db", self.path, "verify", "--json")
        self.assertEqual(code, 3)
        self.assertFalse(payload["ok"])

    def test_falsified_check_is_three(self):
        from strata.model import Falsifier

        self.record(
            subject="a", predicate="b", text="d",
            falsifiers=[Falsifier("file_exists", {"path": "definitely-not-here.txt"})],
        )
        code, payload, _ = self.run_json("--db", self.path, "check", "--json")
        self.assertEqual(code, 3)
        self.assertFalse(payload["ok"])

    def test_cycle_is_four(self):
        first = self.record(subject="a", predicate="b", text="first")
        second = self.record(
            subject="a", predicate="b", text="second",
            status="inferred", depends_on=[first.id],
        )
        code, _, err = self.run_cli(
            "--db", self.path, "assert", "-s", "a", "-p", "b", "-t", "first",
            "--status", "inferred", "-d", second.id,
        )
        self.assertEqual(code, 4)
        self.assertIn("cycle", err.lower())


class TestJsonContract(CliTestCase):
    def test_assert_returns_a_parseable_object(self):
        code, payload, _ = self.run_json(
            "--db", self.path, "assert", "--json",
            "-s", "project:x/db", "-p", "engine", "-v", "postgres",
            "-t", "X uses postgres.",
            "-e", "file:docker-compose.yml#image: postgres:16",
        )
        self.assertEqual(code, 0)
        self.assertEqual(payload["command"], "assert")
        data = payload["data"]
        for key in ("id", "subject", "predicate", "value", "text", "status", "standing"):
            self.assertIn(key, data)
        self.assertEqual(data["status"], "observed")
        self.assertEqual(len(data["evidence"]), 1)
        self.assertEqual(data["evidence"][0]["kind"], "file")

    def test_stdout_holds_nothing_but_json(self):
        self.seed()
        code, out, err = self.run_cli("--db", self.path, "query", "--json")
        # json.loads over the *entire* stream proves no human chatter leaked in.
        parsed = json.loads(out)
        self.assertEqual(parsed["meta"]["count"], 1)
        self.assertEqual(err, "")

    def test_query_reports_metadata(self):
        self.seed()
        code, payload, _ = self.run_json("--db", self.path, "query", "--json")
        self.assertEqual(payload["meta"]["count"], 1)
        self.assertEqual(payload["meta"]["standing"], "active")

    def test_get_includes_event_stream(self):
        claim = self.seed()
        code, payload, _ = self.run_json("--db", self.path, "get", claim.id, "--json")
        self.assertEqual(payload["data"]["events"][0]["kind"], "assert")

    def test_global_flags_work_before_the_command(self):
        self.seed()
        code, payload, _ = self.run_json("--db", self.path, "--json", "stats")
        self.assertEqual(code, 0)
        self.assertEqual(payload["command"], "stats")

    def test_alias_resolves(self):
        self.seed()
        code, payload, _ = self.run_json("--db", self.path, "ls", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(payload["command"], "query")


class TestWorkflow(CliTestCase):
    def test_end_to_end_story(self):
        world = os.path.join(self.tmp, "story.strata")
        code, _, _ = self.run_cli("init", "--db", world, "--json")
        self.assertEqual(code, 0)

        _, payload, _ = self.run_json(
            "--db", world, "assert", "--json",
            "-s", "project:x/db", "-p", "engine", "-v", "postgres", "-t", "X uses postgres.",
        )
        premise = payload["data"]["id"]

        _, payload, _ = self.run_json(
            "--db", world, "assert", "--json",
            "-s", "project:x/ci", "-p", "requirement", "-v", "mysql-client",
            "-t", "CI needs mysql-client.", "--status", "inferred", "-d", premise,
        )
        conclusion = payload["data"]["id"]

        code, payload, _ = self.run_json("--db", world, "blame", conclusion, "--json")
        self.assertEqual(code, 0)
        self.assertEqual(payload["data"]["children"][0]["id"], premise)

        code, _, _ = self.run_cli("--db", world, "retract", premise, "-r", "wrong")
        self.assertEqual(code, 0)

        code, payload, _ = self.run_json("--db", world, "orphans", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(payload["meta"]["count"], 1)
        self.assertEqual(payload["data"][0]["id"], conclusion)


if __name__ == "__main__":
    unittest.main()
