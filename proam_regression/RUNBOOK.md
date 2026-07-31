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

Standing numbers: the only expected failures anywhere are the two blocked
gear-parser tests (die with register decision G3/D2).

## Where the evidence lands

Every delivered cycle ships `RECEIPT_<sha>_*.log` files (full pytest output
per lane, mutation battery output) into `_claude_inbox/` on the operator's
machine, alongside `STATUS.md`. The per-cycle verdict documents live in the
STRATHEX project. Commit messages carry the adversarial record: baseline
before fix, mutation battery results, and regression counts per lane.

## Standing up the rig elsewhere

Until register decision D11-C lands (bootstrap script + database dump
artifact), building the templates requires the cloud rig's Postgres. After
D11-C, one command rebuilds the full rig from the repo on any machine; that
is the path to running this suite in CI and on the operator's machine.
Template build/rebuild procedures: see the C32 recovery doc and the
docstrings in `stage_multitournament.py` and `test_o3_ordering.py`.
