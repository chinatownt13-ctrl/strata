"""Identity, canonicalisation, time parsing, and spec parsing."""

import unittest
from datetime import datetime, timezone

from support import LedgerTestCase  # noqa: F401  (side effect: sys.path)

from strata import model


class TestIdentity(unittest.TestCase):
    def test_same_proposition_derives_same_id(self):
        first = model.compute_id("project:x/db", "engine", "postgres", "Uses postgres.")
        second = model.compute_id("project:x/db", "engine", "postgres", "Uses postgres.")
        self.assertEqual(first, second)
        self.assertEqual(len(first), model.ID_LEN)

    def test_subject_and_predicate_are_case_insensitive(self):
        first = model.compute_id("Project:X/DB", "ENGINE", "postgres", "Uses postgres.")
        second = model.compute_id("project:x/db", "engine", "postgres", "Uses postgres.")
        self.assertEqual(first, second)

    def test_whitespace_is_collapsed(self):
        first = model.compute_id("project:x/db", "engine", "postgres", "Uses   postgres.")
        second = model.compute_id("project:x/db", "engine", "postgres", "  Uses postgres.  ")
        self.assertEqual(first, second)

    def test_text_casing_matters(self):
        """Two different sentences are two different propositions."""
        first = model.compute_id("a", "b", None, "The build is slow.")
        second = model.compute_id("a", "b", None, "The build is fast.")
        self.assertNotEqual(first, second)

    def test_value_case_is_preserved_in_storage_but_ignored_for_comparison(self):
        self.assertEqual(model.normalize_value("Postgres"), "Postgres")
        self.assertEqual(model.value_key("Postgres"), model.value_key("postgres"))

    def test_different_value_yields_different_id(self):
        first = model.compute_id("project:x/db", "engine", "postgres", "Uses postgres.")
        second = model.compute_id("project:x/db", "engine", "mysql", "Uses postgres.")
        self.assertNotEqual(first, second)

    def test_claim_key_normalises_both_halves(self):
        self.assertEqual(
            model.claim_key("Project:X/DB", "ENGINE"), model.claim_key("project:x/db", "engine")
        )


class TestTime(unittest.TestCase):
    def test_absolute_date(self):
        parsed = model.parse_time("2026-01-01")
        self.assertEqual(parsed.year, 2026)
        self.assertEqual(parsed.month, 1)
        self.assertEqual(parsed.tzinfo, timezone.utc)

    def test_relative_days(self):
        base = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)
        parsed = model.parse_time("-7d", base=base)
        self.assertEqual(parsed.day, 3)
        self.assertEqual(parsed.month, 5)

    def test_relative_future(self):
        base = datetime(2026, 5, 10, tzinfo=timezone.utc)
        parsed = model.parse_time("+2h", base=base)
        self.assertEqual(parsed.hour, 2)

    def test_iso_with_z(self):
        parsed = model.parse_time("2026-03-04T09:30:00Z")
        self.assertEqual(parsed.hour, 9)

    def test_garbage_raises(self):
        with self.assertRaises(ValueError):
            model.parse_time("last tuesday-ish")

    def test_humanize(self):
        self.assertIn("ago", model.humanize_age("2020-01-01T00:00:00Z"))


class TestEvidence(unittest.TestCase):
    def test_parse_locator_and_excerpt(self):
        evidence = model.Evidence.parse("file:src/app.py#x = 1")
        self.assertEqual(evidence.kind, "file")
        self.assertEqual(evidence.locator, "src/app.py")
        self.assertEqual(evidence.excerpt, "x = 1")

    def test_parse_url_keeps_colons(self):
        evidence = model.Evidence.parse("url:https://example.com/a?b=1#frag")
        self.assertEqual(evidence.kind, "url")
        self.assertEqual(evidence.locator, "https://example.com/a?b=1")
        self.assertEqual(evidence.excerpt, "frag")

    def test_digest_is_content_addressed(self):
        first = model.Evidence("file", "a.py", "x = 1")
        second = model.Evidence("file", "a.py", "x = 1")
        third = model.Evidence("file", "a.py", "x = 2")
        self.assertEqual(first.digest, second.digest)
        self.assertNotEqual(first.digest, third.digest)

    def test_unknown_kind_rejected(self):
        with self.assertRaises(ValueError):
            model.Evidence("vibes", "somewhere")

    def test_empty_locator_rejected(self):
        with self.assertRaises(ValueError):
            model.Evidence("file", "   ")

    def test_parse_without_colon_rejected(self):
        with self.assertRaises(ValueError):
            model.Evidence.parse("just-a-path")


class TestFalsifier(unittest.TestCase):
    def test_shorthand_exists(self):
        parsed = model.Falsifier.parse("exists:docker-compose.yml")
        self.assertEqual(parsed.kind, "file_exists")
        self.assertEqual(parsed.spec["path"], "docker-compose.yml")

    def test_shorthand_contains(self):
        parsed = model.Falsifier.parse("file:docker-compose.yml~postgres:16")
        self.assertEqual(parsed.kind, "file_contains")
        self.assertEqual(parsed.spec["pattern"], "postgres:16")

    def test_json_form(self):
        parsed = model.Falsifier.parse('regex:{"pattern": "^Uses", "target": "text"}')
        self.assertEqual(parsed.kind, "regex")

    def test_missing_field_rejected(self):
        with self.assertRaises(ValueError):
            model.Falsifier("file_contains", {"path": "a.txt"})

    def test_command_requires_argv_list(self):
        with self.assertRaises(ValueError):
            model.Falsifier("command_success", {"argv": "ls -la"})

    def test_digest_tracks_spec(self):
        first = model.Falsifier("file_exists", {"path": "a.txt"})
        second = model.Falsifier("file_exists", {"path": "b.txt"})
        self.assertNotEqual(first.digest, second.digest)

    def test_describe_is_readable(self):
        parsed = model.Falsifier.parse("exists:README.md")
        self.assertIn("README.md", parsed.describe())


class TestTruncate(unittest.TestCase):
    def test_short_text_untouched(self):
        self.assertEqual(model.truncate("hello", 10), "hello")

    def test_long_text_gets_ellipsis(self):
        result = model.truncate("x" * 100, 10)
        self.assertEqual(len(result), 10)
        self.assertTrue(result.endswith("…"))

    def test_newlines_flattened(self):
        self.assertEqual(model.truncate("a\nb   c", 20), "a b c")


if __name__ == "__main__":
    unittest.main()
