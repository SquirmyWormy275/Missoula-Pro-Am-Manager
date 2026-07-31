#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# rig_bootstrap.sh (D11-C, c42): stand up the full parity rig from nothing.
#
# Inputs: this repo + the pristine dump file (proam_2026pristine.dump).
# The dump contains real competitor PII (names, phone numbers) and is NOT
# committed to this public repo; it travels via the operator's inbox or any
# private storage the operator controls. Pass its path as $1.
#
#   bash scripts/rig_bootstrap.sh /path/to/proam_2026pristine.dump
#
# Idempotent-ish: refuses to overwrite existing template databases unless
# RIG_FORCE=1. Requires: PostgreSQL running locally, python3.10+, and either
# superuser psql access or an existing 'proam' role with CREATEDB.
#
# What it builds (the four lanes in proam_regression/RUNBOOK.md):
#   proam_prod_mirror_2026pristine  restored from the dump (pre-reseed archive)
#   proam_prod_mirror_p0            pristine + the c38 college id reseed
#   proam_prod_mirror_p0rev         p0 with physical row order reversed
#   proam_prod_mirror_mt            p0 + staged 2027 oracle tournament
# ---------------------------------------------------------------------------
set -euo pipefail

DUMP="${1:?usage: rig_bootstrap.sh <path to proam_2026pristine.dump>}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PGHOST="${PGHOST:-localhost}"
export PGPASSWORD="${PGPASSWORD:-proam}"
PSQL="psql -h $PGHOST -U proam"
PY="${RIG_PYTHON:-python3}"

say() { printf '\n== %s ==\n' "$*"; }

say "role + prerequisites"
if ! $PSQL -d postgres -tAc "SELECT 1" >/dev/null 2>&1; then
  echo "role 'proam' cannot connect; creating it (needs local superuser)..."
  sudo -u postgres psql -tAc "CREATE ROLE proam LOGIN PASSWORD 'proam' CREATEDB" \
    || psql -U postgres -tAc "CREATE ROLE proam LOGIN PASSWORD 'proam' CREATEDB"
fi

exists() { $PSQL -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='$1'" | grep -q 1; }
guard() {
  if exists "$1"; then
    if [ "${RIG_FORCE:-0}" = "1" ]; then dropdb -h "$PGHOST" -U proam "$1";
    else echo "$1 exists; set RIG_FORCE=1 to rebuild"; return 1; fi
  fi
}

say "1/4 pristine archive (restore $DUMP)"
if guard proam_prod_mirror_2026pristine; then
  createdb -h "$PGHOST" -U proam -O proam proam_prod_mirror_2026pristine
  pg_restore -h "$PGHOST" -U proam -d proam_prod_mirror_2026pristine "$DUMP"
fi
$PSQL -d proam_prod_mirror_2026pristine -tAc \
  "SELECT 'pristine: '||count(*)||' heats' FROM heats"

say "2/4 p0 = pristine + c38 college id reseed"
if guard proam_prod_mirror_p0; then
  createdb -h "$PGHOST" -U proam -T proam_prod_mirror_2026pristine -O proam proam_prod_mirror_p0
  DATABASE_URL="postgresql://proam:proam@$PGHOST:5432/proam_prod_mirror_p0" \
    "$PY" "$REPO/scripts/reseed_college_ids.py" --apply
fi

say "3/4 p0rev = p0 with physical row order reversed (CLUSTER on DESC pk)"
if guard proam_prod_mirror_p0rev; then
  createdb -h "$PGHOST" -U proam -T proam_prod_mirror_p0 -O proam proam_prod_mirror_p0rev
  $PSQL -d proam_prod_mirror_p0rev -tA <<'SQL' | while IFS='|' read -r t pk; do
SELECT c.relname, (SELECT string_agg(a.attname, ',' ORDER BY x.n)
  FROM unnest(i.indkey) WITH ORDINALITY AS x(attnum,n)
  JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=x.attnum)
FROM pg_class c
JOIN pg_namespace n ON n.oid=c.relnamespace
LEFT JOIN pg_index i ON i.indrelid=c.oid AND i.indisprimary
WHERE n.nspname='public' AND c.relkind='r' AND c.relname <> 'alembic_version';
SQL
    [ -z "$pk" ] && continue
    cols=$(echo "$pk" | sed 's/\([^,][^,]*\)/\1 DESC/g')
    $PSQL -d proam_prod_mirror_p0rev -q -c "CREATE INDEX _rev_idx_$t ON $t ($cols);" \
      -c "CLUSTER $t USING _rev_idx_$t;" -c "DROP INDEX _rev_idx_$t;"
  done
fi

say "4/4 mt = p0 + staged 2027 oracle"
if guard proam_prod_mirror_mt; then
  createdb -h "$PGHOST" -U proam -T proam_prod_mirror_p0 -O proam proam_prod_mirror_mt
  PYTHONPATH="$REPO" PROAM_MT_URL="postgresql://proam:proam@$PGHOST:5432/proam_prod_mirror_mt" \
    "$PY" "$REPO/proam_regression/stage_multitournament.py"
fi

say "done. run the suite:"
echo "  PROAM_APP_ROOT=$REPO PROAM_RIG_TEMPLATE=proam_prod_mirror_p0 \\"
echo "  SECRET_KEY=\$(python3 -c 'print(\"x\"*64)') $PY -m pytest proam_regression -p no:randomly -q"
