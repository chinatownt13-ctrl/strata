"""The dependency graph: cascade invalidation, blame, impact, diff, conflicts."""

import unittest

from support import LedgerTestCase

from strata import graph, model


class GraphTestCase(LedgerTestCase):
    """A small, realistic belief tree.

        A  observed  db.engine = postgres
        B  reported  roadmap.datastore = mysql
        C  inferred  ci.requirement = mysql-client      ← B
        D  inferred  monitor.scope = postgres-exporter  ← A, B
    """

    def setUp(self):
        super().setUp()
        self.a = self.record(
            subject="project:x/db", predicate="engine", value="postgres",
            text="X uses postgres.",
        )
        self.b = self.record(
            subject="project:x/roadmap", predicate="datastore", value="mysql",
            text="X plans to migrate to mysql.",
            status=model.STATUS_REPORTED,
        )
        self.c = self.record(
            subject="project:x/ci", predicate="requirement", value="mysql-client",
            text="CI needs a mysql client after the migration.",
            status=model.STATUS_INFERRED, depends_on=[self.b.id],
        )
        self.d = self.record(
            subject="project:x/monitor", predicate="scope", value="postgres-exporter",
            text="Monitoring stays on the postgres exporter until migration.",
            status=model.STATUS_INFERRED, depends_on=[self.a.id, self.b.id],
        )


class TestIntegrity(GraphTestCase):
    def test_everything_intact_before_retraction(self):
        verdicts = graph.compute_integrity(self.store)
        for assertion in (self.a, self.b, self.c, self.d):
            self.assertEqual(verdicts[assertion.id], model.INTEGRITY_INTACT)

    def test_lone_premise_loss_orphans(self):
        self.store.retract(self.b.id, reason="hypothetical")
        verdicts = graph.compute_integrity(self.store)
        self.assertEqual(verdicts[self.c.id], model.INTEGRITY_ORPHANED)

    def test_partial_premise_loss_puts_at_risk(self):
        self.store.retract(self.b.id, reason="hypothetical")
        verdicts = graph.compute_integrity(self.store)
        self.assertEqual(verdicts[self.d.id], model.INTEGRITY_AT_RISK)

    def test_orphaning_propagates_downstream(self):
        downstream = self.record(
            subject="project:x/ci", predicate="task", value="add-client",
            text="Add mysql-client to the CI image.",
            status=model.STATUS_INFERRED, depends_on=[self.c.id],
        )
        self.store.retract(self.b.id, reason="hypothetical")
        verdicts = graph.compute_integrity(self.store)
        self.assertEqual(verdicts[self.c.id], model.INTEGRITY_ORPHANED)
        self.assertEqual(verdicts[downstream.id], model.INTEGRITY_ORPHANED)

    def test_at_risk_propagates_as_at_risk(self):
        downstream = self.record(
            subject="project:x/monitor", predicate="task", value="update-dashboards",
            text="Update dashboards after the change.",
            status=model.STATUS_INFERRED, depends_on=[self.d.id],
        )
        self.store.retract(self.b.id, reason="hypothetical")
        verdicts = graph.compute_integrity(self.store)
        self.assertEqual(verdicts[downstream.id], model.INTEGRITY_AT_RISK)


class TestFinders(GraphTestCase):
    def test_find_orphans(self):
        self.store.retract(self.b.id, reason="hypothetical")
        orphans = graph.find_orphans(self.store)
        self.assertEqual([a.id for a in orphans], [self.c.id])
        self.assertEqual(orphans[0].integrity, model.INTEGRITY_ORPHANED)

    def test_find_at_risk(self):
        self.store.retract(self.b.id, reason="hypothetical")
        risky = graph.find_at_risk(self.store)
        self.assertEqual([a.id for a in risky], [self.d.id])
        self.assertEqual(risky[0].integrity, model.INTEGRITY_AT_RISK)

    def test_nothing_is_orphaned_before_retraction(self):
        self.assertEqual(graph.find_orphans(self.store), [])
        self.assertEqual(graph.find_at_risk(self.store), [])


class TestTraversal(GraphTestCase):
    def test_downstream_closure(self):
        found = graph.downstream_closure(graph.load_edges(self.store), self.b.id)
        self.assertIn(self.c.id, found)
        self.assertIn(self.d.id, found)
        self.assertNotIn(self.a.id, found)

    def test_upstream_closure(self):
        found = graph.upstream_closure(graph.load_edges(self.store), self.d.id)
        self.assertIn(self.a.id, found)
        self.assertIn(self.b.id, found)

    def test_impact_reports_conclusions(self):
        result = graph.impact(self.store, self.b.id)
        self.assertEqual(result["total"], 2)
        ids = {item["id"] for item in result["conclusions"]}
        self.assertEqual(ids, {self.c.id, self.d.id})

    def test_impact_of_leaf_is_empty(self):
        result = graph.impact(self.store, self.c.id)
        self.assertEqual(result["total"], 0)


class TestBlame(GraphTestCase):
    def test_blame_walks_to_the_premise(self):
        node = graph.blame(self.store, self.c.id)
        self.assertEqual(node.assertion.id, self.c.id)
        self.assertEqual(len(node.children), 1)
        self.assertEqual(node.children[0].assertion.id, self.b.id)

    def test_blame_stops_at_bedrock(self):
        node = graph.blame(self.store, self.c.id)
        self.assertEqual(node.children[0].children, [])

    def test_blame_branching(self):
        node = graph.blame(self.store, self.d.id)
        self.assertEqual({child.assertion.id for child in node.children}, {self.a.id, self.b.id})

    def test_blame_serialises(self):
        payload = graph.blame(self.store, self.c.id).to_dict()
        self.assertIn("children", payload)
        self.assertEqual(payload["children"][0]["id"], self.b.id)


class TestConflicts(LedgerTestCase):
    def test_contradiction_is_reported(self):
        self.record(subject="project:x/db", predicate="engine", value="postgres", text="uses postgres")
        self.record(subject="project:x/db", predicate="engine", value="mysql", text="uses mysql")
        conflicts = graph.find_conflicts(self.store)
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0].key, "project:x/db.engine")
        self.assertEqual(set(conflicts[0].to_dict()["values"]), {"mysql", "postgres"})

    def test_same_value_is_not_a_conflict(self):
        self.record(subject="a", predicate="b", value="same", text="one")
        self.record(subject="a", predicate="b", value="same", text="two")
        self.assertEqual(graph.find_conflicts(self.store), [])

    def test_retraction_resolves_a_conflict(self):
        first = self.record(subject="a", predicate="b", value="postgres", text="one")
        self.record(subject="a", predicate="b", value="mysql", text="two")
        self.assertEqual(len(graph.find_conflicts(self.store)), 1)
        self.store.retract(first.id, reason="outdated")
        self.assertEqual(graph.find_conflicts(self.store), [])


class TestDiff(LedgerTestCase):
    def test_value_change_shows_as_changed_not_removed_plus_added(self):
        original = self.record(
            subject="project:x/db", predicate="engine", value="postgres",
            text="X uses postgres.", asserted_at="2026-01-01T00:00:00Z",
        )
        replacement = self.record(
            subject="project:x/db", predicate="engine", value="mysql",
            text="X uses mysql.", asserted_at="2026-05-01T00:00:00Z",
        )
        self.store.supersede(original.id, replacement.id, at="2026-06-01T00:00:00Z")

        result = graph.diff(self.store, "2026-03-01", "2026-07-01")
        self.assertEqual(len(result.changed), 1)
        before, after = result.changed[0]
        self.assertEqual(before.value, "postgres")
        self.assertEqual(after.value, "mysql")
        self.assertEqual(result.added, [])
        self.assertEqual(result.removed, [])

    def test_new_belief_shows_as_added(self):
        self.record(
            subject="a", predicate="b", value="one", text="first",
            asserted_at="2026-01-01T00:00:00Z",
        )
        result = graph.diff(self.store, "2025-12-01", "2026-02-01")
        self.assertEqual(len(result.added), 1)
        self.assertEqual(result.changed, [])

    def test_unchanged_worldview_is_empty(self):
        self.record(subject="a", predicate="b", value="one", text="first",
                    asserted_at="2026-01-01T00:00:00Z")
        result = graph.diff(self.store, "2026-01-15", "2026-02-15")
        self.assertTrue(result.empty)


if __name__ == "__main__":
    unittest.main()
