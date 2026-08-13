# proam_regression RUNBOOK

**If you ran `pytest` and this directory did not execute, that is by design,
not neglect.** `pytest.ini` pins `testpaths = tests` because this suite needs
a live PostgreSQL clone of the 2026 production database. It is invoked
explicitly, and it runs on every hardening cycle in the cloud rig, across
multiple lanes, with the raw logs delivered as receipts. The 2026-07-30 audit
conclusion "this suite has never run" was a visibility artifact of exactly
this split; this file exists so nobody reaches it again.

## How it runs

    PROAM_APP_ROOT=<repo root> \
    PROAM_RIG_TEMPLATE=<template db> \
    SECRET_KEY=<any 64 chars> \
    python -m pytest proam_regression -p no:randomly -q

Every test clones the template database (see `rig.py`), drives the real app
over HTTP against that clone, and drops the clone afterward. Nothing here
touches the template itself.

## The lanes

| lane      | template                       | purpose                                    |
|-----------|--------------------------------|--------------------------------------------|
| normal    | proam_prod_mirror_p0           | the 2026 production mirror (post-c38 reseed)|
| reversed  | proam_prod_mirror_p0rev        | identical data, physical row order reversed; order-nondeterminism detector (c35) |
| oracle    | proam_prod_mirror_mt           | mirror + staged 2027 tournament; cross-tournament leak detector (c37) |
| pristine  | proam_prod_mirror_2026pristine | pre-reseed archive; used only by test_college_id_reseed.py to keep proving the c38 migration |

The pristine lane is invoked differently and getting it wrong is silent.
`test_college_id_reseed.py` hardcodes its own template name for cloning, but
the session-level real-data gate validates the configured template first. Set
the pristine template explicitly:

    PROAM_APP_ROOT=<repo root> PROAM_RIG_TEMPLATE=proam_prod_mirror_2026pristine \
    SECRET_KEY=<any 64 chars> \
    python -m pytest proam_regression/test_college_id_reseed.py -p no:randomly -q

## Standing numbers

Measured locally on 2026-08-12 after the normalized-roster and gear-conflict
work. A lane that comes back different from this is telling you something,
and the something is usually yours.

| lane      | passed | skipped | xfailed | failed |
|-----------|--------|---------|---------|--------|
| normal    | 205    | 6       | 2       | 0      |
| reversed  | 205    | 6       | 2       | 0      |
| oracle    | 211    | 0       | 2       | 0      |
| pristine  | 3      | 0       | 0       | 0      |

The 2 xfails on every lane are the blocked gear-parser tests, which die with
register decision G3/D2.

The oracle lane must be clean. Its flight-order assertions are explicitly
scoped to the real 2026 tournament, so the staged 2027 heats cannot inflate
the operator-facing stand-conflict count or the 2026 show-order snapshot.

## Two traps that have each cost a cycle

**Lanes may run in parallel.** Each pytest session holds a PostgreSQL advisory
lock and names its clones `proam_rt_<run-token>_<clone-token>`. Startup cleanup
only drops clones from a token that has no live lock, so a second lane cannot
reap another live lane's databases. Pre-token legacy `proam_rt_*` names are
never deleted automatically because their owner cannot be proven dead.

**`rig.py` does not migrate its template.** After any new Alembic revision,
every template above has to be upgraded by hand before its lane will run:

    for d in proam_prod_mirror_p0 proam_prod_mirror_p0rev \
             proam_prod_mirror_mt proam_prod_mirror_2026pristine; do
      DATABASE_URL="postgresql://proam:proam@localhost:5432/$d" \
      SECRET_KEY=<any 64 chars> python -m flask db upgrade
    done

`tests/db_test_utils.py::_ensure_pg_template` does NOT have this hole, contrary
to what this file said through D12-C commit F2. It calls `_chain_head()` and
`_template_is_stale()` and drops and rebuilds `proam_unit_template` itself when
the stamped revision is not head, so the PG unit lane self-heals across a new
revision and needs no manual step. Verified at revision `t9b3c4d5e6f7`: the
four parity templates above were stale and had to be upgraded by hand, and the
unit template rebuilt on its own.

## Where the evidence lands

Every delivered cycle ships `RECEIPT_<sha>_*.log` files (full pytest output
per lane, mutation battery output) into `_claude_inbox/` on the operator's
machine, alongside `STATUS.md`. The per-cycle verdict documents live in the
STRATHEX project. Commit messages carry the adversarial record: baseline
before fix, mutation battery results, and regression counts per lane.

## Standing up the rig elsewhere

Register decision D11-C landed in c42: `scripts/rig_bootstrap.sh` plus a
`pg_dump` artifact rebuild the full rig from the repo on any machine.

**Deviation from the register text, deliberate and standing.** D11-C says the
dump ships as a GitHub release asset. It does not, and it must not. This repo is
public, release assets on a public repo are public, and the pristine dump
carries real competitor contact details including 45 phone numbers. The dump
travels privately to the operator and lives wherever he keeps it. Anything in
this suite that compares real contact data prints digests or counts, never the
values themselves; keep it that way.

Template build and rebuild procedures: see the C32 recovery doc and the
docstrings in `stage_multitournament.py` and `test_o3_ordering.py`.

## Rebuilding after a container swap (c56, third occurrence)

C32 recorded a missing template as a hard blocker requiring the operator,
because `rig_bootstrap.sh` needs the pristine dump and the dump is deliberately
not in this public repo. **Look for `/tmp/proam_dump.sql` first.** It has
survived every swap so far, and the whole rig is rebuildable in-container from
it. Identify it before trusting it, by measuring it against the load-bearing
constants in `test_college_id_reseed.py`: 64 college competitors at ids 29..92,
49 pros, 21 pro/college collisions, 20 bracket ghosts, 11 relay ghosts, 173
heats, 379 heat_assignments, 44 events, tournament id 2. Escalate only if the
file is absent or the numbers disagree.

Three things that each cost time in c56:

  - **A surviving template is not necessarily the right template.** The
    `proam_prod_mirror_p0` that came through the swap measured as pre-reseed
    (college ids 29..92, 21 collisions), i.e. a pre-c39-cutover artifact. Measure
    every template you did not build yourself in this container.
  - **`pg_restore` of the dump fails on PG 16** with `unrecognized configuration
    parameter "transaction_timeout"`, because the dump was produced by pg_dump
    18.1. Strip it alongside the `\restrict` / `\unrestrict` lines:
    `sed -e '/^\\restrict/d' -e '/^\\unrestrict/d' -e '/^SET transaction_timeout/d'
    -e 's/OWNER TO postgres;/OWNER TO proam;/'`.
  - **Export `PGPASSWORD`** before `dropdb` / `createdb` / `psql`, or the call
    hangs on a password prompt until the tool times out.

`stage_multitournament.py` disarms the write-time reference gate for the
duration of the staging run. That is deliberate, it is documented in the script,
and it is open question 17. Do not "fix" it by remapping the payouts without
remeasuring the oracle numbers above in the same commit.

## What a staged tournament has to carry (c57)

The oracle template is only worth its runtime if T3 is a complete copy. Until
c57 the staging script cloned the two heat JSON columns and skipped
`heat_assignments` entirely: T2 had 379 rows, T3 had 0, and nothing read them
yet so no lane complained. After D12-C commit E moves the roster accessors onto
the rows, that hole reads as 173 empty heats.

The rule this leaves behind: **when a table joins the roster path, clone it
here in the same commit that starts reading it.** After restaging, check the
template before trusting a green lane:

    SELECT e.tournament_id, count(DISTINCT h.id), count(a.id)
    FROM heats h JOIN events e ON e.id = h.event_id
    LEFT JOIN heat_assignments a ON a.heat_id = h.id
    GROUP BY e.tournament_id;

Both tournaments must report the same two numbers (173 heats, 379 rows), and
the two uid sets must not intersect.
