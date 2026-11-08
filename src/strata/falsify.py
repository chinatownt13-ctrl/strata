"""strata.falsify — checking whether a belief still holds.

A falsifier is a condition the assertion claims is true. If the condition does
not hold, the assertion is *falsified*: the world has moved on, or we were wrong
when we wrote it down. If it cannot be evaluated, it is an *error* — never
silently treated as passing.

Safety posture: `command_*` falsifiers spawn processes, so they are refused
unless the caller explicitly passes ``allow_exec=True``. A memory store that
executes payloads found in its own rows is a remote code execution primitive
with extra steps; the default must be filesystem-only.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .model import EXEC_FALSIFIERS, Assertion, Falsifier, truncate
from .store import Store

OUTCOME_HOLDS = "holds"
OUTCOME_FALSIFIED = "falsified"
OUTCOME_ERROR = "error"
OUTCOME_SKIPPED = "skipped"

MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_OUTPUT_CHARS = 64 * 1024
DEFAULT_TIMEOUT = 15.0


@dataclass
class CheckOutcome:
    falsifier: Falsifier
    outcome: str
    detail: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "condition": self.falsifier.describe(),
            "kind": self.falsifier.kind,
            "outcome": self.outcome,
            "detail": self.detail,
        }


@dataclass
class AssertionCheck:
    assertion: Assertion
    outcomes: List[CheckOutcome] = field(default_factory=list)

    @property
    def outcome(self) -> str:
        """Worst outcome wins: falsified > error > skipped > holds."""
        kinds = [o.outcome for o in self.outcomes]
        for candidate in (OUTCOME_FALSIFIED, OUTCOME_ERROR, OUTCOME_SKIPPED):
            if candidate in kinds:
                return candidate
        return OUTCOME_HOLDS

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.assertion.id,
            "short": self.assertion.short,
            "subject": self.assertion.subject,
            "predicate": self.assertion.predicate,
            "value": self.assertion.value,
            "text": self.assertion.text,
            "status": self.assertion.status,
            "standing": self.assertion.standing,
            "integrity": self.assertion.integrity,
            "outcome": self.outcome,
            "checks": [o.to_dict() for o in self.outcomes],
        }


@dataclass
class CheckReport:
    results: List[AssertionCheck] = field(default_factory=list)
    allow_exec: bool = False

    def _by(self, outcome: str) -> List[AssertionCheck]:
        return [r for r in self.results if r.outcome == outcome]

    @property
    def holds(self) -> List[AssertionCheck]:
        return self._by(OUTCOME_HOLDS)

    @property
    def falsified(self) -> List[AssertionCheck]:
        return self._by(OUTCOME_FALSIFIED)

    @property
    def errors(self) -> List[AssertionCheck]:
        return self._by(OUTCOME_ERROR)

    @property
    def skipped(self) -> List[AssertionCheck]:
        return self._by(OUTCOME_SKIPPED)

    @property
    def ok(self) -> bool:
        """Clean only if nothing was falsified and nothing failed to evaluate."""
        return not self.falsified and not self.errors

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "allow_exec": self.allow_exec,
            "summary": {
                "total": len(self.results),
                "holds": len(self.holds),
                "falsified": len(self.falsified),
                "errors": len(self.errors),
                "skipped": len(self.skipped),
            },
            "holds": [r.to_dict() for r in self.holds],
            "falsified": [r.to_dict() for r in self.falsified],
            "errors": [r.to_dict() for r in self.errors],
            "skipped": [r.to_dict() for r in self.skipped],
        }


def _resolve_path(path: str, root: str) -> str:
    expanded = os.path.expanduser(path)
    if os.path.isabs(expanded):
        return expanded
    return os.path.normpath(os.path.join(root, expanded))


def _read_text(path: str) -> str:
    size = os.path.getsize(path)
    if size > MAX_FILE_BYTES:
        raise ValueError(f"file too large to check ({size} bytes > {MAX_FILE_BYTES})")
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        return handle.read()


def _compile(pattern: str, ignore_case: bool = False) -> "re.Pattern[str]":
    flags = re.IGNORECASE if ignore_case else 0
    return re.compile(pattern, flags)


def evaluate(
    falsifier: Falsifier,
    assertion: Assertion,
    *,
    root: str,
    allow_exec: bool = False,
    timeout: float = DEFAULT_TIMEOUT,
) -> CheckOutcome:
    """Run one condition against the world. Never raises; returns an outcome."""
    kind = falsifier.kind
    spec = falsifier.spec

    try:
        if kind == "file_exists":
            target = _resolve_path(spec["path"], root)
            if os.path.exists(target):
                return CheckOutcome(falsifier, OUTCOME_HOLDS, f"exists: {target}")
            return CheckOutcome(falsifier, OUTCOME_FALSIFIED, f"missing: {target}")

        if kind == "file_absent":
            target = _resolve_path(spec["path"], root)
            if not os.path.exists(target):
                return CheckOutcome(falsifier, OUTCOME_HOLDS, f"absent: {target}")
            return CheckOutcome(
                falsifier, OUTCOME_FALSIFIED, f"expected absent but found: {target}"
            )

        if kind == "file_contains":
            target = _resolve_path(spec["path"], root)
            if not os.path.exists(target):
                return CheckOutcome(
                    falsifier, OUTCOME_FALSIFIED, f"file does not exist: {target}"
                )
            content = _read_text(target)
            pattern = _compile(spec["pattern"], bool(spec.get("ignore_case")))
            found = pattern.search(content) is not None
            if found:
                return CheckOutcome(
                    falsifier, OUTCOME_HOLDS, f"matched /{spec['pattern']}/ in {target}"
                )
            return CheckOutcome(
                falsifier,
                OUTCOME_FALSIFIED,
                f"no match for /{spec['pattern']}/ in {target}",
            )

        if kind == "regex":
            field_name = spec.get("target", "text")
            haystack = {
                "text": assertion.text,
                "value": assertion.value or "",
                "subject": assertion.subject,
                "predicate": assertion.predicate,
            }[field_name]
            pattern = _compile(spec["pattern"], bool(spec.get("ignore_case")))
            if pattern.search(haystack):
                return CheckOutcome(
                    falsifier, OUTCOME_HOLDS, f"{field_name} matched /{spec['pattern']}/"
                )
            return CheckOutcome(
                falsifier,
                OUTCOME_FALSIFIED,
                f"{field_name} did not match /{spec['pattern']}/",
            )

        if kind in EXEC_FALSIFIERS:
            if not allow_exec:
                return CheckOutcome(
                    falsifier,
                    OUTCOME_SKIPPED,
                    "command falsifiers are disabled; re-run with --allow-exec",
                )
            argv = spec["argv"]
            cwd = _resolve_path(spec["cwd"], root) if spec.get("cwd") else root
            if not os.path.isdir(cwd):
                return CheckOutcome(falsifier, OUTCOME_ERROR, f"cwd does not exist: {cwd}")
            completed = subprocess.run(
                argv,
                cwd=cwd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
            if kind == "command_success":
                if completed.returncode == 0:
                    return CheckOutcome(
                        falsifier, OUTCOME_HOLDS, f"exit 0: {' '.join(argv)}"
                    )
                tail = truncate((completed.stderr or completed.stdout or "").strip(), 160)
                return CheckOutcome(
                    falsifier,
                    OUTCOME_FALSIFIED,
                    f"exit {completed.returncode}: {' '.join(argv)}"
                    + (f" — {tail}" if tail else ""),
                )
            # command_output
            combined = (completed.stdout or "") + (completed.stderr or "")
            combined = combined[:MAX_OUTPUT_CHARS]
            pattern = _compile(spec["pattern"], bool(spec.get("ignore_case")))
            if pattern.search(combined):
                return CheckOutcome(
                    falsifier, OUTCOME_HOLDS, f"output matched /{spec['pattern']}/"
                )
            return CheckOutcome(
                falsifier,
                OUTCOME_FALSIFIED,
                f"output did not match /{spec['pattern']}/",
            )

        return CheckOutcome(falsifier, OUTCOME_ERROR, f"unhandled falsifier kind {kind!r}")

    except subprocess.TimeoutExpired:
        return CheckOutcome(falsifier, OUTCOME_ERROR, f"timed out after {timeout}s")
    except (OSError, ValueError, re.error, KeyError) as exc:
        return CheckOutcome(falsifier, OUTCOME_ERROR, f"{type(exc).__name__}: {exc}")


def check(
    store: Store,
    *,
    assertion_ids: Optional[Sequence[str]] = None,
    allow_exec: bool = False,
    root: Optional[str] = None,
    timeout: float = DEFAULT_TIMEOUT,
    record: bool = False,
) -> CheckReport:
    """Evaluate every falsifier in scope and sort the results.

    `record=True` writes `last_checked` back to the store — useful for cron, and
    deliberately off by default so that a read-only audit stays read-only.
    """
    root = root or os.path.dirname(os.path.abspath(store.path)) or os.getcwd()
    report = CheckReport(allow_exec=allow_exec)

    if assertion_ids:
        assertions = store.hydrate([store._resolve_id(i) for i in assertion_ids])
    else:
        assertions = store.query(standing="active", limit=100000)

    for assertion in assertions:
        if not assertion.falsifiers:
            continue
        result = AssertionCheck(assertion=assertion)
        for falsifier in assertion.falsifiers:
            outcome = evaluate(
                falsifier,
                assertion,
                root=root,
                allow_exec=allow_exec,
                timeout=timeout,
            )
            result.outcomes.append(outcome)
        report.results.append(result)
        if record:
            store.record_check(assertion.id, result.outcome)

    return report
