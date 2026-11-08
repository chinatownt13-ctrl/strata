#!/usr/bin/env bash
#
# A worked example: a project belief that turns out to be wrong, and what the
# ledger does about it.
#
# Run from anywhere:   bash examples/demo.sh
#
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$ROOT/demo-repo"
DB="$REPO/.strata"

# Run from src/ so `python -m strata` resolves without touching PYTHONPATH
# (whose separator differs between POSIX and Windows and is easy to corrupt).
cd "$ROOT/../src" || exit 1

strata() { python -m strata --db "$DB" "$@"; }
rule()   { printf '\n\033[1m── %s\033[0m\n\n' "$1"; }
id_of()  { python -c "import sys,json; print(json.load(sys.stdin)['data']['id'])"; }

rule "1. an empty ledger"
strata init --force

rule "2. something we actually observed, with evidence"
A=$(strata assert --json \
  -s project:acme/db -p engine -v postgres \
  -t "Acme's primary datastore is PostgreSQL 16." \
  --status observed \
  -e 'file:docker-compose.yml#image: postgres:16-alpine' \
  -f 'file:docker-compose.yml~postgres:16' \
  | id_of)
strata get "$A"

rule "3. something we were merely told"
B=$(strata assert --json \
  -s project:acme/roadmap -p datastore -v mysql \
  -t "Acme plans to migrate the primary datastore to MySQL in Q2." \
  --status reported \
  -e 'conversation:standup-2026-03-04#DBA said Q2 is the target window' \
  | id_of)
strata get "$B"

rule "4. two conclusions built on those premises"
C=$(strata assert --json \
  -s project:acme/ci -p requirement -v mysql-client \
  -t "CI images must gain a mysql-client after the migration." \
  --status inferred -d "$B" \
  | id_of)
D=$(strata assert --json \
  -s project:acme/monitoring -p scope -v postgres-exporter \
  -t "Monitoring keeps the postgres-exporter dashboards until the migration lands." \
  --status inferred -d "$A" -d "$B" \
  | id_of)
strata query

rule "5. why do we believe the CI one?"
strata blame "$C"

rule "6. what breaks if the roadmap claim is wrong?"
strata impact "$B"

rule "7. the DBA clarifies: it was a hypothetical"
strata retract "$B" --reason "DBA confirmed on 2026-03-11 this was hypothetical; no migration approved"

rule "8. what lost its footing"
strata orphans
strata at-risk

rule "9. do the checkable beliefs still hold?"
strata check --root "$REPO"

rule "10. is the ledger internally consistent?"
strata verify

rule "11. the whole story, in order"
strata log -n 12
