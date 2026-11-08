# strata — design notes

> This document explains *why* strata looks the way it does. If you only want to
> use it, read `README.md`. If you want to argue with it, read this.

## 0. The claim

Most "memory" systems for AI are retrieval systems wearing the word memory.

A retrieval system answers: *which stored text is closest to this query?*
A memory system answers: *what do we believe, why do we believe it, when did that
change, and what breaks if we're wrong?*

Those are different problems. The first is a ranking problem, solved reasonably
well by embeddings. The second is an **epistemology** problem, and the database
industry solved most of it between 1985 and 2005 — audit logs, MVCC, bitemporal
tables, provenance, dependency tracking. Then a new field arrived and threw all
of it away in favour of cosine similarity.

strata is an attempt to put the parts back that matter.

## 1. Six failure modes of embedding-shaped memory

These are the concrete failures strata is designed against. Every design decision
below traces back to one of them.

| # | Failure | What it looks like in practice |
|---|---------|-------------------------------|
| F1 | **Unauditable** | The agent "knows" something. Nobody can point at where it came from. |
| F2 | **Incorrigible** | You discover a memory is wrong. You fix it. Nothing downstream notices. |
| F3 | **A-temporal** | "The project uses Postgres" (March) and "…uses MySQL" (June) sit side by side, equally weighted. Recency is guessed, not modelled. |
| F4 | **Flat confidence** | A fact read directly from a config file and a fact from a throwaway remark get identical weight. |
| F5 | **Unfalsifiable** | There is no way to ask "is this still true?" — only "does it look similar?" |
| F6 | **Destructive** | Deleting a memory destroys the evidence that it was ever held, so you cannot learn from your own mistakes. |

## 2. The central object: an assertion, not a chunk

A memory chunk is a blob of text with a vector. An **assertion** is a claim with
a shape:

```
subject   "project:acme/db"
predicate "engine"
value     "postgres"
text      "Acme's primary datastore is PostgreSQL 16."
status    observed
```

`text` is for humans. `subject/predicate/value` is for machines — it is what
makes two assertions *comparable*. Without a normalised key you can never detect
that two memories contradict each other; you can only detect that two strings
are lexically dissimilar, which is not the same thing.

The key is deliberately **not** RDF-shaped. Real beliefs are rarely clean triples.
`value` is nullable, so a purely qualitative assertion ("the deploy process is
considered fragile") still has an identity — `subject: "project:acme/deploy"`,
`predicate: "assessment"`, `value: null`.

## 3. Content addressing

```python
id = sha256(canonical(subject) || canonical(predicate) || canonical(value) || canonical(text))[:16]
```

Four consequences, all of them wanted:

1. **Deduplication is free.** Re-asserting the same proposition does not create a
   second row. It creates a second *testimony* (see §5).
2. **Ids are stable across machines.** Two independently running agents that
   observe the same fact derive the same id, so their ledgers merge cleanly.
3. **Tampering is detectable.** `strata verify` recomputes every id; a mutated
   row cannot keep its id.
4. **Nothing is ever "renamed".** There is no UPDATE path for the proposition.
   A changed belief is a different assertion that supersedes the old one.

`asserted_at`, `status`, `evidence` and `actor` are **not** part of the hash.
They describe the *act of asserting*, not the proposition. This is what lets the
same proposition accrete evidence over time and lets two witnesses agree.

## 4. Two clocks (bitemporal)

Every assertion carries two independent time ranges:

- **Assertion time** — when the ledger learned it. Immutable, set at insert.
- **Valid time** — when the claim is true *in the world*. `valid_from`/`valid_to`,
  both optional, both settable to the past.

Keeping them apart is the difference between these two questions:

- "What did we believe on 1 May?" → filter on assertion time. (F3, F6)
- "What was true on 1 May, according to what we know now?" → filter on valid time.

Most systems conflate them and can therefore never answer either question
correctly. Backfilling a fact learned today about last year is only representable
because the clocks are separate.

## 5. Append-only, with events not mutations

The `assertions` table is insert-only. State changes are separate events:

| event | meaning |
|-------|---------|
| `assert` | this proposition was put forward, by this actor, with these evidence |
| `retract` | we no longer stand behind it (and here is why) |
| `supersede` | a newer proposition replaces it (closes its valid time) |
| `annotate` | free-form note; does not change standing |

`retract` never deletes. This is F6: the record that we once believed something
wrong is *the most valuable row in the database*, because it is the only thing
that can teach a future reader why the current belief looks the way it does.

Repeated `assert` events on the same id form a **testimony chain**. Three
independent observers asserting the same proposition is epistemically stronger
than one, and strata can say so because it kept the count.

## 6. Epistemic status instead of a confidence float

A float like `0.83` is false precision: nothing in the system can justify the
third significant figure, and downstream code will compare floats anyway.

strata uses four discrete, *auditable* statuses:

| status | meaning | typical evidence |
|--------|---------|------------------|
| `observed` | read directly from the world | file contents, command output |
| `reported` | stated by a source we did not verify | a user, a doc, a web page |
| `inferred` | derived from other assertions | **must** declare `depends_on` |
| `assumed` | taken as a premise, unverified | design decisions, constraints |

`inferred` is the load-bearing one: it creates edges in the dependency graph, and
that graph is what makes F2 solvable.

## 7. The dependency graph and cascade invalidation

When an assertion is asserted with `status=inferred`, it must point at the
assertions it derives from. Edges are stored as `from → to` (downstream → upstream).

Retracting or falsifying an assertion walks **downstream** and classifies every
descendant:

- **`orphaned`** — *all* upstream support is gone. The conclusion has no basis left.
- **`at-risk`** — *some* upstream support is gone. Still standing, on fewer legs.

Neither is deleted. Both are surfaced loudly. This is the answer to F2: you fix
one bad input, and the system tells you exactly which conclusions were resting
on it, instead of silently serving them forever.

The walk is a recursive CTE, so it stays fast at six figures of assertions and
needs no application-side graph library.

## 8. Falsifiers: memory that can be checked

Any assertion may carry a machine-executable falsifier:

```json
{"kind": "file_contains", "path": "docker-compose.yml", "pattern": "postgres:16"}
{"kind": "file_exists",   "path": ".github/workflows/deploy.yml"}
{"kind": "command_success", "argv": ["git", "rev-parse", "--verify", "main"]}
```

`strata check` runs them and sorts assertions into `holds`, `falsified`, `error`.

Two safety decisions:

- **`command_*` kinds are refused unless `--allow-exec` is passed.** A memory
  store that executes arbitrary payloads from its own rows is a remote code
  execution primitive with extra steps. The default must be filesystem-only.
- Falsifiers **carry their own digest**, computed over the canonical spec bytes,
  and are stored in their own table rather than folded into the assertion id.
  Two reasons: attaching a check to an existing belief must not change its
  identity (a check is metadata about a proposition, exactly like evidence), and
  a per-row digest is sufficient — `verify` recomputes it, so a tampered check
  cannot masquerade as a passing one.

This is F5, and it is the feature I would most want as a consumer: the ability to
ask a memory *"are you still true?"* rather than *"are you still similar?"*

## 9. No embeddings. On purpose.

strata stores no vectors and calls no model. Not as a limitation, but as a
position:

- A vector index cannot tell you **why** a row was returned. Provenance can.
- Similarity ≠ entailment. High cosine score between "we use Postgres" and "we
  migrated off Postgres" is a bug, not a feature.
- Retrieval quality is a moving target; **the ledger format must not move**. Any
  retrieval layer — BM25, embeddings, a future model — can be bolted on top of a
  stable, auditable substrate. The reverse is not true: you cannot bolt
  provenance onto a pile of vectors.

`strata query --text` provides a simple tokenised match so the tool is usable
standalone. It is explicitly not the point.

## 10. Storage: one SQLite file, zero dependencies

`pip install` nothing. `import` only stdlib. A `.strata` file is a normal SQLite
database; `sqlite3 foo.strata .dump` works, and so does opening it in any SQLite
browser ten years from now.

This is the trust argument. A memory layer sits underneath everything else and
will outlive the tool that wrote it. Its format must be (a) inspectable without
strata, (b) readable without a network, (c) implementable by someone else from
the spec in a weekend. WAL mode, `synchronous=NORMAL`, one file, no daemon.

## 11. What strata deliberately does not do

Scope discipline is a feature. strata is not:

- **A vector database.** See §9.
- **An agent framework.** It is a CLI and a 200-line Python library. It never
  calls a model, never opens a socket.
- **A sync service.** `export`/`import` emit mergeable JSONL; replication is the
  user's problem, and content addressing makes their life easy.
- **A UI.** Exit codes and `--json` are the interface. A TUI would be nice; a
  worse contract would not.
- **A wiki.** Prose is not an assertion. If you cannot name a subject and a
  predicate, you probably want a note-taking app.

## 12. Exit codes and the machine contract

Agents are first-class consumers, so the contract is explicit and never changes
within a major version:

| code | meaning |
|------|---------|
| 0 | success |
| 1 | nothing found / operation not applicable |
| 2 | usage error |
| 3 | integrity failure (`verify`, `check` found a falsified assertion) |
| 4 | conflict (ambiguous id prefix, supersede cycle) |

With `--json`, stdout is exactly one JSON object and nothing else. Logs and
warnings go to stderr. Every list command returns `{"ok": bool, "data": [...],
"meta": {...}}`.

## 13. Open questions

Honest list of things I am not sure about:

1. **Predicate vocabulary.** Free-form predicates are flexible but drift
   (`db.engine` vs `engine`). A suggested-but-unenforced vocabulary seems right;
   an enforced one would be hostile.
2. **`value` normalisation.** Casefolding `Postgres` → `postgres` for comparison
   but storing the original is a compromise. It will mis-merge in edge cases.
3. **Falsifier staleness.** A check that passed last year proves nothing today.
   `check` records `last_checked`; nothing forces a re-check. Should passing
   checks expire? Probably yes, eventually, with a per-assertion TTL.
4. **Multi-writer concurrency.** SQLite handles it; the *semantics* of two agents
   retracting each other's assertions are undefined. A "dispute" state may be
   needed.
5. **Whether `inferred` edges can be inferred.** Right now the caller must supply
   `--depends-on`. Asking a model to fill that in is tempting and probably
   dangerous.
