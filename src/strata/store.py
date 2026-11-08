"""strata.store — the SQLite ledger.

Design in one paragraph: the `assertions` table holds propositions and is
insert-only. Everything that can change about a belief — whether it still
stands, what replaced it, how many witnesses it has — lives in `events`, which
is also append-only. Current state is *derived* by folding each assertion's
event stream. Nothing is ever updated in place and nothing is ever deleted,
which is what makes `log`, `diff` and `blame` possible at all.

The cost is a fold per read. With batch hydration (all events for a page of
assertions in one query) it stays well under a millisecond per assertion at
six-figure scale, which is the right trade for a ledger whose main job is being
trustworthy.
"""

from __future__ import annotations

import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from . import model
from .model import (
    Assertion,
    Evidence,
    Event,
    Falsifier,
    INTEGRITY_INTACT,
    STANDING_ACTIVE,
    STANDING_RETRACTED,
    STANDING_SUPERSEDED,
    STATUS_RANK,
    claim_key,
    compute_id,
    now_iso,
    parse_time,
    to_iso,
)

SCHEMA_VERSION = "1"

DEFAULT_FILENAME = ".strata"

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS assertions (
    id         TEXT PRIMARY KEY,
    subject    TEXT NOT NULL,
    predicate  TEXT NOT NULL,
    value      TEXT,
    value_key  TEXT,
    text       TEXT NOT NULL,
    note       TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_assertions_key  ON assertions(subject, predicate);
CREATE INDEX IF NOT EXISTS idx_assertions_vkey ON assertions(subject, predicate, value_key);

CREATE TABLE IF NOT EXISTS events (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT NOT NULL,
    assertion_id TEXT NOT NULL,
    at           TEXT NOT NULL,
    actor        TEXT NOT NULL,
    data         TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_events_assertion ON events(assertion_id, seq);
CREATE INDEX IF NOT EXISTS idx_events_at        ON events(at, seq);

CREATE TABLE IF NOT EXISTS evidence (
    assertion_id TEXT NOT NULL,
    seq          INTEGER NOT NULL,
    kind         TEXT NOT NULL,
    locator      TEXT NOT NULL,
    excerpt      TEXT,
    digest       TEXT NOT NULL,
    captured_at  TEXT,
    PRIMARY KEY (assertion_id, seq)
);

CREATE TABLE IF NOT EXISTS falsifiers (
    assertion_id TEXT NOT NULL,
    seq          INTEGER NOT NULL,
    kind         TEXT NOT NULL,
    spec         TEXT NOT NULL,
    digest       TEXT NOT NULL,
    last_checked TEXT,
    last_result  TEXT,
    PRIMARY KEY (assertion_id, seq)
);

CREATE TABLE IF NOT EXISTS edges (
    from_id TEXT NOT NULL,
    to_id   TEXT NOT NULL,
    PRIMARY KEY (from_id, to_id)
);

CREATE INDEX IF NOT EXISTS idx_edges_to ON edges(to_id);
"""


class StrataError(Exception):
    """Base class for errors that should reach the user as a clean message."""

    exit_code = 1


class NotFound(StrataError):
    exit_code = 1


class AmbiguousId(StrataError):
    exit_code = 4


class CycleDetected(StrataError):
    exit_code = 4


class Conflict(StrataError):
    exit_code = 4


class UsageError(StrataError):
    """Malformed input: missing text, empty subject, unknown status.

    Distinct from :class:`Conflict` (a real semantic clash: cycles, ambiguity).
    Both are the caller's fault, but scripts need to tell "you typed it wrong"
    apart from "the ledger refuses to do this".
    """

    exit_code = 2


def _chunked(items: Sequence[Any], size: int = 400) -> Iterator[Sequence[Any]]:
    """SQLite caps bind variables; batch `IN (...)` clauses to stay well under."""
    for start in range(0, len(items), size):
        yield items[start : start + size]


def find_db_path(explicit: Optional[str] = None) -> str:
    """Resolve which database to use.

    Order: explicit argument → ``$STRATA_DB`` → walk up from cwd looking for a
    ``.strata`` file (so you can run commands from a subdirectory, like git).
    """
    if explicit:
        return os.path.abspath(os.path.expanduser(explicit))

    env = os.environ.get("STRATA_DB")
    if env:
        return os.path.abspath(os.path.expanduser(env))

    current = os.path.abspath(os.getcwd())
    while True:
        candidate = os.path.join(current, DEFAULT_FILENAME)
        if os.path.isfile(candidate):
            return candidate
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return os.path.join(os.getcwd(), DEFAULT_FILENAME)


class Store:
    """A handle on one `.strata` file."""

    def __init__(self, path: str):
        self.path = path
        self._conn: Optional[sqlite3.Connection] = None

    # -- lifecycle ---------------------------------------------------------

    @classmethod
    def create(cls, path: str) -> "Store":
        store = cls(path)
        store.connection()  # opens and initialises
        with store.transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES(?, ?)",
                ("schema_version", SCHEMA_VERSION),
            )
            conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES(?, ?)",
                ("instance_id", uuid.uuid4().hex[:12]),
            )
            conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES(?, ?)",
                ("created_at", now_iso()),
            )
        return store

    @classmethod
    def open(cls, path: str, *, must_exist: bool = True) -> "Store":
        if must_exist and not os.path.isfile(path):
            raise NotFound(
                f"no strata database at {path}\n"
                f"  run `strata init` to create one, or pass --db"
            )
        return cls(path)

    @property
    def exists(self) -> bool:
        return os.path.isfile(self.path)

    def connection(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        directory = os.path.dirname(self.path)
        if directory and not os.path.isdir(directory):
            os.makedirs(directory, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(SCHEMA)
        self._conn = conn
        version = self.meta("schema_version")
        if version is None:
            self.set_meta("schema_version", SCHEMA_VERSION)
        elif version != SCHEMA_VERSION:
            raise StrataError(
                f"database schema is v{version}, this build expects v{SCHEMA_VERSION}"
            )
        return conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "Store":
        self.connection()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self.connection()
        try:
            yield conn
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()

    # -- meta --------------------------------------------------------------

    def meta(self, key: str) -> Optional[str]:
        row = self.connection().execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    # -- writes ------------------------------------------------------------

    def assert_claim(
        self,
        *,
        subject: str,
        predicate: str,
        text: str,
        status: str = model.STATUS_OBSERVED,
        actor: str = "human:local",
        value: Optional[str] = None,
        evidence: Sequence[Evidence] = (),
        falsifiers: Sequence[Falsifier] = (),
        depends_on: Sequence[str] = (),
        valid_from: Optional[str] = None,
        valid_to: Optional[str] = None,
        note: Optional[str] = None,
        asserted_at: Optional[str] = None,
        supersedes: Optional[str] = None,
    ) -> Tuple[Assertion, bool]:
        """Record a claim. Returns ``(assertion, was_new)``.

        Idempotent by construction: asserting the same proposition twice adds a
        testimony and any new evidence, but never a second row.
        """
        if status not in model.STATUSES:
            raise UsageError(
                f"unknown status {status!r}; expected one of {', '.join(model.STATUSES)}"
            )
        if status == model.STATUS_INFERRED and not depends_on:
            raise UsageError(
                "an `inferred` assertion must declare at least one --depends-on; "
                "a conclusion with no stated premises cannot be audited"
            )

        subject_n = model.normalize_subject(subject)
        predicate_n = model.normalize_predicate(predicate)
        value_n = model.normalize_value(value)
        text_n = model.normalize_text(text)
        if not subject_n:
            raise UsageError("subject must not be empty")
        if not predicate_n:
            raise UsageError("predicate must not be empty")
        if not text_n:
            raise UsageError("text must not be empty")

        assertion_id = compute_id(subject_n, predicate_n, value_n, text_n)
        timestamp = asserted_at or now_iso()

        resolved_deps = [self._resolve_id(dep) for dep in depends_on]
        for dep in resolved_deps:
            if dep == assertion_id:
                raise UsageError(f"assertion {assertion_id} cannot depend on itself")
            if self._reaches(dep, assertion_id):
                raise CycleDetected(
                    f"adding {assertion_id} → {dep} would create a dependency cycle"
                )

        with self.transaction() as conn:
            existing = conn.execute(
                "SELECT id FROM assertions WHERE id = ?", (assertion_id,)
            ).fetchone()
            was_new = existing is None

            if was_new:
                conn.execute(
                    "INSERT INTO assertions(id, subject, predicate, value, value_key, text, note, created_at) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        assertion_id,
                        subject_n,
                        predicate_n,
                        value_n,
                        model.value_key(value_n),
                        text_n,
                        note,
                        timestamp,
                    ),
                )
            elif note:
                conn.execute(
                    "UPDATE assertions SET note = COALESCE(note, ?) WHERE id = ?",
                    (note, assertion_id),
                )

            payload = {
                "status": status,
                "valid_from": valid_from,
                "valid_to": valid_to,
                "note": note,
            }
            conn.execute(
                "INSERT INTO events(kind, assertion_id, at, actor, data) VALUES(?, ?, ?, ?, ?)",
                (
                    model.EVENT_ASSERT,
                    assertion_id,
                    timestamp,
                    actor,
                    model.canonical_json(payload),
                ),
            )

            self._append_evidence(conn, assertion_id, evidence)
            self._append_falsifiers(conn, assertion_id, falsifiers)

            for dep in resolved_deps:
                conn.execute(
                    "INSERT OR IGNORE INTO edges(from_id, to_id) VALUES(?, ?)",
                    (assertion_id, dep),
                )

        if supersedes:
            self.supersede(supersedes, assertion_id, actor=actor, reason=note)

        return self.get(assertion_id), was_new

    def retract(
        self,
        assertion_id: str,
        *,
        reason: str,
        actor: str = "human:local",
        at: Optional[str] = None,
    ) -> Assertion:
        """Withdraw a belief. The row and its history survive; the standing does not."""
        resolved = self._resolve_id(assertion_id)
        if not reason or not reason.strip():
            raise UsageError(
                "retraction requires a reason; an unexplained retraction cannot be audited"
            )
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO events(kind, assertion_id, at, actor, data) VALUES(?, ?, ?, ?, ?)",
                (
                    model.EVENT_RETRACT,
                    resolved,
                    at or now_iso(),
                    actor,
                    model.canonical_json({"reason": reason.strip()}),
                ),
            )
        return self.get(resolved)

    def supersede(
        self,
        old_id: str,
        new_id: str,
        *,
        actor: str = "human:local",
        reason: Optional[str] = None,
        at: Optional[str] = None,
    ) -> Tuple[Assertion, Assertion]:
        """Declare that `new_id` replaces `old_id`, closing the old valid time."""
        old = self._resolve_id(old_id)
        new = self._resolve_id(new_id)
        if old == new:
            raise UsageError("an assertion cannot supersede itself")

        closed_at = at or now_iso()
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO events(kind, assertion_id, at, actor, data) VALUES(?, ?, ?, ?, ?)",
                (
                    model.EVENT_SUPERSEDE,
                    old,
                    closed_at,
                    actor,
                    model.canonical_json(
                        {"by": new, "reason": reason, "closes_valid_at": closed_at}
                    ),
                ),
            )
            conn.execute(
                "INSERT OR IGNORE INTO edges(from_id, to_id) VALUES(?, ?)", (new, old)
            )
        return self.get(old), self.get(new)

    def annotate(
        self,
        assertion_id: str,
        note: str,
        *,
        actor: str = "human:local",
        at: Optional[str] = None,
    ) -> Event:
        """Attach a free-form remark. Does not change standing."""
        resolved = self._resolve_id(assertion_id)
        if not note or not note.strip():
            raise UsageError("annotation must not be empty")
        timestamp = at or now_iso()
        with self.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO events(kind, assertion_id, at, actor, data) VALUES(?, ?, ?, ?, ?)",
                (
                    model.EVENT_ANNOTATE,
                    resolved,
                    timestamp,
                    actor,
                    model.canonical_json({"note": note.strip()}),
                ),
            )
            seq = int(cursor.lastrowid)
        return Event(seq, model.EVENT_ANNOTATE, resolved, timestamp, actor, {"note": note.strip()})

    def add_evidence(self, assertion_id: str, evidence: Sequence[Evidence]) -> Assertion:
        """Append evidence to an existing belief without re-asserting it."""
        resolved = self._resolve_id(assertion_id)
        with self.transaction() as conn:
            self._append_evidence(conn, resolved, evidence)
        return self.get(resolved)

    def add_falsifier(self, assertion_id: str, falsifier: Falsifier) -> Assertion:
        resolved = self._resolve_id(assertion_id)
        with self.transaction() as conn:
            self._append_falsifiers(conn, resolved, [falsifier])
        return self.get(resolved)

    def record_check(self, assertion_id: str, result: str, at: Optional[str] = None) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE falsifiers SET last_checked = ?, last_result = ? WHERE assertion_id = ?",
                (at or now_iso(), result, assertion_id),
            )

    # -- internals ---------------------------------------------------------

    def _append_evidence(
        self, conn: sqlite3.Connection, assertion_id: str, evidence: Sequence[Evidence]
    ) -> None:
        if not evidence:
            return
        row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) AS n FROM evidence WHERE assertion_id = ?",
            (assertion_id,),
        ).fetchone()
        next_seq = int(row["n"])
        seen = {
            r["digest"]
            for r in conn.execute(
                "SELECT digest FROM evidence WHERE assertion_id = ?", (assertion_id,)
            )
        }
        for item in evidence:
            if item.digest in seen:
                continue  # evidence is content-addressed; duplicates carry no signal
            seen.add(item.digest)
            next_seq += 1
            conn.execute(
                "INSERT INTO evidence(assertion_id, seq, kind, locator, excerpt, digest, captured_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?)",
                (
                    assertion_id,
                    next_seq,
                    item.kind,
                    item.locator,
                    item.excerpt,
                    item.digest,
                    item.captured_at or now_iso(),
                ),
            )

    def _append_falsifiers(
        self, conn: sqlite3.Connection, assertion_id: str, falsifiers: Sequence[Falsifier]
    ) -> None:
        if not falsifiers:
            return
        row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) AS n FROM falsifiers WHERE assertion_id = ?",
            (assertion_id,),
        ).fetchone()
        next_seq = int(row["n"])
        seen = {
            r["digest"]
            for r in conn.execute(
                "SELECT digest FROM falsifiers WHERE assertion_id = ?", (assertion_id,)
            )
        }
        for item in falsifiers:
            if item.digest in seen:
                continue
            seen.add(item.digest)
            next_seq += 1
            conn.execute(
                "INSERT INTO falsifiers(assertion_id, seq, kind, spec, digest) VALUES(?, ?, ?, ?, ?)",
                (
                    assertion_id,
                    next_seq,
                    item.kind,
                    model.canonical_json(item.spec),
                    item.digest,
                ),
            )

    def _resolve_id(self, id_or_prefix: str) -> str:
        """Resolve an id prefix, raising a useful error on ambiguity."""
        if not id_or_prefix:
            raise NotFound("empty assertion id")
        if len(id_or_prefix) == model.ID_LEN and self._row_exists(id_or_prefix):
            return id_or_prefix
        conn = self.connection()
        rows = conn.execute(
            "SELECT id FROM assertions WHERE id LIKE ? ORDER BY id LIMIT 8",
            (id_or_prefix.replace("%", "").replace("_", "") + "%",),
        ).fetchall()
        if not rows:
            raise NotFound(f"no assertion matching {id_or_prefix!r}")
        if len(rows) == 1:
            return rows[0]["id"]
        if len(rows) > 1:
            exact = [r["id"] for r in rows if r["id"] == id_or_prefix]
            if exact:
                return exact[0]
            listing = ", ".join(r["id"][:12] for r in rows[:5])
            raise AmbiguousId(
                f"{id_or_prefix!r} matches {len(rows)} assertions: {listing}"
                + (" …" if len(rows) > 5 else "")
            )
        raise NotFound(f"no assertion matching {id_or_prefix!r}")

    def _row_exists(self, assertion_id: str) -> bool:
        return (
            self.connection()
            .execute("SELECT 1 FROM assertions WHERE id = ?", (assertion_id,))
            .fetchone()
            is not None
        )

    def _reaches(self, start: str, target: str) -> bool:
        """True if `start` transitively depends on `target` (used for cycle checks)."""
        worklist = [start]
        seen = set()
        conn = self.connection()
        while worklist:
            node = worklist.pop()
            if node in seen:
                continue
            seen.add(node)
            for row in conn.execute(
                "SELECT to_id FROM edges WHERE from_id = ?", (node,)
            ):
                dependency = row["to_id"]
                if dependency == target:
                    return True
                if dependency not in seen:
                    worklist.append(dependency)
        return False

    # -- reads -------------------------------------------------------------

    def get(self, id_or_prefix: str) -> Assertion:
        resolved = self._resolve_id(id_or_prefix)
        results = self.hydrate([resolved])
        if not results:
            raise NotFound(f"assertion {id_or_prefix} disappeared during read")
        return results[0]

    def hydrate(self, ids: Sequence[str]) -> List[Assertion]:
        """Batch-load assertions with their events, evidence, falsifiers and edges.

        One round trip per table regardless of how many ids are requested — this
        is what keeps `log` and `query` fast.
        """
        if not ids:
            return []
        conn = self.connection()
        unique = list(dict.fromkeys(ids))

        rows: Dict[str, sqlite3.Row] = {}
        for chunk in _chunked(unique):
            placeholders = ",".join("?" * len(chunk))
            for row in conn.execute(
                f"SELECT * FROM assertions WHERE id IN ({placeholders})", tuple(chunk)
            ):
                rows[row["id"]] = row

        events = self._events_by_assertion(unique)
        evidence = self._evidence_by_assertion(unique)
        falsifiers = self._falsifiers_by_assertion(unique)
        deps = self._edges_by_assertion(unique)

        out: List[Assertion] = []
        for assertion_id in unique:
            row = rows.get(assertion_id)
            if row is None:
                continue
            out.append(
                self._fold(
                    row,
                    events.get(assertion_id, []),
                    evidence.get(assertion_id, []),
                    falsifiers.get(assertion_id, []),
                    deps.get(assertion_id, []),
                )
            )
        return out

    def _events_by_assertion(self, ids: Sequence[str]) -> Dict[str, List[Event]]:
        conn = self.connection()
        grouped: Dict[str, List[Event]] = {}
        for chunk in _chunked(ids):
            placeholders = ",".join("?" * len(chunk))
            for row in conn.execute(
                f"SELECT seq, kind, assertion_id, at, actor, data FROM events "
                f"WHERE assertion_id IN ({placeholders}) ORDER BY seq",
                tuple(chunk),
            ):
                grouped.setdefault(row["assertion_id"], []).append(
                    Event(
                        seq=int(row["seq"]),
                        kind=row["kind"],
                        assertion_id=row["assertion_id"],
                        at=row["at"],
                        actor=row["actor"],
                        data=_load_json(row["data"]),
                    )
                )
        return grouped

    def _evidence_by_assertion(self, ids: Sequence[str]) -> Dict[str, List[Evidence]]:
        conn = self.connection()
        grouped: Dict[str, List[Evidence]] = {}
        for chunk in _chunked(ids):
            placeholders = ",".join("?" * len(chunk))
            for row in conn.execute(
                f"SELECT * FROM evidence WHERE assertion_id IN ({placeholders}) "
                f"ORDER BY assertion_id, seq",
                tuple(chunk),
            ):
                grouped.setdefault(row["assertion_id"], []).append(
                    Evidence(
                        kind=row["kind"],
                        locator=row["locator"],
                        excerpt=row["excerpt"],
                        captured_at=row["captured_at"],
                    )
                )
        return grouped

    def _falsifiers_by_assertion(self, ids: Sequence[str]) -> Dict[str, List[Falsifier]]:
        conn = self.connection()
        grouped: Dict[str, List[Falsifier]] = {}
        for chunk in _chunked(ids):
            placeholders = ",".join("?" * len(chunk))
            for row in conn.execute(
                f"SELECT * FROM falsifiers WHERE assertion_id IN ({placeholders}) "
                f"ORDER BY assertion_id, seq",
                tuple(chunk),
            ):
                grouped.setdefault(row["assertion_id"], []).append(
                    Falsifier(kind=row["kind"], spec=_load_json(row["spec"]))
                )
        return grouped

    def _edges_by_assertion(self, ids: Sequence[str]) -> Dict[str, List[str]]:
        conn = self.connection()
        grouped: Dict[str, List[str]] = {}
        for chunk in _chunked(ids):
            placeholders = ",".join("?" * len(chunk))
            for row in conn.execute(
                f"SELECT from_id, to_id FROM edges WHERE from_id IN ({placeholders}) "
                f"ORDER BY to_id",
                tuple(chunk),
            ):
                grouped.setdefault(row["from_id"], []).append(row["to_id"])
        return grouped

    def _fold(
        self,
        row: sqlite3.Row,
        events: Sequence[Event],
        evidence: Sequence[Evidence],
        falsifiers: Sequence[Falsifier],
        dependencies: Sequence[str],
    ) -> Assertion:
        """Collapse an event stream into the assertion's current state.

        This is the only place where 'what a belief means right now' is decided.
        Keep it boring and total: every event kind must be handled.
        """
        status = model.STATUS_ASSUMED
        testimonials = 0
        valid_from: Optional[str] = None
        valid_to: Optional[str] = None
        retracted_at: Optional[str] = None
        retract_reason: Optional[str] = None
        superseded_by: Optional[str] = None
        superseded_at: Optional[str] = None
        latest_assert_at = row["created_at"]
        actor = "human:local"

        for event in events:
            if event.kind == model.EVENT_ASSERT:
                testimonials += 1
                actor = event.actor
                latest_assert_at = event.at
                claimed = event.data.get("status")
                if claimed in STATUS_RANK and STATUS_RANK[claimed] >= STATUS_RANK[status]:
                    status = claimed
                if event.data.get("valid_from"):
                    valid_from = event.data["valid_from"]
                if event.data.get("valid_to"):
                    valid_to = event.data["valid_to"]
                # A re-assertion after retraction restores the belief: someone
                # put the claim forward again, on the record.
                retracted_at = None
                retract_reason = None
            elif event.kind == model.EVENT_RETRACT:
                retracted_at = event.at
                retract_reason = event.data.get("reason")
            elif event.kind == model.EVENT_SUPERSEDE:
                superseded_by = event.data.get("by")
                superseded_at = event.at
                if event.data.get("closes_valid_at"):
                    valid_to = valid_to or event.data["closes_valid_at"]

        if testimonials == 0:  # pragma: no cover - defensive
            testimonials = 1

        if superseded_by:
            standing = STANDING_SUPERSEDED
        elif retracted_at:
            standing = STANDING_RETRACTED
        else:
            standing = STANDING_ACTIVE

        return Assertion(
            id=row["id"],
            subject=row["subject"],
            predicate=row["predicate"],
            text=row["text"],
            status=status,
            asserted_at=latest_assert_at,
            actor=actor,
            value=row["value"],
            valid_from=valid_from,
            valid_to=valid_to,
            note=row["note"],
            evidence=list(evidence),
            falsifiers=list(falsifiers),
            depends_on=list(dependencies),
            standing=standing,
            integrity=INTEGRITY_INTACT,
            testimonials=testimonials,
            superseded_by=superseded_by,
            # A superseded assertion is also "closed" — surface when that happened
            # so renderers can say "replaced 3d ago" rather than nothing.
            retracted_at=retracted_at or superseded_at,
            retract_reason=retract_reason,
        )

    def query(
        self,
        *,
        subject: Optional[str] = None,
        predicate: Optional[str] = None,
        key: Optional[str] = None,
        status: Optional[str] = None,
        standing: str = "active",
        text: Optional[str] = None,
        actor: Optional[str] = None,
        as_of: Optional[str] = None,
        valid_at: Optional[str] = None,
        limit: int = 200,
        order: str = "recent",
    ) -> List[Assertion]:
        """Filtered listing. `standing` defaults to 'active'; pass 'all' for everything."""
        sql = ["SELECT a.id FROM assertions a"]
        params: List[Any] = []
        where: List[str] = []

        if subject:
            where.append("a.subject = ?")
            params.append(model.normalize_subject(subject))
        if predicate:
            where.append("a.predicate = ?")
            params.append(model.normalize_predicate(predicate))
        if key and not (subject or predicate):
            if "." not in key:
                raise UsageError("--key must look like subject.predicate")
            key_subject, key_predicate = key.rsplit(".", 1)
            where.append("a.subject = ? AND a.predicate = ?")
            params.extend(
                [model.normalize_subject(key_subject), model.normalize_predicate(key_predicate)]
            )
        if text:
            for token in model.normalize_text(text).split():
                where.append("a.text LIKE ?")
                params.append(f"%{token}%")

        if where:
            sql.append("WHERE " + " AND ".join(where))
        else:
            # Everything below appends `AND ...`; an unconditional WHERE keeps
            # the statement valid when no filters were given.
            sql.append("WHERE 1=1")

        # Temporal filtering happens in SQL on the event log so that `--as-of`
        # reflects what was believed then, not what exists now.
        if as_of:
            cutoff = to_iso(parse_time(as_of))
            sql.append(
                "AND EXISTS (SELECT 1 FROM events e WHERE e.assertion_id = a.id "
                "AND e.kind = 'assert' AND e.at <= ?)"
            )
            params.append(cutoff)
            sql.append(
                "AND NOT EXISTS (SELECT 1 FROM events e2 WHERE e2.assertion_id = a.id "
                "AND e2.kind = 'retract' AND e2.at <= ? "
                "AND e2.seq > (SELECT COALESCE(MAX(e3.seq), 0) FROM events e3 "
                "              WHERE e3.assertion_id = a.id AND e3.kind = 'assert' "
                "              AND e3.at <= ?))"
            )
            params.extend([cutoff, cutoff])
        elif standing != "all":
            sql.append(
                "AND EXISTS (SELECT 1 FROM events e WHERE e.assertion_id = a.id "
                "AND e.kind = 'assert')"
            )
            if standing == "active":
                sql.append(
                    "AND NOT EXISTS (SELECT 1 FROM events e4 WHERE e4.assertion_id = a.id "
                    "AND e4.kind IN ('retract', 'supersede'))"
                )
            elif standing == "retracted":
                sql.append(
                    "AND EXISTS (SELECT 1 FROM events e5 WHERE e5.assertion_id = a.id "
                    "AND e5.kind = 'retract')"
                )
                sql.append(
                    "AND NOT EXISTS (SELECT 1 FROM events e6 WHERE e6.assertion_id = a.id "
                    "AND e6.kind = 'supersede')"
                )
            elif standing == "superseded":
                sql.append(
                    "AND EXISTS (SELECT 1 FROM events e7 WHERE e7.assertion_id = a.id "
                    "AND e7.kind = 'supersede')"
                )
            else:
                raise UsageError(
                    f"unknown standing {standing!r}; expected active, retracted, superseded or all"
                )

        sql.append("ORDER BY a.created_at DESC, a.id" if order == "recent" else "ORDER BY a.subject, a.predicate, a.id")
        sql.append("LIMIT ?")
        params.append(max(1, limit))

        conn = self.connection()
        ids = [row["id"] for row in conn.execute(" ".join(sql), tuple(params))]
        assertions = self.hydrate(ids)

        if status:
            assertions = [a for a in assertions if a.status == status]
        if actor:
            assertions = [a for a in assertions if a.actor == actor]
        if valid_at:
            moment = to_iso(parse_time(valid_at))
            assertions = [
                a
                for a in assertions
                if (a.valid_from is None or a.valid_from <= moment)
                and (a.valid_to is None or a.valid_to >= moment)
            ]
        return assertions

    def events_for(self, assertion_id: str) -> List[Event]:
        resolved = self._resolve_id(assertion_id)
        return self._events_by_assertion([resolved]).get(resolved, [])

    def recent_events(
        self, limit: int = 50, *, since: Optional[str] = None
    ) -> List[Event]:
        sql = "SELECT seq, kind, assertion_id, at, actor, data FROM events"
        params: List[Any] = []
        if since:
            sql += " WHERE at >= ?"
            params.append(to_iso(parse_time(since)))
        sql += " ORDER BY seq DESC LIMIT ?"
        params.append(max(1, limit))
        return [
            Event(
                seq=int(row["seq"]),
                kind=row["kind"],
                assertion_id=row["assertion_id"],
                at=row["at"],
                actor=row["actor"],
                data=_load_json(row["data"]),
            )
            for row in self.connection().execute(sql, tuple(params))
        ]

    def all_ids(self) -> List[str]:
        return [
            row["id"]
            for row in self.connection().execute("SELECT id FROM assertions ORDER BY id")
        ]

    # -- integrity ---------------------------------------------------------

    def check_integrity(self) -> List[Dict[str, Any]]:
        """Recompute content hashes and digests; report anything that drifted.

        Everything strata claims about auditability reduces to this function
        returning an empty list on a healthy database.
        """
        conn = self.connection()
        problems: List[Dict[str, Any]] = []

        for row in conn.execute("SELECT * FROM assertions"):
            expected = compute_id(
                row["subject"], row["predicate"], row["value"], row["text"]
            )
            if expected != row["id"]:
                problems.append(
                    {
                        "kind": "assertion-hash-mismatch",
                        "id": row["id"],
                        "expected": expected,
                        "detail": "proposition content no longer matches its id",
                    }
                )
            if row["value_key"] != model.value_key(row["value"]):
                problems.append(
                    {
                        "kind": "value-key-mismatch",
                        "id": row["id"],
                        "detail": "derived value_key is stale",
                    }
                )

        for row in conn.execute(
            "SELECT assertion_id, seq, kind, locator, excerpt, digest FROM evidence"
        ):
            recomputed = Evidence(
                kind=row["kind"], locator=row["locator"], excerpt=row["excerpt"]
            ).digest
            if recomputed != row["digest"]:
                problems.append(
                    {
                        "kind": "evidence-digest-mismatch",
                        "id": row["assertion_id"],
                        "seq": row["seq"],
                        "detail": "evidence content changed after capture",
                    }
                )

        for row in conn.execute(
            "SELECT assertion_id, seq, kind, spec, digest FROM falsifiers"
        ):
            recomputed = Falsifier(
                kind=row["kind"], spec=_load_json(row["spec"])
            ).digest
            if recomputed != row["digest"]:
                problems.append(
                    {
                        "kind": "falsifier-digest-mismatch",
                        "id": row["assertion_id"],
                        "seq": row["seq"],
                        "detail": "falsifier definition changed after capture",
                    }
                )

        known = set(self.all_ids())
        for row in conn.execute("SELECT from_id, to_id FROM edges"):
            if row["from_id"] not in known or row["to_id"] not in known:
                problems.append(
                    {
                        "kind": "dangling-edge",
                        "id": row["from_id"],
                        "detail": f"edge points at missing assertion {row['to_id']}",
                    }
                )

        for row in conn.execute("SELECT DISTINCT assertion_id FROM evidence"):
            if row["assertion_id"] not in known:
                problems.append(
                    {
                        "kind": "orphan-evidence",
                        "id": row["assertion_id"],
                        "detail": "evidence row has no assertion",
                    }
                )

        return problems

    def stats(self) -> Dict[str, Any]:
        conn = self.connection()

        def scalar(sql: str, *params: Any) -> int:
            row = conn.execute(sql, params).fetchone()
            return int(row[0]) if row and row[0] is not None else 0

        total = scalar("SELECT COUNT(*) FROM assertions")
        active = scalar(
            "SELECT COUNT(DISTINCT a.id) FROM assertions a WHERE "
            "NOT EXISTS (SELECT 1 FROM events e WHERE e.assertion_id = a.id "
            "AND e.kind IN ('retract','supersede'))"
        )
        by_status: Dict[str, int] = {}
        for row in conn.execute(
            "SELECT data FROM events WHERE kind = 'assert'"
        ):
            claimed = _load_json(row["data"]).get("status")
            if claimed:
                by_status[claimed] = by_status.get(claimed, 0) + 1

        return {
            "path": self.path,
            "assertions": total,
            "active": active,
            "retracted": scalar(
                "SELECT COUNT(DISTINCT assertion_id) FROM events WHERE kind='retract'"
            ),
            "superseded": scalar(
                "SELECT COUNT(DISTINCT assertion_id) FROM events WHERE kind='supersede'"
            ),
            "events": scalar("SELECT COUNT(*) FROM events"),
            "evidence": scalar("SELECT COUNT(*) FROM evidence"),
            "falsifiers": scalar("SELECT COUNT(*) FROM falsifiers"),
            "edges": scalar("SELECT COUNT(*) FROM edges"),
            "subjects": scalar("SELECT COUNT(DISTINCT subject) FROM assertions"),
            # Counts assert *events*, not distinct assertions — one belief asserted
            # three times contributes three. Named accordingly.
            "assert_events_by_status": by_status,
            "created_at": self.meta("created_at"),
            "schema_version": self.meta("schema_version"),
            "instance_id": self.meta("instance_id"),
        }

    # -- export / import ---------------------------------------------------

    def export_rows(self) -> Iterator[Dict[str, Any]]:
        """Yield a mergeable JSONL-friendly representation of the whole ledger."""
        ids = self.all_ids()
        for assertion in self.hydrate(ids):
            yield {
                "type": "assertion",
                "id": assertion.id,
                "subject": assertion.subject,
                "predicate": assertion.predicate,
                "value": assertion.value,
                "text": assertion.text,
                "note": assertion.note,
                "created_at": assertion.asserted_at,
                "events": [e.to_dict() for e in self.events_for(assertion.id)],
                "evidence": [
                    {
                        "kind": e.kind,
                        "locator": e.locator,
                        "excerpt": e.excerpt,
                        "digest": e.digest,
                        "captured_at": e.captured_at,
                    }
                    for e in assertion.evidence
                ],
                "falsifiers": [
                    {"kind": f.kind, "spec": f.spec, "digest": f.digest}
                    for f in assertion.falsifiers
                ],
                "depends_on": list(assertion.depends_on),
            }

    def import_rows(self, rows: Sequence[Dict[str, Any]]) -> Dict[str, int]:
        """Merge rows produced by :meth:`export_rows` into this ledger.

        Merge is idempotent and content-driven: assertions are keyed by their
        content address, events by (kind, at, actor, payload), evidence and
        falsifiers by digest. Running the same import twice changes nothing.

        Rows whose id does not match their content are **rejected rather than
        repaired** — a merge is exactly the moment when silently accepting a
        tampered record would be most damaging.
        """
        imported = 0
        skipped = 0
        rejected = 0
        events_added = 0
        conn = self.connection()

        with self.transaction() as conn:
            for row in rows:
                if not isinstance(row, dict):
                    rejected += 1
                    continue
                if row.get("type") not in (None, "assertion"):
                    continue
                try:
                    assertion_id = row["id"]
                    subject = model.normalize_subject(row["subject"])
                    predicate = model.normalize_predicate(row["predicate"])
                    value = model.normalize_value(row.get("value"))
                    text = model.normalize_text(row["text"])
                except (KeyError, TypeError, AttributeError):
                    rejected += 1
                    continue

                if compute_id(subject, predicate, value, text) != assertion_id:
                    rejected += 1
                    continue

                created_at = row.get("created_at") or now_iso()
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO assertions(id, subject, predicate, value, value_key, text, note, created_at) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        assertion_id,
                        subject,
                        predicate,
                        value,
                        model.value_key(value),
                        text,
                        row.get("note"),
                        created_at,
                    ),
                )
                if cursor.rowcount:
                    imported += 1
                else:
                    skipped += 1

                existing_events = {
                    (r["kind"], r["at"], r["actor"], r["data"])
                    for r in conn.execute(
                        "SELECT kind, at, actor, data FROM events WHERE assertion_id = ?",
                        (assertion_id,),
                    )
                }
                for event in row.get("events", []) or []:
                    try:
                        kind = event["kind"]
                        at = event["at"]
                        actor = event.get("actor") or "human:unknown"
                        data = model.canonical_json(event.get("data") or {})
                    except (KeyError, TypeError):
                        continue
                    if kind not in model.EVENT_KINDS:
                        continue
                    key = (kind, at, actor, data)
                    if key in existing_events:
                        continue
                    existing_events.add(key)
                    conn.execute(
                        "INSERT INTO events(kind, assertion_id, at, actor, data) VALUES(?, ?, ?, ?, ?)",
                        (kind, assertion_id, at, actor, data),
                    )
                    events_added += 1

                seen_digests = {
                    r["digest"]
                    for r in conn.execute(
                        "SELECT digest FROM evidence WHERE assertion_id = ?", (assertion_id,)
                    )
                }
                next_seq = int(
                    conn.execute(
                        "SELECT COALESCE(MAX(seq), 0) FROM evidence WHERE assertion_id = ?",
                        (assertion_id,),
                    ).fetchone()[0]
                )
                for item in row.get("evidence", []) or []:
                    try:
                        evidence = Evidence(
                            kind=item["kind"],
                            locator=item["locator"],
                            excerpt=item.get("excerpt"),
                            captured_at=item.get("captured_at"),
                        )
                    except (KeyError, ValueError, TypeError):
                        continue
                    if evidence.digest in seen_digests:
                        continue
                    seen_digests.add(evidence.digest)
                    next_seq += 1
                    conn.execute(
                        "INSERT INTO evidence(assertion_id, seq, kind, locator, excerpt, digest, captured_at) "
                        "VALUES(?, ?, ?, ?, ?, ?, ?)",
                        (
                            assertion_id,
                            next_seq,
                            evidence.kind,
                            evidence.locator,
                            evidence.excerpt,
                            evidence.digest,
                            evidence.captured_at or now_iso(),
                        ),
                    )

                seen_falsifiers = {
                    r["digest"]
                    for r in conn.execute(
                        "SELECT digest FROM falsifiers WHERE assertion_id = ?", (assertion_id,)
                    )
                }
                next_fseq = int(
                    conn.execute(
                        "SELECT COALESCE(MAX(seq), 0) FROM falsifiers WHERE assertion_id = ?",
                        (assertion_id,),
                    ).fetchone()[0]
                )
                for item in row.get("falsifiers", []) or []:
                    try:
                        falsifier = Falsifier(kind=item["kind"], spec=item["spec"])
                    except (KeyError, ValueError, TypeError):
                        continue
                    if falsifier.digest in seen_falsifiers:
                        continue
                    seen_falsifiers.add(falsifier.digest)
                    next_fseq += 1
                    conn.execute(
                        "INSERT INTO falsifiers(assertion_id, seq, kind, spec, digest) VALUES(?, ?, ?, ?, ?)",
                        (
                            assertion_id,
                            next_fseq,
                            falsifier.kind,
                            model.canonical_json(falsifier.spec),
                            falsifier.digest,
                        ),
                    )

                for dependency in row.get("depends_on", []) or []:
                    if isinstance(dependency, str) and self._row_exists(dependency):
                        conn.execute(
                            "INSERT OR IGNORE INTO edges(from_id, to_id) VALUES(?, ?)",
                            (assertion_id, dependency),
                        )

        return {
            "imported": imported,
            "skipped": skipped,
            "rejected": rejected,
            "events_added": events_added,
        }


def _load_json(value: Optional[str]) -> Dict[str, Any]:
    if not value:
        return {}
    import json

    try:
        loaded = json.loads(value)
    except ValueError:
        return {"_raw": value}
    return loaded if isinstance(loaded, dict) else {"_value": loaded}
