"""The ledger: idempotence, event sourcing, retraction, supersession, integrity."""

import os
import sqlite3
import unittest

from support import LedgerTestCase

from strata import model
from strata.store import AmbiguousId, CycleDetected, NotFound, Store, UsageError


class TestAsserting(LedgerTestCase):
    def test_first_assert_creates(self):
        assertion = self.record(
            subject="project:x/db",
            predicate="engine",
            value="postgres",
            text="X uses postgres.",
        )
        self.assertEqual(assertion.standing, model.STANDING_ACTIVE)
        self.assertEqual(assertion.testimonials, 1)
        self.assertEqual(assertion.status, model.STATUS_OBSERVED)

    def test_reassertion_is_idempotent_but_counts_witnesses(self):
        first = self.record(
            subject="project:x/db", predicate="engine", value="postgres", text="X uses postgres."
        )
        second = self.record(
            subject="project:x/db", predicate="engine", value="postgres", text="X uses postgres.",
            actor="agent:claw",
        )
        self.assertEqual(first.id, second.id)
        self.assertEqual(second.testimonials, 2)
        self.assertEqual(len(self.store.all_ids()), 1)

    def test_blank_text_rejected(self):
        with self.assertRaises(UsageError):
            self.record(subject="a", predicate="b", text="   ")

    def test_unknown_status_rejected(self):
        with self.assertRaises(UsageError):
            self.record(subject="a", predicate="b", text="c", status="gut-feeling")

    def test_inferred_requires_premises(self):
        with self.assertRaises(UsageError) as caught:
            self.record(subject="a", predicate="b", text="c", status=model.STATUS_INFERRED)
        self.assertIn("depends-on", str(caught.exception))

    def test_status_upgrades_to_strongest_witness(self):
        base = dict(subject="a", predicate="b", text="c")
        self.record(status=model.STATUS_ASSUMED, **base)
        assertion = self.record(status=model.STATUS_OBSERVED, **base)
        self.assertEqual(assertion.status, model.STATUS_OBSERVED)
        # …and does not downgrade when the weaker witness speaks again.
        assertion = self.record(status=model.STATUS_ASSUMED, **base)
        self.assertEqual(assertion.status, model.STATUS_OBSERVED)

    def test_evidence_is_content_addressed(self):
        base = dict(subject="a", predicate="b", text="c")
        self.record(evidence=[self.file_evidence("f.py", "x = 1")], **base)
        assertion = self.record(evidence=[self.file_evidence("f.py", "x = 1")], **base)
        self.assertEqual(len(assertion.evidence), 1)
        assertion = self.store.add_evidence(
            assertion.id, [self.file_evidence("f.py", "x = 2")]
        )
        self.assertEqual(len(assertion.evidence), 2)

    def test_self_dependency_rejected(self):
        claim = self.record(subject="a", predicate="b", text="c")
        with self.assertRaises(UsageError):
            self.store.assert_claim(
                subject="a", predicate="b", text="c",
                status=model.STATUS_INFERRED, depends_on=[claim.id],
            )

    def test_cycle_rejected(self):
        first = self.record(subject="a", predicate="b", text="first")
        second = self.record(
            subject="a", predicate="b", text="second",
            status=model.STATUS_INFERRED, depends_on=[first.id],
        )
        # Now close the loop: make `first` depend on `second`.
        with self.assertRaises(CycleDetected):
            self.store.assert_claim(
                subject="a", predicate="b", text="first",
                status=model.STATUS_INFERRED, depends_on=[second.id],
            )


class TestRetraction(LedgerTestCase):
    def setUp(self):
        super().setUp()
        self.claim = self.record(
            subject="project:x/roadmap", predicate="datastore", value="mysql",
            text="X migrates to mysql in Q2.",
        )

    def test_reason_is_mandatory(self):
        with self.assertRaises(UsageError):
            self.store.retract(self.claim.id, reason="  ")

    def test_retraction_preserves_the_record(self):
        updated = self.store.retract(self.claim.id, reason="it was hypothetical")
        self.assertEqual(updated.standing, model.STANDING_RETRACTED)
        self.assertEqual(len(self.store.all_ids()), 1)
        # still retrievable by id — nothing was deleted
        self.assertEqual(self.store.get(self.claim.id).retract_reason, "it was hypothetical")

    def test_retracted_claims_leave_the_active_view(self):
        self.store.retract(self.claim.id, reason="wrong")
        self.assertEqual(self.store.query(standing="active"), [])
        self.assertEqual(len(self.store.query(standing="retracted")), 1)
        self.assertEqual(len(self.store.query(standing="all")), 1)

    def test_reassertion_after_retraction_restores_the_belief(self):
        self.store.retract(self.claim.id, reason="premature")
        restored = self.record(
            subject="project:x/roadmap", predicate="datastore", value="mysql",
            text="X migrates to mysql in Q2.",
        )
        self.assertEqual(restored.standing, model.STANDING_ACTIVE)
        self.assertEqual(restored.testimonials, 2)

    def test_history_keeps_both_sides(self):
        self.store.retract(self.claim.id, reason="wrong")
        kinds = [event.kind for event in self.store.events_for(self.claim.id)]
        self.assertEqual(kinds, ["assert", "retract"])


class TestSupersession(LedgerTestCase):
    def test_supersede_closes_valid_time(self):
        old = self.record(
            subject="project:x/db", predicate="engine", value="postgres", text="X uses postgres."
        )
        new = self.record(
            subject="project:x/db", predicate="engine", value="mysql", text="X uses mysql."
        )
        closed_old, opened_new = self.store.supersede(old.id, new.id, reason="migrated")
        self.assertEqual(closed_old.standing, model.STANDING_SUPERSEDED)
        self.assertEqual(closed_old.superseded_by, new.id)
        self.assertIsNotNone(closed_old.valid_to)
        self.assertEqual(opened_new.standing, model.STANDING_ACTIVE)

    def test_self_supersession_rejected(self):
        claim = self.record(subject="a", predicate="b", text="c")
        with self.assertRaises(UsageError):
            self.store.supersede(claim.id, claim.id)


class TestQuerying(LedgerTestCase):
    def setUp(self):
        super().setUp()
        self.observed = self.record(
            subject="project:x/db", predicate="engine", value="postgres",
            text="X uses postgres.", asserted_at="2026-01-05T00:00:00Z",
        )
        self.reported = self.record(
            subject="project:x/roadmap", predicate="datastore", value="mysql",
            text="X will migrate to mysql.", asserted_at="2026-03-01T00:00:00Z",
            status=model.STATUS_REPORTED,
        )

    def test_filter_by_subject(self):
        found = self.store.query(subject="project:x/db")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].id, self.observed.id)

    def test_filter_by_key(self):
        found = self.store.query(key="project:x/roadmap.datastore")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].id, self.reported.id)

    def test_filter_by_status(self):
        self.assertEqual(len(self.store.query(status="reported")), 1)

    def test_text_search(self):
        self.assertEqual(len(self.store.query(text="migrate")), 1)
        self.assertEqual(len(self.store.query(text="nothing-like-this")), 0)

    def test_as_of_sees_the_past_worldview(self):
        before = self.store.query(standing="all", as_of="2026-02-01")
        self.assertEqual([a.id for a in before], [self.observed.id])
        after = self.store.query(standing="all", as_of="2026-04-01")
        self.assertEqual(len(after), 2)

    def test_as_of_excludes_after_retraction(self):
        self.store.retract(self.reported.id, reason="rumour", at="2026-03-15T00:00:00Z")
        mid = self.store.query(standing="all", as_of="2026-03-10")
        later = self.store.query(standing="all", as_of="2026-03-20")
        self.assertEqual(len(mid), 2)
        self.assertEqual([a.id for a in later], [self.observed.id])

    def test_limit_respected(self):
        self.assertEqual(len(self.store.query(limit=1)), 1)

    def test_unknown_standing_rejected(self):
        with self.assertRaises(UsageError):
            self.store.query(standing="ghost")


class TestIdResolution(LedgerTestCase):
    def test_prefix_resolves(self):
        claim = self.record(subject="a", predicate="b", text="c")
        self.assertEqual(self.store.get(claim.id[:6]).id, claim.id)

    def test_missing_id_reports_clearly(self):
        with self.assertRaises(NotFound):
            self.store.get("deadbeefdeadbeef")

    def test_ambiguous_prefix_is_an_error_not_a_guess(self):
        # Insert two rows sharing a prefix directly; the write API would never
        # produce this, but a corrupted or hand-edited file might.
        with self.store.transaction() as conn:
            for suffix in ("1", "2"):
                conn.execute(
                    "INSERT INTO assertions(id, subject, predicate, value, value_key, text, created_at) "
                    "VALUES(?, 's', 'p', NULL, NULL, 't', '2026-01-01T00:00:00Z')",
                    ("aaaa" + suffix,),
                )
        with self.assertRaises(AmbiguousId):
            self.store.get("aaaa")


class TestIntegrity(LedgerTestCase):
    def test_clean_ledger_verifies(self):
        self.record(
            subject="project:x/db", predicate="engine", value="postgres",
            text="X uses postgres.", evidence=[self.file_evidence()],
        )
        self.assertEqual(self.store.check_integrity(), [])

    def test_tampered_text_is_detected(self):
        claim = self.record(subject="a", predicate="b", text="original")
        with self.store.transaction() as conn:
            conn.execute("UPDATE assertions SET text = 'rewritten' WHERE id = ?", (claim.id,))
        problems = self.store.check_integrity()
        self.assertEqual(len(problems), 1)
        self.assertEqual(problems[0]["kind"], "assertion-hash-mismatch")

    def test_tampered_evidence_is_detected(self):
        claim = self.record(
            subject="a", predicate="b", text="c", evidence=[self.file_evidence("f.py", "x = 1")]
        )
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE evidence SET excerpt = 'y = 2' WHERE assertion_id = ?", (claim.id,)
            )
        problems = self.store.check_integrity()
        self.assertEqual(len(problems), 1)
        self.assertEqual(problems[0]["kind"], "evidence-digest-mismatch")

    def test_tampered_falsifier_is_detected(self):
        claim = self.record(
            subject="a", predicate="b", text="c",
            falsifiers=[self.file_check("a.txt", "needle")],
        )
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE falsifiers SET spec = ? WHERE assertion_id = ?",
                ('{"path":"a.txt","pattern":"something-else"}', claim.id),
            )
        problems = self.store.check_integrity()
        self.assertEqual(problems[0]["kind"], "falsifier-digest-mismatch")


class TestExportImport(LedgerTestCase):
    def test_roundtrip_preserves_everything(self):
        claim = self.record(
            subject="project:x/db", predicate="engine", value="postgres",
            text="X uses postgres.",
            evidence=[self.file_evidence()],
            falsifiers=[self.file_check()],
        )
        conclusion = self.record(
            subject="project:x/ops", predicate="task", value="upgrade",
            text="Upgrading postgres is on the roadmap.",
            status=model.STATUS_INFERRED, depends_on=[claim.id],
        )
        self.store.retract(conclusion.id, reason="deprioritised")
        rows = list(self.store.export_rows())

        other_path = os.path.join(self.tmp, "other.strata")
        other = Store.create(other_path)
        self.addCleanup(other.close)
        result = other.import_rows(rows)

        self.assertEqual(result["imported"], 2)
        self.assertEqual(result["events_added"], 3)
        imported = other.get(claim.id)
        self.assertEqual(imported.text, claim.text)
        self.assertEqual(len(imported.evidence), 1)
        self.assertEqual(len(imported.falsifiers), 1)
        self.assertEqual(other.get(conclusion.id).standing, model.STANDING_RETRACTED)

    def test_import_is_idempotent(self):
        self.record(subject="a", predicate="b", text="c")
        rows = list(self.store.export_rows())
        first = self.store.import_rows(rows)
        second = self.store.import_rows(rows)
        self.assertEqual(first["imported"], 0)
        self.assertEqual(first["events_added"], 0)
        self.assertEqual(second["skipped"], 1)
        self.assertEqual(len(self.store.all_ids()), 1)

    def test_import_rejects_content_that_does_not_match_its_id(self):
        self.record(subject="a", predicate="b", text="c")
        rows = list(self.store.export_rows())
        rows[0]["text"] = "silently rewritten"
        result = self.store.import_rows(rows)
        self.assertEqual(result["rejected"], 1)
        self.assertEqual(result["imported"], 0)


class TestStats(LedgerTestCase):
    def test_counts_move_with_the_ledger(self):
        claim = self.record(subject="a", predicate="b", text="c", evidence=[self.file_evidence()])
        self.store.retract(claim.id, reason="mistake")
        stats = self.store.stats()
        self.assertEqual(stats["assertions"], 1)
        self.assertEqual(stats["active"], 0)
        self.assertEqual(stats["retracted"], 1)
        self.assertEqual(stats["evidence"], 1)
        self.assertEqual(stats["events"], 2)


if __name__ == "__main__":
    unittest.main()
