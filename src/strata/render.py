"""strata.render — human-readable output.

Two rules govern this module:

1. **Widths are computed, not guessed.** East Asian characters occupy two cells;
   ANSI codes occupy none. `ljust` gets both wrong, so everything here goes
   through :func:`dwidth`.
2. **Colour is optional and never load-bearing.** Every distinction is also
   encoded in a glyph or a word, so piping to a file or a pager loses nothing
   that matters.
"""

from __future__ import annotations

import os
import re
import unicodedata
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .model import (
    Assertion,
    Event,
    INTEGRITY_AT_RISK,
    INTEGRITY_ORPHANED,
    STANDING_ACTIVE,
    STANDING_RETRACTED,
    STANDING_SUPERSEDED,
    humanize_age,
    truncate,
)

_ANSI = re.compile(r"\x1b\[[0-9;]*m")

_RESET = "\x1b[0m"
CODES = {
    "bold": "\x1b[1m",
    "dim": "\x1b[2m",
    "italic": "\x1b[3m",
    "red": "\x1b[31m",
    "green": "\x1b[32m",
    "yellow": "\x1b[33m",
    "blue": "\x1b[34m",
    "magenta": "\x1b[35m",
    "cyan": "\x1b[36m",
    "grey": "\x1b[90m",
    "white": "\x1b[37m",
}

#: ASCII fallbacks so `--ascii` output stays legible on legacy terminals.
GLYPHS = {
    "active": ("●", "*"),
    "retracted": ("○", "o"),
    "superseded": ("⊘", "x"),
    "at-risk": ("◐", "!"),
    "orphaned": ("✕", "X"),
    "branch": ("├─", "+-"),
    "last": ("└─", "`-"),
    # Each level contributes exactly three columns, so a non-last child must pad
    # to three too — "│ " is 2 wide and silently skews everything below it.
    "pipe": ("│  ", "|  "),
    "bullet": ("▸", ">"),
    "check": ("✓", "v"),
    "cross": ("✕", "x"),
    "warn": ("!", "!"),
    "arrow": ("→", "->"),
}


class Console:
    """Colour state and layout primitives shared by every renderer."""

    def __init__(self, colour: Optional[bool] = None, ascii_only: Optional[bool] = None):
        if colour is None:
            colour = self._detect_colour()
        self.colour = colour
        self.ascii_only = bool(ascii_only) if ascii_only is not None else False

    @staticmethod
    def _detect_colour() -> bool:
        override = os.environ.get("STRATA_COLOR", "").strip().lower()
        if override in ("always", "1", "true"):
            return True
        if override in ("never", "0", "false"):
            return False
        if os.environ.get("NO_COLOR"):
            return False
        if os.environ.get("TERM") == "dumb":
            return False
        return bool(getattr(os.sys.stdout, "isatty", lambda: False)())

    def paint(self, text: str, *styles: str) -> str:
        if not self.colour or not styles:
            return text
        prefix = "".join(CODES.get(style, "") for style in styles)
        return f"{prefix}{text}{_RESET}"

    def glyph(self, name: str) -> str:
        pair = GLYPHS.get(name)
        if not pair:
            return name
        return pair[1] if self.ascii_only else pair[0]

    # -- text measurement --------------------------------------------------

    @staticmethod
    def dwidth(text: str) -> int:
        stripped = _ANSI.sub("", text)
        total = 0
        for char in stripped:
            if unicodedata.combining(char):
                continue
            total += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
        return total

    def ljust(self, text: str, width: int) -> str:
        return text + " " * max(0, width - self.dwidth(text))

    def rjust(self, text: str, width: int) -> str:
        return " " * max(0, width - self.dwidth(text)) + text

    def columns(self, rows: Sequence[Sequence[str]], gap: int = 2) -> List[str]:
        """Pad a grid of already-styled cells; ANSI codes do not affect widths."""
        if not rows:
            return []
        widths = [0] * max(len(r) for r in rows)
        for row in rows:
            for index, cell in enumerate(row):
                widths[index] = max(widths[index], self.dwidth(cell))
        lines = []
        for row in rows:
            cells = [
                self.ljust(cell, widths[index])
                for index, cell in enumerate(row[:-1])
            ]
            cells.append(row[-1] if row else "")
            lines.append((" " * gap).join(cells).rstrip())
        return lines


def status_glyph(console: Console, assertion: Assertion) -> str:
    """One glyph that says everything about an assertion's standing."""
    if assertion.standing == STANDING_RETRACTED:
        return console.paint(console.glyph("retracted"), "grey")
    if assertion.standing == STANDING_SUPERSEDED:
        return console.paint(console.glyph("superseded"), "grey")
    if assertion.integrity == INTEGRITY_ORPHANED:
        return console.paint(console.glyph("orphaned"), "red")
    if assertion.integrity == INTEGRITY_AT_RISK:
        return console.paint(console.glyph("at-risk"), "yellow")
    return console.paint(console.glyph("active"), "green")


_STATUS_STYLE = {
    "observed": "green",
    "reported": "cyan",
    "inferred": "magenta",
    "assumed": "grey",
}


def _meta_line(console: Console, assertion: Assertion) -> str:
    bits = [
        console.paint(assertion.status, _STATUS_STYLE.get(assertion.status, "dim")),
        f"{assertion.testimonials}×" if assertion.testimonials > 1 else "1×",
        humanize_age(assertion.asserted_at),
        assertion.actor,
    ]
    if assertion.valid_from or assertion.valid_to:
        span = f"{assertion.valid_from or '…'} {console.glyph('arrow')} {assertion.valid_to or '…'}"
        bits.append(console.paint(f"valid {span}", "dim"))
    if assertion.standing == STANDING_RETRACTED and assertion.retract_reason:
        bits.append(console.paint(f"retracted: {assertion.retract_reason}", "grey"))
    if assertion.standing == STANDING_SUPERSEDED and assertion.superseded_by:
        bits.append(console.paint(f"replaced by {assertion.superseded_by[:8]}", "grey"))
    return console.paint(" · ", "dim").join(bits)


def render_assertion(console: Console, assertion: Assertion, *, verbose: bool = False) -> List[str]:
    """The full card for one assertion."""
    glyph = status_glyph(console, assertion)
    head = [
        console.paint(assertion.short, "cyan", "bold"),
        glyph,
        console.paint(assertion.key, "bold"),
    ]
    if assertion.value is not None:
        head.append(console.paint(f"= {assertion.value}", "yellow"))

    lines = ["  ".join(head)]
    lines.append("    " + assertion.text)
    lines.append("    " + _meta_line(console, assertion))

    if assertion.integrity == INTEGRITY_ORPHANED:
        lines.append(
            "    " + console.paint("all premises retracted — this conclusion has no basis", "red")
        )
    elif assertion.integrity == INTEGRITY_AT_RISK:
        lines.append("    " + console.paint("some premises retracted", "yellow"))

    show_evidence = verbose or len(assertion.evidence) <= 2
    if assertion.evidence and show_evidence:
        for evidence in assertion.evidence:
            snippet = ""
            if evidence.excerpt:
                snippet = " " + console.paint(f'"{truncate(evidence.excerpt, 64)}"', "dim")
            lines.append(
                "    "
                + console.paint(console.glyph("bullet"), "dim")
                + " "
                + console.paint(evidence.kind, "blue")
                + " "
                + evidence.locator
                + snippet
            )
    elif assertion.evidence:
        lines.append(
            "    "
            + console.paint(f"{len(assertion.evidence)} evidence records (--verbose to list)", "dim")
        )

    if verbose:
        for falsifier in assertion.falsifiers:
            lines.append(
                "    " + console.paint("?", "yellow") + " " + falsifier.describe()
            )
        if assertion.depends_on:
            lines.append(
                "    "
                + console.paint("depends on", "dim")
                + " "
                + ", ".join(d[:8] for d in assertion.depends_on)
            )
        if assertion.note:
            lines.append("    " + console.paint(f"note: {assertion.note}", "dim"))

    return lines


def render_compact(console: Console, assertion: Assertion, *, show_age: bool = True) -> str:
    """One line per assertion: id, slot, value, standing."""
    parts = [
        console.paint(assertion.short, "cyan"),
        status_glyph(console, assertion),
        console.ljust(console.paint(assertion.key, "bold"), 38),
    ]
    value = assertion.value if assertion.value is not None else truncate(assertion.text, 44)
    parts.append(console.paint(truncate(value, 44), "yellow" if assertion.value else "white"))
    if show_age:
        parts.append(console.paint(humanize_age(assertion.asserted_at), "dim"))
    return "  ".join(parts)


def render_list(
    console: Console,
    assertions: Sequence[Assertion],
    *,
    empty_hint: Optional[str] = None,
    show_age: bool = True,
) -> List[str]:
    if not assertions:
        return [console.paint(empty_hint or "nothing here", "dim")]
    return [render_compact(console, a, show_age=show_age) for a in assertions]


def render_blame(
    console: Console,
    node: Any,
    prefix: str = "",
    connector: str = "",
    is_last: bool = True,
) -> List[str]:
    """ASCII tree of a blame walk.

    `prefix` is the accumulated left margin, `connector` is this node's own
    elbow. Children inherit a margin built from the parent's: a last child
    contributes blank space, any other contributes a vertical bar — the standard
    tree rule, applied recursively.
    """
    assertion = node.assertion
    glyph = status_glyph(console, assertion)
    label = (
        console.paint(assertion.short, "cyan")
        + "  "
        + glyph
        + " "
        + console.paint(f"[{assertion.status}]", _STATUS_STYLE.get(assertion.status, "dim"))
        + " "
        + truncate(assertion.text, 60)
    )
    if assertion.evidence:
        first = assertion.evidence[0]
        label += console.paint(f"  {first.kind}:{truncate(first.locator, 34)}", "dim")

    lines = [prefix + connector + label]

    if connector:
        child_prefix = prefix + ("   " if is_last else console.glyph("pipe"))
    else:
        child_prefix = ""

    for index, child in enumerate(node.children):
        last = index == len(node.children) - 1
        elbow = console.glyph("last") if last else console.glyph("branch")
        lines.extend(render_blame(console, child, child_prefix, elbow + " ", last))

    if node.cyclic:
        lines.append(
            child_prefix + console.paint("(cycle detected, truncated)", "red")
        )
    return lines


def render_events(console: Console, events: Sequence[Event]) -> List[str]:
    rows: List[List[str]] = []
    style = {
        "assert": ("green", "+"),
        "retract": ("red", "-"),
        "supersede": ("yellow", "~"),
        "annotate": ("blue", "#"),
    }
    for event in events:
        colour, sign = style.get(event.kind, ("dim", "·"))
        detail = ""
        if event.kind == "assert":
            detail = event.data.get("status", "")
            if event.data.get("note"):
                detail += f" — {event.data['note']}"
        elif event.kind == "retract":
            detail = event.data.get("reason", "")
        elif event.kind == "supersede":
            detail = f"replaced by {(event.data.get('by') or '')[:8]}"
            if event.data.get("reason"):
                detail += f" — {event.data['reason']}"
        elif event.kind == "annotate":
            detail = event.data.get("note", "")
        rows.append(
            [
                console.paint(f"{event.seq:>5}", "dim"),
                console.paint(f"{sign} {event.kind:<9}", colour),
                console.paint(event.assertion_id[:8], "cyan"),
                console.paint(event.at, "dim"),
                console.paint(truncate(event.actor, 18), "dim"),
                truncate(detail, 56),
            ]
        )
    return console.columns(rows)


def render_diff(console: Console, diff_result: Any) -> List[str]:
    lines: List[str] = []
    if diff_result.empty:
        return [console.paint("worldview unchanged", "dim")]

    for assertion in diff_result.added:
        lines.append(
            console.paint("+ ", "green")
            + console.paint(assertion.short, "cyan")
            + "  "
            + console.ljust(assertion.key, 38)
            + " "
            + (assertion.value if assertion.value is not None else truncate(assertion.text, 40))
        )
    for assertion in diff_result.removed:
        lines.append(
            console.paint("- ", "red")
            + console.paint(assertion.short, "cyan")
            + "  "
            + console.ljust(assertion.key, 38)
            + " "
            + console.paint(assertion.value or truncate(assertion.text, 40), "dim")
        )
    for old, new in diff_result.changed:
        lines.append(
            console.paint("~ ", "yellow")
            + console.paint(new.key, "bold")
            + "  "
            + console.paint(old.value or "∅", "red")
            + console.paint(" " + console.glyph("arrow") + " ", "dim")
            + console.paint(new.value or "∅", "green")
        )
    for assertion in diff_result.superseded:
        lines.append(
            console.paint("· ", "grey")
            + console.paint(assertion.short, "grey")
            + "  "
            + console.paint(f"{assertion.key} became inactive", "dim")
        )
    return lines


def render_check(console: Console, report: Any) -> List[str]:
    lines: List[str] = []
    if not report.results:
        return [console.paint("no falsifiers to evaluate", "dim")]

    for result in report.falsified:
        lines.append(
            console.paint(console.glyph("cross") + " falsified ", "red")
            + console.paint(result.assertion.short, "cyan")
            + "  "
            + truncate(result.assertion.text, 56)
        )
        for outcome in result.outcomes:
            if outcome.outcome == "falsified":
                lines.append("    " + console.paint(outcome.detail, "dim"))

    for result in report.errors:
        lines.append(
            console.paint(console.glyph("warn") + " error ", "yellow")
            + console.paint(result.assertion.short, "cyan")
            + "  "
            + truncate(result.assertion.text, 56)
        )
        for outcome in result.outcomes:
            if outcome.outcome == "error":
                lines.append("    " + console.paint(outcome.detail, "dim"))

    for result in report.skipped:
        lines.append(
            console.paint("· skipped ", "grey")
            + console.paint(result.assertion.short, "cyan")
            + "  "
            + console.paint("needs --allow-exec", "dim")
        )

    for result in report.holds:
        lines.append(
            console.paint(console.glyph("check") + " holds ", "green")
            + console.paint(result.assertion.short, "cyan")
            + "  "
            + console.paint(truncate(result.assertion.text, 56), "dim")
        )

    summary = report.to_dict()["summary"]
    lines.append("")
    lines.append(
        console.paint(
            f"{summary['total']} checked · {summary['holds']} holds · "
            f"{summary['falsified']} falsified · {summary['errors']} errors · "
            f"{summary['skipped']} skipped",
            "bold",
        )
    )
    return lines


def render_stats(console: Console, stats: Dict[str, Any]) -> List[str]:
    rows = [
        ["assertions", str(stats["assertions"])],
        ["active", str(stats["active"])],
        ["retracted", str(stats["retracted"])],
        ["superseded", str(stats["superseded"])],
        ["subjects", str(stats["subjects"])],
        ["events", str(stats["events"])],
        ["evidence", str(stats["evidence"])],
        ["falsifiers", str(stats["falsifiers"])],
        ["dependency edges", str(stats["edges"])],
    ]
    lines = [
        console.paint("ledger ", "dim") + stats["path"],
        console.paint("instance ", "dim")
        + str(stats.get("instance_id"))
        + console.paint("  schema v", "dim")
        + str(stats.get("schema_version")),
        "",
    ]
    for label, value in rows:
        lines.append("  " + console.ljust(console.paint(label, "dim"), 20) + value)
    return lines


def render_conflicts(console: Console, conflicts: Sequence[Any]) -> List[str]:
    if not conflicts:
        return [console.paint("no contradictions among active assertions", "green")]
    lines: List[str] = []
    for conflict in conflicts:
        lines.append(console.paint(f"conflict  {conflict.key}", "red", "bold"))
        for assertion in conflict.assertions:
            lines.append(
                "  "
                + console.paint(assertion.short, "cyan")
                + "  "
                + console.paint(
                    assertion.value if assertion.value is not None else "(no value)", "yellow"
                )
                + "  "
                + console.paint(
                    f"{humanize_age(assertion.asserted_at)} · {assertion.actor}", "dim"
                )
            )
            lines.append("            " + console.paint(truncate(assertion.text, 66), "dim"))
        lines.append("")
    return lines
