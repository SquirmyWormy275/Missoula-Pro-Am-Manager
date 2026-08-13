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
import re
import subprocess
import uuid
from contextlib import contextmanager

import psycopg2

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

# One pytest process owns one run token.  A session-level advisory lock keeps
# cleanup from mistaking another live process's per-test databases for debris.
RUN_TOKEN = os.environ.get("PROAM_RIG_RUN_TOKEN", uuid.uuid4().hex[:12])
_RUN_TOKEN_RE = re.compile(r"^[a-f0-9]{12}$")
_CLONE_NAME_RE = re.compile(r"^proam_rt_([a-f0-9]{12})_([a-f0-9]{10})$")


def _url(dbname):
    return f"postgresql://{PG_USER}:{PG_PASS}@{PG_HOST}:{PG_PORT}/{dbname}"


def _lock_key(run_token):
    return f"proam-regression:{run_token}"


def _validate_run_token(run_token):
    if not _RUN_TOKEN_RE.fullmatch(run_token):
        raise RuntimeError(
            "PROAM_RIG_RUN_TOKEN must be exactly 12 lowercase hexadecimal "
            "characters. Leave it unset to generate a safe token automatically.")


@contextmanager
def hold_run_lock():
    """Hold this process's PostgreSQL advisory lock for the test session."""
    _validate_run_token(RUN_TOKEN)
    connection = psycopg2.connect(_url("postgres"))
    connection.autocommit = True
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_lock(hashtext(%s))", (_lock_key(RUN_TOKEN),))
        yield
    finally:
        connection.close()


def run_is_active(run_token):
    """Whether a token is locked by a live harness process.

    Failure to inspect the lock is deliberately treated as active. Cleanup must
    leak a disposable clone rather than force-drop a database it cannot prove
    abandoned.
    """
    connection = None
    try:
        connection = psycopg2.connect(_url("postgres"))
        connection.autocommit = True
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_try_advisory_lock(hashtext(%s))", (_lock_key(run_token),))
            acquired = bool(cursor.fetchone()[0])
        return not acquired
    except psycopg2.Error:
        return True
    finally:
        if connection is not None:
            connection.close()


def _psql(dbname, sql):
    env = dict(os.environ, PGPASSWORD=PG_PASS)
    return subprocess.run(
        ["psql", "-h", PG_HOST, "-p", PG_PORT, "-U", PG_USER, "-d", dbname,
         "-v", "ON_ERROR_STOP=1", "-tAc", sql],
        env=env, capture_output=True, text=True,
    )


def clone_production():
    """Create a private copy of the production mirror. Returns (dbname, url)."""
    _validate_run_token(RUN_TOKEN)
    name = f"proam_rt_{RUN_TOKEN}_{uuid.uuid4().hex[:10]}"
    r = _psql("postgres", f'CREATE DATABASE "{name}" TEMPLATE {TEMPLATE_DB};')
    if r.returncode != 0:
        raise RuntimeError(
            f"could not clone {TEMPLATE_DB}: {r.stderr.strip()}\n"
            f"Load the production dump into {TEMPLATE_DB} first."
        )
    return name, _url(name)


def drop_clone(name):
    result = _psql("postgres", f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE);')
    if result.returncode != 0:
        raise RuntimeError(
            f"could not drop disposable regression clone {name}: "
            f"{result.stderr.strip()}"
        )


def orphan_clones():
    """All clone-looking databases, including legacy untagged clones."""
    r = _psql("postgres",
              "SELECT datname FROM pg_database WHERE datname LIKE 'proam_rt_%';")
    if r.returncode != 0:
        return []
    return [line for line in r.stdout.split() if line]


def run_clones(run_token=RUN_TOKEN):
    """Return only safely named clones owned by one harness run token."""
    _validate_run_token(run_token)
    return [
        name for name in orphan_clones()
        if (match := _CLONE_NAME_RE.fullmatch(name)) and match.group(1) == run_token
    ]


def drop_run_clones(run_token=RUN_TOKEN):
    """Drop every clone owned by this session after its tests finish."""
    dropped = []
    for name in run_clones(run_token):
        drop_clone(name)
        dropped.append(name)
    return dropped


def drop_orphans():
    """Reap only tokenized clones whose owning session is no longer live.

    A pre-token clone cannot be tied to an owning process, so it is not safe to
    drop automatically. Leave it for an operator's explicit local cleanup.
    """
    dropped = []
    candidates = {}
    for name in orphan_clones():
        match = _CLONE_NAME_RE.fullmatch(name)
        if match:
            candidates.setdefault(match.group(1), []).append(name)
    for run_token, names in candidates.items():
        if run_is_active(run_token):
            continue
        for name in names:
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
