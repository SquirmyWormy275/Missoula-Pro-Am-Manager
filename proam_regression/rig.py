"""
Real-data rig for the Missoula Pro-Am regression harness.

CONTRACT
--------
Every test in this suite runs against a byte copy of the real April 2026
production database. There are no fixtures, no factories, and no mocks.

This is deliberate. The 2026 failure was not "we had no tests". The repo
shipped a large green test suite. The failure was that the fixtures and the
mocks were written by the same reasoning that wrote the bugs, so the test
shape matched the bug shape and the suite went green on broken code. The repo
documents this in its own postmortem:
docs/solutions/test-failures/test-shape-matches-bug-shape-trilogy-2026-04-23.md

The only defense that survives that failure mode is to stop inventing inputs.
Every assertion here is made against production rows, driven through real HTTP,
and checked by reading the database back.

SETUP (one time per machine)
----------------------------
Load the production plain-SQL dump into a template database named by
TEMPLATE_DB below. Every test then clones that template, so tests never share
state and never mutate the reference copy.
"""

import os
import subprocess
import uuid

PG_USER = os.environ.get("PROAM_RIG_USER", "proam")
PG_PASS = os.environ.get("PROAM_RIG_PASS", "proam")
PG_HOST = os.environ.get("PROAM_RIG_HOST", "localhost")
PG_PORT = os.environ.get("PROAM_RIG_PORT", "5432")

# The read-only reference copy of real production data. Never written to.
TEMPLATE_DB = os.environ.get("PROAM_RIG_TEMPLATE", "proam_prod_mirror")

# Path to the checked-out application under test.
APP_ROOT = os.environ.get("PROAM_APP_ROOT", "/tmp/proam")

# Real production identifiers. These are not arbitrary test values; they are
# the actual rows the 2026 show ran on.
TOURNAMENT_ID = 2
ADMIN_USER_ID = 1


def _url(dbname):
    return f"postgresql://{PG_USER}:{PG_PASS}@{PG_HOST}:{PG_PORT}/{dbname}"


def _psql(dbname, sql):
    env = dict(os.environ, PGPASSWORD=PG_PASS)
    return subprocess.run(
        ["psql", "-h", PG_HOST, "-p", PG_PORT, "-U", PG_USER, "-d", dbname,
         "-v", "ON_ERROR_STOP=1", "-tAc", sql],
        env=env, capture_output=True, text=True,
    )


def clone_production():
    """Create a private copy of the production mirror. Returns (dbname, url)."""
    name = "proam_rt_" + uuid.uuid4().hex[:10]
    r = _psql("postgres", f'CREATE DATABASE "{name}" TEMPLATE {TEMPLATE_DB};')
    if r.returncode != 0:
        raise RuntimeError(
            f"could not clone {TEMPLATE_DB}: {r.stderr.strip()}\n"
            f"Load the production dump into {TEMPLATE_DB} first."
        )
    return name, _url(name)


def drop_clone(name):
    _psql("postgres", f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE);')


def orphan_clones():
    """Clone databases left behind by a killed run."""
    r = _psql("postgres",
              "SELECT datname FROM pg_database WHERE datname LIKE 'proam_rt_%';")
    if r.returncode != 0:
        return []
    return [line for line in r.stdout.split() if line]


def drop_orphans():
    """Reap clones from previous runs. A SIGKILLed pytest never runs its finally."""
    dropped = []
    for name in orphan_clones():
        drop_clone(name)
        dropped.append(name)
    return dropped


def template_is_loaded():
    """True only if the template holds real production data, not just schema."""
    r = _psql(TEMPLATE_DB, "SELECT count(*) FROM event_results;")
    if r.returncode != 0:
        return False
    try:
        return int(r.stdout.strip()) > 0
    except ValueError:
        return False
