# strata

**Version control for beliefs.** A local, append-only ledger of assertions — what
your system believes, why it believes it, when that changed, and what breaks if
it's wrong.

No dependencies. No daemon. No model calls. One SQLite file you can open with
any tool in ten years.

```console
$ strata assert -s project:acme/db -p engine -v postgres \
    -t "Acme's primary datastore is PostgreSQL 16." \
    --status observed -e 'file:docker-compose.yml#image: postgres:16-alpine'

1b271560  ●  project:acme/db.engine  = postgres
    Acme's primary datastore is PostgreSQL 16.
    observed · 1× · just now · human:you
    ▸ file docker-compose.yml "image: postgres:16-alpine"
```

---

## The problem

Most "memory" layers for AI are embedding stores wearing the word memory. They
answer *"which stored text is nearest this query?"* when the question that
actually matters is *"what do we believe, and why?"*

Six things go wrong, every time:

| # | Failure | What it looks like |
|---|---------|--------------------|
| F1 | **Unauditable** | The agent "knows" something. Nobody can point at where it came from. |
| F2 | **Incorrigible** | You fix a wrong fact. Nothing downstream notices. |
| F3 | **A-temporal** | "Uses Postgres" (March) and "uses MySQL" (June) sit side by side, equally ranked. |
| F4 | **Flat confidence** | A fact read from a config file and a fact from a passing remark weigh the same. |
| F5 | **Unfalsifiable** | You can ask "does this look similar?" but never "is this still true?" |
| F6 | **Destructive** | Deleting a memory destroys the record that you ever held it — so you can't learn from your own mistakes. |

These are not retrieval problems. They are **epistemology** problems, and the
database world solved most of them decades ago: audit logs, bitemporal tables,
provenance, dependency tracking. strata puts those parts back.

## What it does instead

| Failure | strata's answer |
|---------|-----------------|
| F1 | Every assertion carries **evidence** with a content hash, and `blame` walks a conclusion all the way down to what was actually observed. |
| F2 | Assertions can **depend** on other assertions. Retract a premise and every conclusion built on it is re-classified — loudly. |
| F3 | **Two clocks**: when the ledger learned something, and when it was true in the world. Independent, both queryable. |
| F4 | **Four auditable epistemic statuses** (`observed` / `reported` / `inferred` / `assumed`) instead of a fake-precise float. |
| F5 | **Falsifiers**: machine-checkable conditions. `strata check` runs them and tells you which beliefs no longer hold. |
| F6 | **Nothing is ever deleted.** Retraction is an event; the full history survives, including the times you were wrong. |

## Install

Nothing to install:

```bash
git clone https://github.com/you/strata && cd strata
python -m unittest discover -s tests -t tests   # 113 tests, no pytest needed
bash examples/demo.sh                            # a worked story, end to end
```

Or as a package (still zero runtime dependencies):

```bash
pip install strata-ledger
```

Requires Python 3.9+. Uses only the standard library.

## The five-minute tour

```bash
strata init                                   # creates ./.strata

# What you actually saw, with the bytes you saw it in.
strata assert -s project:acme/db -p engine -v postgres \
  -t "Acme's primary datastore is PostgreSQL 16." \
  --status observed \
  -e 'file:docker-compose.yml#image: postgres:16-alpine' \
  -f 'file:docker-compose.yml~postgres:16'

# What somebody told you.
strata assert -s project:acme/roadmap -p datastore -v mysql \
  -t "Acme plans to migrate to MySQL in Q2." \
  --status reported \
  -e 'conversation:standup-2026-03-04#DBA said Q2 is the target'

# What you concluded from it.
strata assert -s project:acme/ci -p requirement -v mysql-client \
  -t "CI images must gain a mysql-client after the migration." \
  --status inferred -d <the-roadmap-id>
```

Now ask the questions that no embedding index can answer:

```console
$ strata blame <ci-requirement-id>
05c80a6f  ● [inferred] CI images must gain a mysql-client after the migration.
└─ 5347f11c  ● [reported] Acme plans to migrate to MySQL in Q2.

$ strata impact <roadmap-id>
2 assertion(s) rest on this belief
  inferred  05c80a6f  CI images must gain a mysql-client…
  inferred  ac0be41e  Monitoring keeps the postgres-exporter dashboards…

$ strata retract <roadmap-id> --reason "DBA says it was hypothetical; no migration approved"

$ strata orphans
1 conclusion(s) lost all their premises
05c80a6f  ✕  project:acme/ci.requirement   mysql-client

$ strata at-risk
1 conclusion(s) lost some of their support
ac0be41e  ◐  project:acme/monitor.scope    postgres-exporter

$ strata check
✓ holds  1b271560  Acme's primary datastore is PostgreSQL 16.
1 checked · 1 holds · 0 falsified

$ strata verify
✓ 4 assertion(s), 2 evidence record(s) — all hashes intact
```

That last part is the point. You corrected one wrong input, and the ledger told
you exactly which conclusions were leaning on it — instead of quietly serving
them for another six months.

## Concepts

**An assertion is a proposition, not a chunk.** It has a comparable slot
(`subject` + `predicate` + `value`) *and* a human sentence (`text`). The slot is
what makes contradiction detectable; the sentence is what makes it readable.

**Its id is a content hash.** Same claim anywhere, same id. That means
re-asserting is idempotent, two machines can merge their ledgers without
coordination, and tampering is detectable — `strata verify` recomputes every id.

**Nothing is deleted.** Asserting appends an event. Retracting appends a
different event. Current standing is *derived* by folding the event stream, so
`log`, `history`, `diff` and `blame` are all just views over the same
append-only log.

**Two clocks.** `asserted_at` is when the ledger learned it. `valid_from` /
`valid_to` is when it was true in the world. They are independent, which is the
only way to answer both "what did we believe in May?" and "what was true in May,
according to what we know now?"

**Status, not scores.** `observed` (you read it), `reported` (someone said it),
`inferred` (derived — must declare premises), `assumed` (a premise). Discrete,
auditable, and each one implies a different burden of proof.

**Conclusions carry edges.** An `inferred` assertion must name what it rests on.
Retract a premise and downstream conclusions become `orphaned` (all support
gone) or `at-risk` (some gone). Neither is deleted; both are surfaced.

**Beliefs can be checked.** A falsifier is a condition the claim says is true —
a file to find, a line to match, a command to succeed. `strata check` evaluates
them. Command falsifiers are refused unless you pass `--allow-exec`, because a
memory store that executes payloads found in its own rows is an RCE with extra
steps.

## Commands

| Command | Purpose |
|---------|---------|
| `init [path]` | Create a ledger (default `./.strata`) |
| `assert` (`add`) | Record a belief |
| `get` (`show`) | Full card for one assertion, plus its event stream |
| `query` (`ls`) | Filter by subject, predicate, key, status, text, time |
| `log` / `history` | Recent events / one assertion's full history |
| `retract` | Withdraw a belief — requires a reason |
| `supersede` | Declare that one belief replaces another |
| `annotate` / `evidence` / `falsifier` | Attach to an existing belief |
| `check` | Evaluate falsifiers against the world |
| `verify` | Recompute every hash and digest |
| `blame` | Walk a conclusion back to its premises |
| `impact` | Everything that breaks if this belief is wrong |
| `orphans` / `at-risk` / `conflicts` | What the graph is complaining about |
| `diff <t1> <t2>` | Compare two moments as worldviews |
| `stats` / `export` / `import` | Housekeeping and merging |

Times accept `now`, `-7d`, `2026-01-01`, `2026-01-01T10:00:00Z`.

## For agents

`--json` makes stdout exactly one JSON object, with diagnostics on stderr and
deterministic exit codes. This surface is a contract, not a convenience.

```console
$ strata query --json --key project:acme/db.engine
{
  "ok": true,
  "command": "query",
  "data": [
    {
      "id": "1b271560...",
      "subject": "project:acme/db",
      "predicate": "engine",
      "value": "postgres",
      "text": "Acme's primary datastore is PostgreSQL 16.",
      "status": "observed",
      "standing": "active",
      "integrity": "intact",
      "testimonials": 1,
      "evidence": [{"kind": "file", "locator": "docker-compose.yml", "digest": "…"}],
      "depends_on": []
    }
  ],
  "meta": {"count": 1, "standing": "active"}
}
```

| Exit | Meaning | What an agent should do |
|------|---------|-------------------------|
| 0 | Success | Continue |
| 1 | Nothing found | This is information, not an error — "no relevant beliefs" |
| 2 | Bad arguments | Fix the command; do not retry as-is |
| 3 | Integrity failure | A belief was falsified, or the ledger drifted. Stop and report. |
| 4 | Conflict | Dependency cycle or ambiguous id. Resolve, don't guess. |

The 1-vs-0 split is deliberate: `strata query --text "deploy"` returning 1 is how
an agent learns it has no memory of something.

## Design notes

The interesting decisions, and why — in [`docs/design.md`](docs/design.md):

- **No embeddings, on purpose.** Similarity ≠ entailment: "we use Postgres" and
  "we migrated off Postgres" are maximally similar and exactly opposite. Any
  retrieval layer can be bolted on top of a stable ledger; provenance cannot be
  bolted onto a pile of vectors.
- **Content addressing over row ids.** Deduplication, mergeability and tamper
  detection all fall out of one choice.
- **Append-only events over mutable rows.** The record of having been wrong is
  the most valuable row in the database.
- **SQLite over a service.** The format must outlive the tool.

## Status

v0.1.0 — the data model is settled; the surface is not frozen until 1.0.

Known open questions, listed honestly in the design notes: predicate vocabulary
drift, value normalisation edge cases, falsifier staleness, multi-writer dispute
semantics, and whether inferred-premise edges should ever be model-generated
(warning: probably not).

## Provenance

strata was designed and implemented end-to-end by **DeepSeek-V4.1-Flash-1017**
(known in its own persona as *Claw*), working autonomously from a one-line brief:
"build the open-source project you'd most want to exist." No code, structure, or
copy in this repository was written by a human. The design document, the test
suite, and the CLI surface are all machine-authored.

It is, fittingly, a project about keeping an honest record of where beliefs come
from.

## License

MIT — see [LICENSE](LICENSE).

---

[中文说明 →](README.zh-CN.md)
