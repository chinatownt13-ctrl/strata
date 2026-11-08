"""strata.graph — the dependency graph and everything that flows through it.

`inferred` assertions point upstream at their premises. That single edge type
buys four things:

* **cascade invalidation** — when a premise is retracted, downstream conclusions
  are classified as `orphaned` (all support gone) or `at-risk` (some gone)
  instead of being silently served forever;
* **blame** — walk any conclusion back to the observations it rests on;
* **conflict detection** — two active assertions on the same `subject.predicate`
  slot with different values cannot both be true;
* **worldview diff** — what changed between two moments, expressed as beliefs
  rather than as rows.

The graph is stored as flat edges in SQLite and traversed in Python. At the scale
a personal ledger reaches (tens of thousands of edges) an in-memory BFS is both
faster and easier to reason about than recursive CTEs; the recursive query is
reserved for the one place it wins, the cycle check at write time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .model import (
    INTEGRITY_AT_RISK,
    INTEGRITY_INTACT,
    INTEGRITY_ORPHANED,
    STANDING_ACTIVE,
    Assertion,
    claim_key,
    value_key,
)
from .store import Store


@dataclass
class BlameNode:
    """One frame of a blame walk: an assertion plus how it relates to its caller."""

    assertion: Assertion
    children: List["BlameNode"] = field(default_factory=list)
    cyclic: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            **self.assertion.to_dict(),
            "children": [c.to_dict() for c in self.children],
            "cyclic": self.cyclic,
        }


@dataclass
class Conflict:
    key: str
    assertions: List[Assertion]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "values": sorted({(a.value or "∅") for a in self.assertions}),
            "assertions": [a.to_dict() for a in self.assertions],
        }


def load_edges(store: Store) -> Dict[str, List[str]]:
    """downstream id → [upstream ids]. One query, whole graph."""
    graph: Dict[str, List[str]] = {}
    for row in store.connection().execute("SELECT from_id, to_id FROM edges"):
        graph.setdefault(row["from_id"], []).append(row["to_id"])
    return graph


def _reverse(graph: Dict[str, List[str]]) -> Dict[str, List[str]]:
    reverse: Dict[str, List[str]] = {}
    for downstream, upstreams in graph.items():
        for upstream in upstreams:
            reverse.setdefault(upstream, []).append(downstream)
    return reverse


def upstream_closure(graph: Dict[str, List[str]], start: str) -> List[str]:
    """Everything `start` rests on, transitively. Nearest-first, cycle-safe."""
    found: List[str] = []
    seen: Set[str] = {start}
    frontier = list(graph.get(start, ()))
    while frontier:
        current = frontier.pop(0)
        if current in seen:
            continue
        seen.add(current)
        found.append(current)
        frontier.extend(graph.get(current, ()))
    return found


def downstream_closure(graph: Dict[str, List[str]], start: str) -> List[str]:
    """Everything that rests on `start`, transitively. Excludes `start`."""
    reverse = _reverse(graph)
    found: List[str] = []
    seen: Set[str] = {start}
    frontier = list(reverse.get(start, ()))
    while frontier:
        current = frontier.pop(0)
        if current in seen:
            continue
        seen.add(current)
        found.append(current)
        frontier.extend(reverse.get(current, ()))
    return found


def compute_integrity(
    store: Store,
    assertions: Optional[Dict[str, Assertion]] = None,
    graph: Optional[Dict[str, List[str]]] = None,
) -> Dict[str, str]:
    """Assign `intact` / `at-risk` / `orphaned` to every assertion in the ledger.

    A belief is *supported* upstream when that upstream assertion is still active
    and is not itself orphaned. `at-risk` deliberately propagates: a conclusion
    resting on an at-risk premise is itself weakened, because the user deserves
    to see the whole rotting branch, not just the first bad joint.
    """
    graph = graph if graph is not None else load_edges(store)
    if assertions is None:
        assertions = {a.id: a for a in store.hydrate(store.all_ids())}

    memo: Dict[str, str] = {}
    in_progress: Set[str] = set()

    def resolve(assertion_id: str) -> str:
        if assertion_id in memo:
            return memo[assertion_id]
        if assertion_id in in_progress:
            # Should be impossible — writes reject cycles — but a corrupted file
            # must not hang the reader.
            return INTEGRITY_AT_RISK
        in_progress.add(assertion_id)

        assertion = assertions.get(assertion_id)
        if assertion is None:
            in_progress.discard(assertion_id)
            memo[assertion_id] = INTEGRITY_ORPHANED
            return INTEGRITY_ORPHANED

        if assertion.status != "inferred":
            result = INTEGRITY_INTACT
        else:
            premises = graph.get(assertion_id, [])
            if not premises:
                result = INTEGRITY_ORPHANED
            else:
                verdicts = []
                for premise_id in premises:
                    premise = assertions.get(premise_id)
                    if premise is None:
                        verdicts.append(INTEGRITY_ORPHANED)
                        continue
                    if premise.standing != STANDING_ACTIVE:
                        verdicts.append(INTEGRITY_ORPHANED)
                        continue
                    verdicts.append(resolve(premise_id))
                if all(v == INTEGRITY_ORPHANED for v in verdicts):
                    result = INTEGRITY_ORPHANED
                elif all(v == INTEGRITY_INTACT for v in verdicts):
                    result = INTEGRITY_INTACT
                else:
                    result = INTEGRITY_AT_RISK

        in_progress.discard(assertion_id)
        memo[assertion_id] = result
        return result

    for assertion_id in assertions:
        resolve(assertion_id)
    return memo


def annotate_integrity(
    store: Store, assertions: Sequence[Assertion]
) -> Sequence[Assertion]:
    """Stamp integrity onto already-hydrated assertions (one graph load)."""
    if not assertions:
        return assertions
    graph = load_edges(store)
    lookup = {a.id: a for a in store.hydrate(store.all_ids())}
    verdicts = compute_integrity(store, lookup, graph)
    for assertion in assertions:
        assertion.integrity = verdicts.get(assertion.id, INTEGRITY_INTACT)
    return assertions


def find_orphans(store: Store, *, include_downstream: bool = False) -> List[Assertion]:
    """Conclusions that have lost every premise they depended on."""
    everything = store.hydrate(store.all_ids())
    verdicts = compute_integrity(store, {a.id: a for a in everything})
    orphans = [
        a
        for a in everything
        if verdicts.get(a.id) == INTEGRITY_ORPHANED
        and a.status == "inferred"
        and a.standing == STANDING_ACTIVE
    ]
    if include_downstream:
        graph = load_edges(store)
        extra: Dict[str, Assertion] = {}
        by_id = {a.id: a for a in everything}
        for orphan in orphans:
            for descendant in downstream_closure(graph, orphan.id):
                candidate = by_id.get(descendant)
                if candidate and candidate is not orphan and candidate.standing == STANDING_ACTIVE:
                    extra[candidate.id] = candidate
        known = {a.id for a in orphans}
        orphans.extend(a for a in extra.values() if a.id not in known)
    for orphan in orphans:
        orphan.integrity = INTEGRITY_ORPHANED
    return sorted(orphans, key=lambda a: (a.subject, a.predicate))


def find_at_risk(store: Store) -> List[Assertion]:
    """Conclusions still standing, but on fewer legs than they were."""
    everything = store.hydrate(store.all_ids())
    verdicts = compute_integrity(store, {a.id: a for a in everything})
    risky = [
        a
        for a in everything
        if verdicts.get(a.id) == INTEGRITY_AT_RISK
        and a.status == "inferred"
        and a.standing == STANDING_ACTIVE
    ]
    for assertion in risky:
        assertion.integrity = INTEGRITY_AT_RISK
    return sorted(risky, key=lambda a: (a.subject, a.predicate))


def blame(
    store: Store,
    assertion_id: str,
    *,
    max_depth: int = 6,
    max_branches: int = 12,
) -> BlameNode:
    """Walk a conclusion back to its premises, recursively.

    Bedrock assertions (observed / reported / assumed) have no children — the
    walk stops where the evidence stops being derived, which is exactly the
    boundary a human auditor cares about.
    """
    graph = load_edges(store)
    cache: Dict[str, Assertion] = {}

    def load(target: str) -> Assertion:
        if target not in cache:
            cache[target] = store.get(target)
        return cache[target]

    def walk(target: str, depth: int, path: Tuple[str, ...]) -> BlameNode:
        assertion = load(target)
        node = BlameNode(assertion=assertion)
        if depth >= max_depth:
            return node
        for premise_id in graph.get(target, [])[:max_branches]:
            if premise_id in path:
                node.cyclic = True
                continue
            node.children.append(walk(premise_id, depth + 1, path + (premise_id,)))
        return node

    return walk(store._resolve_id(assertion_id), 0, (store._resolve_id(assertion_id),))


def impact(store: Store, assertion_id: str) -> Dict[str, Any]:
    """What breaks if this assertion turns out to be wrong?

    This is the question that makes retraction safe to consider. It answers with
    the full downstream set, split by whether each dependent is load-bearing
    (inferred) or merely referential (superseded-by, which is structural).
    """
    graph = load_edges(store)
    resolved = store._resolve_id(assertion_id)
    downstream = downstream_closure(graph, resolved)
    assertions = {a.id: a for a in store.hydrate(downstream)} if downstream else {}
    conclusions = [a for a in assertions.values() if a.status == "inferred"]
    other = [a for a in assertions.values() if a.status != "inferred"]

    return {
        "id": resolved,
        "total": len(downstream),
        "conclusions": [a.to_dict() for a in sorted(conclusions, key=lambda a: a.id)],
        "referencing": [a.to_dict() for a in sorted(other, key=lambda a: a.id)],
    }


def find_conflicts(store: Store, *, limit: int = 200) -> List[Conflict]:
    """Active assertions competing for the same slot with different values.

    Two active claims on `project:acme/db.engine` — one saying postgres, one
    saying mysql — is not a ranking problem. It is a contradiction, and strata
    reports it as one instead of letting recency or cosine score decide.
    """
    active = store.query(standing="active", limit=limit * 10)
    buckets: Dict[str, List[Assertion]] = {}
    for assertion in active:
        buckets.setdefault(claim_key(assertion.subject, assertion.predicate), []).append(
            assertion
        )

    conflicts: List[Conflict] = []
    for key, group in buckets.items():
        if len(group) < 2:
            continue
        distinct_values = {value_key(a.value) for a in group}
        if len(distinct_values) > 1:
            conflicts.append(Conflict(key=key, assertions=sorted(group, key=lambda a: a.id)))
    return sorted(conflicts, key=lambda c: c.key)


def snapshot(store: Store, at: str) -> Dict[str, Assertion]:
    """What the ledger believed at a given moment."""
    return {a.id: a for a in store.query(standing="all", as_of=at, limit=100000)}


@dataclass
class WorldviewDiff:
    added: List[Assertion] = field(default_factory=list)
    removed: List[Assertion] = field(default_factory=list)
    changed: List[Tuple[Assertion, Assertion]] = field(default_factory=list)
    superseded: List[Assertion] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.added or self.removed or self.changed or self.superseded)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "added": [a.to_dict() for a in self.added],
            "removed": [a.to_dict() for a in self.removed],
            "changed": [
                {"from": old.to_dict(), "to": new.to_dict()} for old, new in self.changed
            ],
            "superseded": [a.to_dict() for a in self.superseded],
            "summary": {
                "added": len(self.added),
                "removed": len(self.removed),
                "changed": len(self.changed),
                "superseded": len(self.superseded),
            },
        }


def diff(store: Store, before: str, after: str) -> WorldviewDiff:
    """Compare two moments as worldviews, not as row sets.

    A belief that changed value on the same slot shows up under `changed` rather
    than as one removal plus one addition, because that is what actually
    happened and it is what a reader needs to see.
    """
    first = snapshot(store, before)
    second = snapshot(store, after)
    result = WorldviewDiff()

    first_keys = {
        claim_key(a.subject, a.predicate): a for a in first.values()
    }
    second_keys = {
        claim_key(a.subject, a.predicate): a for a in second.values()
    }

    for assertion_id, assertion in second.items():
        if assertion_id not in first:
            prior = first_keys.get(claim_key(assertion.subject, assertion.predicate))
            if prior is not None and value_key(prior.value) != value_key(assertion.value):
                result.changed.append((prior, assertion))
            else:
                result.added.append(assertion)

    for assertion_id, assertion in first.items():
        if assertion_id not in second:
            successor = second_keys.get(claim_key(assertion.subject, assertion.predicate))
            if successor is not None and value_key(successor.value) != value_key(assertion.value):
                continue  # already reported as `changed`
            if assertion.standing != STANDING_ACTIVE:
                result.superseded.append(assertion)
            else:
                result.removed.append(assertion)

    result.added.sort(key=lambda a: (a.subject, a.predicate))
    result.removed.sort(key=lambda a: (a.subject, a.predicate))
    result.changed.sort(key=lambda pair: (pair[1].subject, pair[1].predicate))
    return result
