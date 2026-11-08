"""strata.model — immutable value objects and the identity rules.

Nothing in this module touches disk. Every rule about what an assertion *is* and
how its identity is derived lives here, so it can be reasoned about and tested
without a database.

The one idea worth internalising before reading the code: an assertion's id is
derived from its *proposition* only (subject, predicate, value, text) — never
from who said it, when, or how strongly. Identity is about the claim; everything
else is metadata that accretes around it.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# --------------------------------------------------------------------------
# constants
# --------------------------------------------------------------------------

#: Truncated hex width of an assertion id. 16 hex chars = 64 bits. Collisions are
#: not a practical concern below ~10^9 assertions, and the short form is far
#: easier to type, which matters for a tool driven by hand.
ID_LEN = 16

#: Bumped when the id-derivation algorithm changes, so old and new ids never mix.
ID_VERSION = 1

STATUS_OBSERVED = "observed"
STATUS_REPORTED = "reported"
STATUS_INFERRED = "inferred"
STATUS_ASSUMED = "assumed"

#: Ordered weakest → strongest. Used when an assertion has been re-asserted under
#: different statuses: the strongest witnessed status becomes the headline.
STATUS_RANK = {
    STATUS_ASSUMED: 0,
    STATUS_INFERRED: 1,
    STATUS_REPORTED: 2,
    STATUS_OBSERVED: 3,
}
STATUSES: Tuple[str, ...] = tuple(STATUS_RANK)

EVIDENCE_FILE = "file"
EVIDENCE_URL = "url"
EVIDENCE_CONVERSATION = "conversation"
EVIDENCE_COMMAND = "command"
EVIDENCE_OBSERVATION = "observation"
EVIDENCE_OTHER = "other"

EVIDENCE_KINDS: Tuple[str, ...] = (
    EVIDENCE_FILE,
    EVIDENCE_URL,
    EVIDENCE_CONVERSATION,
    EVIDENCE_COMMAND,
    EVIDENCE_OBSERVATION,
    EVIDENCE_OTHER,
)

EVENT_ASSERT = "assert"
EVENT_RETRACT = "retract"
EVENT_SUPERSEDE = "supersede"
EVENT_ANNOTATE = "annotate"

EVENT_KINDS: Tuple[str, ...] = (
    EVENT_ASSERT,
    EVENT_RETRACT,
    EVENT_SUPERSEDE,
    EVENT_ANNOTATE,
)

#: Derived standing of an assertion. Not stored — recomputed from the event log.
STANDING_ACTIVE = "active"
STANDING_RETRACTED = "retracted"
STANDING_SUPERSEDED = "superseded"

#: Derived integrity of an `inferred` assertion, relative to its upstream graph.
INTEGRITY_INTACT = "intact"
INTEGRITY_AT_RISK = "at-risk"
INTEGRITY_ORPHANED = "orphaned"

#: Falsifier kinds that shell out. Refused unless the caller opts in.
EXEC_FALSIFIERS: Tuple[str, ...] = ("command_success", "command_output")
FALSIFIER_KINDS: Tuple[str, ...] = (
    "file_exists",
    "file_absent",
    "file_contains",
    "regex",
    "command_success",
    "command_output",
)


# --------------------------------------------------------------------------
# canonicalisation
# --------------------------------------------------------------------------


def canonical_json(obj: Any) -> str:
    """Stable JSON serialisation: sorted keys, no insignificant whitespace.

    Non-ASCII is kept literal so the digest of a Chinese assertion is readable
    in a hex dump; determinism is preserved either way.
    """
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


_WS = re.compile(r"\s+")


def _collapse(text: str) -> str:
    return _WS.sub(" ", text).strip()


def normalize_subject(value: str) -> str:
    """Subjects are namespaced identifiers and are compared case-insensitively.

    `Project:Acme/DB` and `project:acme/db` must be the same subject, otherwise
    contradiction detection silently fails.
    """
    return _collapse(value).casefold()


def normalize_predicate(value: str) -> str:
    return _collapse(value).casefold()


def normalize_value(value: Optional[str]) -> Optional[str]:
    """Values keep their original casing — `Postgres` is the name of a thing.

    Comparison uses :func:`value_key`, which is case-insensitive. This is a
    deliberate compromise: storage fidelity over merge aggressiveness.
    """
    if value is None:
        return None
    cleaned = _collapse(value)
    return cleaned or None


def value_key(value: Optional[str]) -> Optional[str]:
    return None if value is None else _collapse(value).casefold()


def normalize_text(value: str) -> str:
    """Collapse runs of whitespace but keep casing and punctuation.

    `"Uses Postgres."` and `"Uses Postgres"` are different propositions as far
    as the ledger is concerned. Precision beats deduplication here.
    """
    return _collapse(value)


def compute_id(
    subject: str,
    predicate: str,
    value: Optional[str],
    text: str,
) -> str:
    """Content address of a proposition.

    Deliberately excludes actor, timestamp, status, evidence and falsifiers:
    those describe the act of asserting, not the claim. Excluding them is what
    makes re-assertion idempotent and lets two independent observers land on the
    same id.
    """
    payload = {
        "v": ID_VERSION,
        "s": normalize_subject(subject),
        "p": normalize_predicate(predicate),
        "val": normalize_value(value),
        "t": normalize_text(text),
    }
    return sha256_hex(canonical_json(payload))[:ID_LEN]


def claim_key(subject: str, predicate: str) -> str:
    """The comparable slot an assertion occupies: `subject.predicate`.

    Two active assertions sharing a claim_key but disagreeing on value are a
    *contradiction*, and strata will say so rather than ranking them.
    """
    return f"{normalize_subject(subject)}.{normalize_predicate(predicate)}"


# --------------------------------------------------------------------------
# time
# --------------------------------------------------------------------------

_ISO_BASIC = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_RELATIVE = re.compile(r"^([+-])(\d+)([smhdwMy])$")
_UNIT_SECONDS = {
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
    "w": 604800,
    "M": 2592000,  # 30 days, deliberately approximate
    "y": 31536000,
}


def now() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_iso() -> str:
    return to_iso(now())


def parse_time(text: str, *, base: Optional[datetime] = None) -> datetime:
    """Parse the time formats strata accepts on the command line.

    Accepted: `now`, `-7d`, `+2h`, `2026-01-01`, `2026-01-01T10:00:00Z`,
    `2026-01-01 10:00`, and a bare `YYYY-MM` (treated as the first of the month).
    Relative forms are resolved against ``base`` (default: now).
    """
    raw = text.strip()
    reference = base or now()

    if raw.lower() in ("now", "today"):
        return reference if raw.lower() == "now" else reference.replace(
            hour=0, minute=0, second=0, microsecond=0
        )

    match = _RELATIVE.match(raw)
    if match:
        sign, amount, unit = match.groups()
        delta = timedelta(seconds=int(amount) * _UNIT_SECONDS[unit])
        return reference + delta if sign == "+" else reference - delta

    if _ISO_BASIC.match(raw):
        return datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    if re.match(r"^\d{4}-\d{2}$", raw):
        return datetime.strptime(raw, "%Y-%m").replace(tzinfo=timezone.utc)

    normalised = raw.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalised)
    except ValueError as exc:  # pragma: no cover - message path
        raise ValueError(f"unrecognised time format: {text!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def humanize_age(iso_timestamp: str, *, reference: Optional[datetime] = None) -> str:
    """`3d ago`, `in 2h`, `just now`. Best-effort, never raises."""
    try:
        then = parse_time(iso_timestamp)
    except ValueError:
        return iso_timestamp
    delta = (reference or now()) - then
    seconds = delta.total_seconds()
    future = seconds < 0
    seconds = abs(seconds)

    if seconds < 45:
        return "in a moment" if future else "just now"
    for unit, size in (("y", 31536000), ("mo", 2592000), ("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            count = int(seconds // size)
            label = f"{count}{unit}"
            return f"in {label}" if future else f"{label} ago"
    return "in a moment" if future else "just now"


# --------------------------------------------------------------------------
# evidence
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Evidence:
    """A pointer to something in the world that supports an assertion.

    `excerpt` is the load-bearing field: it is the bytes we actually saw, and
    its hash is what `verify` checks. A locator alone ("I read it somewhere on
    that URL") is not evidence, because the world changes underneath locators.
    """

    kind: str
    locator: str
    excerpt: Optional[str] = None
    captured_at: Optional[str] = None

    def __post_init__(self) -> None:
        if self.kind not in EVIDENCE_KINDS:
            raise ValueError(
                f"unknown evidence kind {self.kind!r}; expected one of {', '.join(EVIDENCE_KINDS)}"
            )
        if not self.locator or not self.locator.strip():
            raise ValueError("evidence locator must not be empty")

    @property
    def digest(self) -> str:
        """Hash over the content we saw. Tamper with the excerpt, lose the hash."""
        return sha256_hex(
            canonical_json(
                {
                    "kind": self.kind,
                    "locator": self.locator.strip(),
                    "excerpt": self.excerpt,
                }
            )
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "locator": self.locator,
            "excerpt": self.excerpt,
            "captured_at": self.captured_at,
            "digest": self.digest,
        }

    @classmethod
    def parse(cls, spec: str, *, captured_at: Optional[str] = None) -> "Evidence":
        """Parse `kind:locator` or `kind:locator#excerpt`.

        The `#` separator is used because locators commonly contain `:` (URLs,
        Windows drive letters) but rarely `#` before a fragment. A literal `#` in
        an excerpt stays literal; only the first one splits.
        """
        if ":" not in spec:
            raise ValueError(
                f"evidence {spec!r} must look like kind:locator (e.g. file:src/app.py)"
            )
        kind, rest = spec.split(":", 1)
        kind = kind.strip().casefold()
        excerpt = None
        if "#" in rest:
            rest, excerpt = rest.split("#", 1)
        if not rest.strip():
            raise ValueError(f"evidence {spec!r} has an empty locator")
        return cls(
            kind=kind,
            locator=rest.strip(),
            excerpt=excerpt,
            captured_at=captured_at,
        )


# --------------------------------------------------------------------------
# falsifiers
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Falsifier:
    """A machine-checkable condition that would show this assertion is wrong.

    Stored separately from the assertion id on purpose: attaching a test to a
    belief must not create a new belief. Integrity is protected by `digest`,
    which `verify` recomputes.
    """

    kind: str
    spec: Dict[str, Any]

    def __post_init__(self) -> None:
        if self.kind not in FALSIFIER_KINDS:
            raise ValueError(
                f"unknown falsifier kind {self.kind!r}; expected one of {', '.join(FALSIFIER_KINDS)}"
            )
        validate_falsifier_spec(self.kind, self.spec)

    @property
    def digest(self) -> str:
        return sha256_hex(canonical_json({"kind": self.kind, "spec": self.spec}))

    def describe(self) -> str:
        k, s = self.kind, self.spec
        if k == "file_exists":
            return f"file exists: {s['path']}"
        if k == "file_absent":
            return f"file absent: {s['path']}"
        if k == "file_contains":
            return f"{s['path']} contains /{s['pattern']}/"
        if k == "regex":
            return f"{s.get('target', 'text')} matches /{s['pattern']}/"
        if k == "command_success":
            return f"command succeeds: {' '.join(s['argv'])}"
        if k == "command_output":
            return f"command output matches /{s['pattern']}/: {' '.join(s['argv'])}"
        return f"{k} {s}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "spec": self.spec,
            "digest": self.digest,
            "description": self.describe(),
        }

    @classmethod
    def parse(cls, raw: str) -> "Falsifier":
        """Parse `kind:{json}` or a small set of friendly shorthands.

        Shorthands exist because writing JSON on a command line is miserable:
            `file:docker-compose.yml~postgres`  → file_contains
            `exists:docker-compose.yml`         → file_exists
            `absent:Gemfile`                    → file_absent
        """
        if "{" in raw:
            kind, payload = raw.split(":", 1)
            kind = kind.strip()
            # Accept both `kind:{json}` and `kind: {json}`.
            return cls(kind=kind, spec=json.loads(payload))

        if ":" in raw:
            prefix, rest = raw.split(":", 1)
            prefix = prefix.strip().casefold()
            if prefix == "exists":
                return cls(kind="file_exists", spec={"path": rest})
            if prefix == "absent":
                return cls(kind="file_absent", spec={"path": rest})
            if prefix == "file":
                if "~" not in rest:
                    return cls(kind="file_exists", spec={"path": rest})
                path, pattern = rest.split("~", 1)
                return cls(
                    kind="file_contains",
                    spec={"path": path, "pattern": pattern},
                )
        raise ValueError(
            "cannot parse falsifier; use kind:{json} or a shorthand "
            "(exists:path, absent:path, file:path~pattern)"
        )


def validate_falsifier_spec(kind: str, spec: Dict[str, Any]) -> None:
    """Reject malformed specs at the boundary, not at check time."""
    if not isinstance(spec, dict):
        raise ValueError("falsifier spec must be a JSON object")

    required = {
        "file_exists": ("path",),
        "file_absent": ("path",),
        "file_contains": ("path", "pattern"),
        "regex": ("pattern",),
        "command_success": ("argv",),
        "command_output": ("argv", "pattern"),
    }[kind]

    missing = [key for key in required if key not in spec]
    if missing:
        raise ValueError(f"falsifier {kind!r} requires {', '.join(missing)}")

    if kind in ("command_success", "command_output"):
        argv = spec["argv"]
        if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv) or not argv:
            raise ValueError("falsifier argv must be a non-empty list of strings")

    if kind == "regex":
        target = spec.get("target", "text")
        if target not in ("text", "value", "subject", "predicate"):
            raise ValueError("regex target must be one of text, value, subject, predicate")

    for key in ("pattern",):
        if key in spec and not isinstance(spec[key], str):
            raise ValueError(f"falsifier {key} must be a string")
    for key in ("path",):
        if key in spec and (not isinstance(spec[key], str) or not spec[key].strip()):
            raise ValueError(f"falsifier {key} must be a non-empty string")


# --------------------------------------------------------------------------
# the assertion
# --------------------------------------------------------------------------


@dataclass
class Assertion:
    """A proposition plus the metadata of the act that put it forward.

    Mutable on the metadata side (status can strengthen as witnesses accumulate,
    evidence can be appended) but the *proposition* fields are frozen by
    convention: changing them means a different id, which means a different
    assertion. The store enforces this.
    """

    id: str
    subject: str
    predicate: str
    text: str
    status: str
    asserted_at: str
    actor: str
    value: Optional[str] = None
    valid_from: Optional[str] = None
    valid_to: Optional[str] = None
    note: Optional[str] = None
    evidence: List[Evidence] = field(default_factory=list)
    falsifiers: List[Falsifier] = field(default_factory=list)
    depends_on: List[str] = field(default_factory=list)

    # derived at read time, never stored
    standing: str = STANDING_ACTIVE
    integrity: str = INTEGRITY_INTACT
    testimonials: int = 1
    superseded_by: Optional[str] = None
    retracted_at: Optional[str] = None
    retract_reason: Optional[str] = None

    @property
    def key(self) -> str:
        return claim_key(self.subject, self.predicate)

    @property
    def short(self) -> str:
        return self.id[:8]

    @property
    def is_active(self) -> bool:
        return self.standing == STANDING_ACTIVE

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "short": self.short,
            "subject": self.subject,
            "predicate": self.predicate,
            "key": self.key,
            "value": self.value,
            "text": self.text,
            "status": self.status,
            "actor": self.actor,
            "asserted_at": self.asserted_at,
            "asserted_age": humanize_age(self.asserted_at),
            "valid_from": self.valid_from,
            "valid_to": self.valid_to,
            "note": self.note,
            "evidence": [e.to_dict() for e in self.evidence],
            "falsifiers": [f.to_dict() for f in self.falsifiers],
            "depends_on": list(self.depends_on),
            "standing": self.standing,
            "integrity": self.integrity,
            "testimonials": self.testimonials,
            "superseded_by": self.superseded_by,
            "retracted_at": self.retracted_at,
            "retract_reason": self.retract_reason,
        }


@dataclass
class Event:
    """One append-only fact about the ledger itself."""

    seq: int
    kind: str
    assertion_id: str
    at: str
    actor: str
    data: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "seq": self.seq,
            "kind": self.kind,
            "assertion_id": self.assertion_id,
            "at": self.at,
            "actor": self.actor,
            "data": self.data,
        }


def truncate(text: str, width: int = 60) -> str:
    """Single-line, width-bounded rendering helper used across the CLI."""
    flat = _collapse(text)
    if len(flat) <= width:
        return flat
    return flat[: max(0, width - 1)] + "…"
