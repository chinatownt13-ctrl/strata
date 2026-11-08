"""strata.cli — the command line surface.

The JSON contract is deliberate and stable: with ``--json``, stdout carries
exactly one JSON object, diagnostics go to stderr, and exit codes follow the
table in the README. Agents are first-class consumers, so this surface is
treated as an API rather than as a convenience.

Exit codes:
    0  success
    1  nothing found / operation not applicable
    2  usage error
    3  integrity failure (verify found drift, or check found a falsified belief)
    4  conflict (ambiguous id, dependency cycle, invalid state transition)
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from typing import Any, Callable, Dict, List, Optional, Sequence

from . import __version__, falsify, graph, model, render
from .model import Evidence, Falsifier
from .store import (
    AmbiguousId,
    Conflict,
    CycleDetected,
    NotFound,
    Store,
    StrataError,
    UsageError,
    find_db_path,
)

EXIT_OK = 0
EXIT_EMPTY = 1
EXIT_USAGE = 2
EXIT_INTEGRITY = 3
EXIT_CONFLICT = 4


# --------------------------------------------------------------------------
# output plumbing
# --------------------------------------------------------------------------


class Output:
    """Single funnel for everything the user sees, so stdout stays clean."""

    def __init__(self, as_json: bool, console: render.Console):
        self.as_json = as_json
        self.console = console

    def json(self, command: str, data: Any, *, ok: bool = True, meta: Optional[Dict[str, Any]] = None) -> None:
        """Emit the machine contract — only when ``--json`` was requested."""
        if not self.as_json:
            return
        payload: Dict[str, Any] = {"ok": ok, "command": command, "data": data}
        if meta:
            payload["meta"] = meta
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")

    def lines(self, lines: Sequence[str]) -> None:
        if self.as_json:
            return
        for line in lines:
            sys.stdout.write(line + "\n")

    def note(self, message: str) -> None:
        """Progress chatter. Always stderr, so `export -o file` leaves stdout clean."""
        sys.stderr.write(message + "\n")

    def error(self, message: str) -> None:
        sys.stderr.write(message + "\n")


def default_actor() -> str:
    try:
        user = getpass.getuser()
    except Exception:  # pragma: no cover - exotic environments
        user = "local"
    return f"human:{user}"


# --------------------------------------------------------------------------
# argument parsing
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="strata",
        description="A ledger of assertions: version control for beliefs.",
        epilog="Run `strata <command> --help` for details. Exit codes: "
        "0 ok, 1 empty, 2 usage, 3 integrity failure, 4 conflict.",
    )
    parser.add_argument("--version", action="version", version=f"strata {__version__}")

    # Global flags are accepted both before and after the subcommand. The
    # subcommand copies use SUPPRESS so that omitting them there does not
    # clobber a value already set at the top level (`strata --db x query`).
    parser.add_argument("--db", metavar="PATH", help="path to the .strata file")
    parser.add_argument("--json", dest="as_json", action="store_true", help="emit one JSON object on stdout")
    parser.add_argument("--no-color", action="store_true", help="disable ANSI colour")
    parser.add_argument("--ascii", action="store_true", help="use ASCII glyphs only")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", metavar="PATH", default=argparse.SUPPRESS, help="path to the .strata file")
    common.add_argument("--json", dest="as_json", action="store_true", default=argparse.SUPPRESS, help="emit one JSON object on stdout")
    common.add_argument("--no-color", action="store_true", default=argparse.SUPPRESS, help="disable ANSI colour")
    common.add_argument("--ascii", action="store_true", default=argparse.SUPPRESS, help="use ASCII glyphs only")

    subs = parser.add_subparsers(dest="command", metavar="<command>")

    def add(name: str, help_text: str, *, aliases: Sequence[str] = ()) -> argparse.ArgumentParser:
        return subs.add_parser(
            name, parents=[common], help=help_text, aliases=list(aliases)
        )

    # init ---------------------------------------------------------------
    p_init = add("init", "create a new ledger")
    p_init.add_argument("path", nargs="?", help="where to create it (default: ./.strata)")
    p_init.add_argument("--force", action="store_true", help="overwrite an existing ledger")

    # assert -------------------------------------------------------------
    p_assert = add("assert", "record a belief", aliases=["add"])
    p_assert.add_argument("-s", "--subject", required=True, help="e.g. project:acme/db")
    p_assert.add_argument("-p", "--predicate", required=True, help="e.g. engine")
    p_assert.add_argument("-v", "--value", help="the comparable value, e.g. postgres")
    p_assert.add_argument("-t", "--text", help="the full human-readable claim ('-' reads stdin)")
    p_assert.add_argument(
        "--status",
        default=model.STATUS_OBSERVED,
        choices=list(model.STATUSES),
        help="epistemic status (default: observed)",
    )
    p_assert.add_argument("--actor", default=None, help="who is asserting (default: human:<you>)")
    p_assert.add_argument("-e", "--evidence", action="append", default=[], metavar="KIND:LOCATOR[#EXCERPT]")
    p_assert.add_argument("-f", "--falsifier", action="append", default=[], metavar="SPEC")
    p_assert.add_argument("-d", "--depends-on", action="append", default=[], metavar="ID", help="required for --status inferred")
    p_assert.add_argument("--valid-from", help="when this became true in the world")
    p_assert.add_argument("--valid-to", help="when it stopped being true in the world")
    p_assert.add_argument("--note", help="free-form note stored with the assertion")
    p_assert.add_argument("--supersedes", metavar="ID", help="retire an older assertion in the same step")

    # get ----------------------------------------------------------------
    p_get = add("get", "show one assertion in full", aliases=["show"])
    p_get.add_argument("id")
    p_get.add_argument("--verbose", action="store_true", help="include falsifiers, deps and notes")

    # query --------------------------------------------------------------
    p_query = add("query", "list assertions", aliases=["ls"])
    p_query.add_argument("-s", "--subject")
    p_query.add_argument("-p", "--predicate")
    p_query.add_argument("-k", "--key", help="subject.predicate")
    p_query.add_argument("--status", choices=list(model.STATUSES))
    p_query.add_argument(
        "--standing",
        default="active",
        choices=["active", "retracted", "superseded", "all"],
        help="which lifecycle state to include (default: active)",
    )
    p_query.add_argument("--text", help="substring match on the claim text")
    p_query.add_argument("--as-of", help="what was believed at this moment")
    p_query.add_argument("--valid-at", help="what was true in the world at this moment")
    p_query.add_argument("--actor")
    p_query.add_argument("-n", "--limit", type=int, default=50)
    p_query.add_argument("--full", action="store_true", help="print full cards instead of one line each")

    # log / history ------------------------------------------------------
    p_log = add("log", "recent events across the ledger")
    p_log.add_argument("-n", "--limit", type=int, default=30)
    p_log.add_argument("--since", help="e.g. -7d, 2026-01-01")

    p_history = add("history", "the full event stream for one assertion")
    p_history.add_argument("id")

    # mutations ----------------------------------------------------------
    p_retract = add("retract", "withdraw a belief (nothing is deleted)")
    p_retract.add_argument("id")
    p_retract.add_argument("-r", "--reason", required=True)

    p_super = add("supersede", "declare that one belief replaces another")
    p_super.add_argument("old_id")
    p_super.add_argument("new_id")
    p_super.add_argument("-r", "--reason")

    p_annot = add("annotate", "attach a note without changing standing")
    p_annot.add_argument("id")
    p_annot.add_argument("-n", "--note", required=True)

    p_ev = add("evidence", "append evidence to an existing assertion")
    p_ev.add_argument("id")
    p_ev.add_argument("-e", "--evidence", action="append", required=True, metavar="KIND:LOCATOR[#EXCERPT]")

    p_fal = add("falsifier", "attach a machine-checkable condition")
    p_fal.add_argument("id")
    p_fal.add_argument("-f", "--falsifier", action="append", required=True, metavar="SPEC")

    # analysis -----------------------------------------------------------
    p_check = add("check", "evaluate falsifiers against the world")
    p_check.add_argument("-i", "--id", action="append", dest="ids", default=[])
    p_check.add_argument("--allow-exec", action="store_true", help="permit command_* falsifiers")
    p_check.add_argument("--root", help="base directory for relative paths (default: next to the ledger)")
    p_check.add_argument("--timeout", type=float, default=falsify.DEFAULT_TIMEOUT)
    p_check.add_argument("--record", action="store_true", help="write last_checked back to the ledger")

    p_verify = add("verify", "recompute every hash and digest")

    p_blame = add("blame", "walk a conclusion back to its premises")
    p_blame.add_argument("id")
    p_blame.add_argument("--depth", type=int, default=6)

    p_impact = add("impact", "what breaks if this belief is wrong")
    p_impact.add_argument("id")

    add("orphans", "conclusions that lost every premise")
    add("at-risk", "conclusions standing on fewer legs")
    add("conflicts", "active assertions competing on the same slot")

    p_diff = add("diff", "compare two moments as worldviews")
    p_diff.add_argument("before")
    p_diff.add_argument("after", nargs="?", default="now")

    add("stats", "ledger summary")

    # io -----------------------------------------------------------------
    p_export = add("export", "write the ledger as mergeable JSONL")
    p_export.add_argument("-o", "--output", help="file to write (default: stdout)")
    p_export.add_argument("--format", choices=["jsonl", "json"], default="jsonl")

    p_import = add("import", "merge a JSONL export into this ledger")
    p_import.add_argument("path")

    return parser


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def open_store(args: argparse.Namespace) -> Store:
    path = find_db_path(getattr(args, "db", None))
    return Store.open(path)


def parse_evidence(specs: Sequence[str]) -> List[Evidence]:
    parsed = []
    for spec in specs:
        try:
            parsed.append(Evidence.parse(spec))
        except ValueError as exc:
            raise UsageError(str(exc)) from exc
    return parsed


def parse_falsifiers(specs: Sequence[str]) -> List[Falsifier]:
    parsed = []
    for spec in specs:
        try:
            parsed.append(Falsifier.parse(spec))
        except (ValueError, json.JSONDecodeError) as exc:
            raise UsageError(f"invalid falsifier {spec!r}: {exc}") from exc
    return parsed


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------


def cmd_init(args: argparse.Namespace, out: Output) -> int:
    # Positional path wins, then --db, then the usual discovery rules. Without
    # the --db fallback, `strata init --db somewhere.strata` silently created the
    # file in the cwd instead.
    path = find_db_path(args.path or getattr(args, "db", None))
    if os.path.exists(path) and not args.force:
        raise Conflict(f"{path} already exists (use --force to overwrite)")
    if os.path.exists(path) and args.force:
        for suffix in ("", "-wal", "-shm"):
            candidate = path + suffix
            if os.path.exists(candidate):
                os.remove(candidate)

    store = Store.create(path)
    stats = store.stats()
    store.close()

    out.json("init", stats)
    out.lines(
        [
            out.console.paint("initialised ", "green") + path,
            out.console.paint(f"  instance {stats['instance_id']} · schema v{stats['schema_version']}", "dim"),
        ]
    )
    return EXIT_OK


def cmd_assert(args: argparse.Namespace, out: Output) -> int:
    store = open_store(args)

    text = args.text
    if text == "-":
        text = sys.stdin.read().strip()
    if not text:
        raise UsageError(
            "an assertion needs text: pass -t \"the full claim\" (or -t - to read stdin)"
        )

    assertion, was_new = store.assert_claim(
        subject=args.subject,
        predicate=args.predicate,
        value=args.value,
        text=text,
        status=args.status,
        actor=args.actor or default_actor(),
        evidence=parse_evidence(args.evidence),
        falsifiers=parse_falsifiers(args.falsifier),
        depends_on=args.depends_on,
        valid_from=args.valid_from,
        valid_to=args.valid_to,
        note=args.note,
        supersedes=args.supersedes,
    )
    store.close()

    out.json("assert", assertion.to_dict(), meta={"created": was_new})
    action = "asserted" if was_new else "re-asserted"
    out.lines(
        [out.console.paint(f"{action} ", "green") + assertion.short]
        + render.render_assertion(out.console, assertion, verbose=bool(args.evidence))
    )
    return EXIT_OK


def cmd_get(args: argparse.Namespace, out: Output) -> int:
    store = open_store(args)
    assertion = store.get(args.id)
    graph.annotate_integrity(store, [assertion])
    events = store.events_for(assertion.id)
    store.close()

    out.json("get", {**assertion.to_dict(), "events": [e.to_dict() for e in events]})
    out.lines(render.render_assertion(out.console, assertion, verbose=True))
    if events:
        out.lines([""] + [out.console.paint("event stream", "dim")])
        out.lines(render.render_events(out.console, events))
    return EXIT_OK


def cmd_query(args: argparse.Namespace, out: Output) -> int:
    store = open_store(args)
    assertions = store.query(
        subject=args.subject,
        predicate=args.predicate,
        key=args.key,
        status=args.status,
        standing=args.standing,
        text=args.text,
        actor=args.actor,
        as_of=args.as_of,
        valid_at=args.valid_at,
        limit=args.limit,
    )
    graph.annotate_integrity(store, assertions)
    store.close()

    out.json(
        "query",
        [a.to_dict() for a in assertions],
        ok=True,
        meta={"count": len(assertions), "standing": args.standing},
    )
    if args.full:
        lines: List[str] = []
        for assertion in assertions:
            lines.extend(render.render_assertion(out.console, assertion))
            lines.append("")
        out.lines(lines)
    else:
        out.lines(
            render.render_list(
                out.console,
                assertions,
                empty_hint="no assertions matched",
            )
        )
    return EXIT_OK if assertions else EXIT_EMPTY


def cmd_log(args: argparse.Namespace, out: Output) -> int:
    store = open_store(args)
    events = store.recent_events(limit=args.limit, since=args.since)
    store.close()
    out.json("log", [e.to_dict() for e in events], meta={"count": len(events)})
    out.lines(render.render_events(out.console, events))
    return EXIT_OK if events else EXIT_EMPTY


def cmd_history(args: argparse.Namespace, out: Output) -> int:
    store = open_store(args)
    assertion = store.get(args.id)
    events = store.events_for(assertion.id)
    store.close()
    out.json("history", {"assertion": assertion.to_dict(), "events": [e.to_dict() for e in events]})
    out.lines([out.console.paint(f"history of {assertion.short}", "bold"), assertion.text, ""])
    out.lines(render.render_events(out.console, events))
    return EXIT_OK


def cmd_retract(args: argparse.Namespace, out: Output) -> int:
    store = open_store(args)
    assertion = store.get(args.id)
    downstream = graph.downstream_closure(graph.load_edges(store), assertion.id)
    updated = store.retract(assertion.id, reason=args.reason, actor=default_actor())
    graph.annotate_integrity(store, [updated])
    store.close()

    out.json(
        "retract",
        updated.to_dict(),
        meta={"downstream_affected": len(downstream)},
    )
    out.lines(render.render_assertion(out.console, updated))
    if downstream:
        out.lines(
            [
                "",
                out.console.paint(
                    f"{len(downstream)} downstream assertion(s) now depend on this being reconsidered",
                    "yellow",
                ),
                out.console.paint("run `strata orphans` to see which conclusions lost all support", "dim"),
            ]
        )
    return EXIT_OK


def cmd_supersede(args: argparse.Namespace, out: Output) -> int:
    store = open_store(args)
    old, new = store.supersede(
        args.old_id, args.new_id, actor=default_actor(), reason=args.reason
    )
    store.close()
    out.json("supersede", {"old": old.to_dict(), "new": new.to_dict()})
    out.lines(
        [
            out.console.paint("superseded ", "yellow")
            + old.short
            + out.console.paint(" → ", "dim")
            + new.short,
            out.console.paint(f"  {old.key}: {old.value or '∅'} → {new.value or '∅'}", "dim"),
        ]
    )
    return EXIT_OK


def cmd_annotate(args: argparse.Namespace, out: Output) -> int:
    store = open_store(args)
    event = store.annotate(args.id, args.note, actor=default_actor())
    store.close()
    out.json("annotate", event.to_dict())
    out.lines([out.console.paint(f"annotated {event.assertion_id[:8]} at seq {event.seq}", "green")])
    return EXIT_OK


def cmd_evidence(args: argparse.Namespace, out: Output) -> int:
    store = open_store(args)
    assertion = store.add_evidence(args.id, parse_evidence(args.evidence))
    store.close()
    out.json("evidence", assertion.to_dict())
    out.lines(
        [
            out.console.paint(
                f"{assertion.short} now has {len(assertion.evidence)} evidence record(s)", "green"
            )
        ]
    )
    return EXIT_OK


def cmd_falsifier(args: argparse.Namespace, out: Output) -> int:
    store = open_store(args)
    assertion = store.get(args.id)
    for falsifier in parse_falsifiers(args.falsifier):
        assertion = store.add_falsifier(assertion.id, falsifier)
    store.close()
    out.json("falsifier", assertion.to_dict())
    out.lines(
        [
            out.console.paint(
                f"{assertion.short} now has {len(assertion.falsifiers)} check(s)", "green"
            )
        ]
    )
    return EXIT_OK


def cmd_check(args: argparse.Namespace, out: Output) -> int:
    store = open_store(args)
    report = falsify.check(
        store,
        assertion_ids=args.ids or None,
        allow_exec=args.allow_exec,
        root=args.root,
        timeout=args.timeout,
        record=args.record,
    )
    store.close()

    out.json("check", report.to_dict(), ok=report.ok)
    out.lines(render.render_check(out.console, report))
    return EXIT_OK if report.ok else EXIT_INTEGRITY


def cmd_verify(args: argparse.Namespace, out: Output) -> int:
    store = open_store(args)
    problems = store.check_integrity()
    stats = store.stats()
    store.close()

    out.json(
        "verify",
        {"problems": problems, "checked": stats["assertions"]},
        ok=not problems,
    )
    if problems:
        out.lines(
            [out.console.paint(f"{len(problems)} integrity problem(s)", "red", "bold"), ""]
        )
        for problem in problems:
            out.lines(
                [
                    "  "
                    + out.console.paint(problem["kind"], "red")
                    + "  "
                    + out.console.paint(problem.get("id", "")[:12], "cyan")
                    + "  "
                    + out.console.paint(problem.get("detail", ""), "dim")
                ]
            )
        return EXIT_INTEGRITY

    out.lines(
        [
            out.console.paint(
                f"{out.console.glyph('check')} {stats['assertions']} assertion(s), "
                f"{stats['evidence']} evidence record(s) — all hashes intact",
                "green",
            )
        ]
    )
    return EXIT_OK


def cmd_blame(args: argparse.Namespace, out: Output) -> int:
    store = open_store(args)
    tree = graph.blame(store, args.id, max_depth=args.depth)
    store.close()
    out.json("blame", tree.to_dict())
    out.lines(render.render_blame(out.console, tree))
    return EXIT_OK


def cmd_impact(args: argparse.Namespace, out: Output) -> int:
    store = open_store(args)
    result = graph.impact(store, args.id)
    store.close()
    out.json("impact", result)

    lines = [
        out.console.paint(f"{result['total']} assertion(s) rest on this belief", "bold"),
        "",
    ]
    for item in result["conclusions"]:
        lines.append(
            "  "
            + out.console.paint("inferred  ", "magenta")
            + out.console.paint(item["short"], "cyan")
            + "  "
            + render.truncate(item["text"], 60)
        )
    for item in result["referencing"]:
        lines.append(
            "  "
            + out.console.paint("refers    ", "dim")
            + out.console.paint(item["short"], "cyan")
            + "  "
            + out.console.paint(render.truncate(item["text"], 60), "dim")
        )
    if not result["total"]:
        lines.append(out.console.paint("  nothing depends on it — safe to retract", "green"))
    out.lines(lines)
    return EXIT_OK


def cmd_orphans(args: argparse.Namespace, out: Output) -> int:
    store = open_store(args)
    orphans = graph.find_orphans(store)
    store.close()
    out.json("orphans", [a.to_dict() for a in orphans], meta={"count": len(orphans)})
    if not orphans:
        out.lines([out.console.paint("no orphaned conclusions", "green")])
    else:
        out.lines(
            [out.console.paint(f"{len(orphans)} conclusion(s) lost all their premises", "red", "bold"), ""]
        )
        out.lines(render.render_list(out.console, orphans))
    return EXIT_OK if orphans else EXIT_EMPTY


def cmd_at_risk(args: argparse.Namespace, out: Output) -> int:
    store = open_store(args)
    risky = graph.find_at_risk(store)
    store.close()
    out.json("at-risk", [a.to_dict() for a in risky], meta={"count": len(risky)})
    if not risky:
        out.lines([out.console.paint("nothing is standing on partial support", "green")])
    else:
        out.lines(
            [out.console.paint(f"{len(risky)} conclusion(s) lost some of their support", "yellow", "bold"), ""]
        )
        out.lines(render.render_list(out.console, risky))
    return EXIT_OK if risky else EXIT_EMPTY


def cmd_conflicts(args: argparse.Namespace, out: Output) -> int:
    store = open_store(args)
    conflicts = graph.find_conflicts(store)
    store.close()
    out.json("conflicts", [c.to_dict() for c in conflicts], meta={"count": len(conflicts)})
    out.lines(render.render_conflicts(out.console, conflicts))
    return EXIT_OK if conflicts else EXIT_EMPTY


def cmd_diff(args: argparse.Namespace, out: Output) -> int:
    store = open_store(args)
    result = graph.diff(store, args.before, args.after)
    store.close()
    out.json("diff", result.to_dict())
    out.lines(
        [out.console.paint(f"worldview {args.before} ", "bold") + out.console.paint("→ ", "dim") + out.console.paint(args.after, "bold"), ""]
    )
    out.lines(render.render_diff(out.console, result))
    return EXIT_OK


def cmd_stats(args: argparse.Namespace, out: Output) -> int:
    store = open_store(args)
    stats = store.stats()
    store.close()
    out.json("stats", stats)
    out.lines(render.render_stats(out.console, stats))
    return EXIT_OK


def cmd_export(args: argparse.Namespace, out: Output) -> int:
    store = open_store(args)
    rows = list(store.export_rows())
    store.close()

    if args.format == "json":
        text = json.dumps(rows, ensure_ascii=False, indent=2)
    else:
        text = "\n".join(json.dumps(row, ensure_ascii=False) for row in rows)
    if text and not text.endswith("\n"):
        text += "\n"

    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(text)
        out.json("export", {"path": args.output, "rows": len(rows)})
        out.note(f"exported {len(rows)} assertion(s) to {args.output}")
    else:
        if args.as_json:
            out.json("export", rows, meta={"count": len(rows)})
        else:
            sys.stdout.write(text)
    return EXIT_OK


def cmd_import(args: argparse.Namespace, out: Output) -> int:
    store = open_store(args)
    if not os.path.isfile(args.path):
        raise NotFound(f"no such file: {args.path}")

    rows: List[Dict[str, Any]] = []
    with open(args.path, "r", encoding="utf-8") as handle:
        content = handle.read().strip()
    if not content:
        raise Conflict(f"{args.path} is empty")
    if content.startswith("["):
        rows = json.loads(content)
    else:
        for line in content.splitlines():
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))

    result = store.import_rows(rows)
    stats = store.stats()
    store.close()

    out.json("import", result, meta={"assertions_total": stats["assertions"]})
    out.lines(
        [
            out.console.paint(
                f"merged {result['imported']} new assertion(s), "
                f"{result['events_added']} new event(s), "
                f"{result['skipped']} already present",
                "green",
            )
        ]
    )
    return EXIT_OK


# --------------------------------------------------------------------------
# dispatch
# --------------------------------------------------------------------------

COMMANDS: Dict[str, Callable[[argparse.Namespace, Output], int]] = {
    "init": cmd_init,
    "assert": cmd_assert,
    "get": cmd_get,
    "query": cmd_query,
    "log": cmd_log,
    "history": cmd_history,
    "retract": cmd_retract,
    "supersede": cmd_supersede,
    "annotate": cmd_annotate,
    "evidence": cmd_evidence,
    "falsifier": cmd_falsifier,
    "check": cmd_check,
    "verify": cmd_verify,
    "blame": cmd_blame,
    "impact": cmd_impact,
    "orphans": cmd_orphans,
    "at-risk": cmd_at_risk,
    "conflicts": cmd_conflicts,
    "diff": cmd_diff,
    "stats": cmd_stats,
    "export": cmd_export,
    "import": cmd_import,
}

ALIASES = {"add": "assert", "show": "get", "ls": "query"}


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return EXIT_OK

    console = render.Console(
        colour=False if getattr(args, "no_color", False) else None,
        ascii_only=getattr(args, "ascii", False),
    )
    out = Output(bool(getattr(args, "as_json", False)), console)

    command = ALIASES.get(args.command, args.command)
    handler = COMMANDS.get(command)
    if handler is None:  # pragma: no cover - argparse rejects unknown commands
        parser.print_help()
        return EXIT_USAGE

    try:
        return handler(args, out)
    except NotFound as exc:
        out.error(str(exc))
        if out.as_json:
            out.json(command, None, ok=False, meta={"error": str(exc), "code": "not_found"})
        return exc.exit_code
    except (AmbiguousId, CycleDetected, Conflict) as exc:
        out.error(f"error: {exc}")
        if out.as_json:
            out.json(command, None, ok=False, meta={"error": str(exc), "code": type(exc).__name__})
        return exc.exit_code
    except StrataError as exc:
        out.error(f"error: {exc}")
        if out.as_json:
            out.json(command, None, ok=False, meta={"error": str(exc), "code": "strata_error"})
        return exc.exit_code
    except BrokenPipeError:  # pragma: no cover - `strata query | head`
        try:
            sys.stdout.close()
        except Exception:
            pass
        return EXIT_OK
    except KeyboardInterrupt:  # pragma: no cover
        out.error("interrupted")
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
