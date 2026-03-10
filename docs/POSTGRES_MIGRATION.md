# PostgreSQL Migration Guide — Missoula Pro-Am Manager

*Prepared 2026-03-09 | Target deployment: Railway (PostgreSQL 15)*

This document describes the end-to-end process for migrating the Missoula Pro-Am Manager
from SQLite (`instance/proam.db`) to PostgreSQL on Railway. Follow every step in order.

---

## 1. Prerequisites

| Requirement | Notes |
|-------------|-------|
| Python 3.10+ | Same version as production |
| `psycopg2-binary` | Already in `requirements.txt` |
| `flask-migrate` 4.x | Already in `requirements.txt`; runs Alembic under the hood |
| Railway account | Project with a PostgreSQL add-on provisioned |
| SQLite export tool | `sqlite3` CLI or Python `sqlite3` module |

---

## 2. Environment Variables

Set the following variables in Railway (Settings → Variables) before deploying:

```
DATABASE_URL=postgresql://user:password@host:5432/dbname
SECRET_KEY=<strong-random-secret>
FLASK_APP=app.py
```

Railway auto-sets `DATABASE_URL` when you attach a PostgreSQL service. The app reads
it via `config.py`:

```python
SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL', 'sqlite:///instance/proam.db')
```

If Railway supplies a `postgres://` URL (older format), prefix-fix it in `config.py`:

```python
uri = os.environ.get('DATABASE_URL', 'sqlite:///instance/proam.db')
if uri.startswith('postgres://'):
    uri = uri.replace('postgres://', 'postgresql://', 1)
SQLALCHEMY_DATABASE_URI = uri
```

---

## 3. Schema Migration (Flask-Migrate / Alembic)

The app uses Flask-Migrate exclusively for all schema changes. Never run `db.create_all()`.

### 3.1 Initialize on a fresh PostgreSQL database

```bash
# Locally, point to the Railway PostgreSQL URL
export DATABASE_URL=postgresql://...

flask db upgrade
```

`railway.toml` already includes `releaseCommand = "flask db upgrade"`, so Railway
runs this automatically on every deploy.

### 3.2 Migration chain (as of V2.3.0)

```
l9m0n1o2p3q4  (handicap_factor + mark_assignment_seconds on event_results — Phase 4A)
k8l9m0n1o2p3  (strathmark_id on competitors)
j7k8l9m0n1o2  (is_handicap on events)
i6j7k8l9m0n1  (scoring engine overhaul — run3_value, tiebreak_value, throwoff_pending, etc.)
h5i6j7k8l9m0  (schedule_config on tournaments)
... (earlier migrations)
```

Run `flask db history` to confirm the full chain against the target database.

---

## 4. Data Migration (SQLite → PostgreSQL)

After the schema is created on PostgreSQL, transfer existing data from the SQLite file.

### 4.1 Export from SQLite

```bash
python scripts/export_sqlite.py --output proam_export.json
```

The export script (`scripts/export_sqlite.py`) serializes all rows to JSON in
dependency order: Tournament → Team → CollegeCompetitor → ProCompetitor →
Event → Heat → HeatAssignment → EventResult → Flight → WoodConfig →
SchoolCaptain → ProEventRank → PayoutTemplate → User → AuditLog.

If `scripts/export_sqlite.py` does not yet exist, use the manual procedure in
Section 4.3.

### 4.2 Import into PostgreSQL

```bash
python scripts/import_postgres.py --input proam_export.json
```

The import script sets sequences after bulk insert so that auto-increment IDs do not
collide with imported IDs.

### 4.3 Manual procedure (if scripts are unavailable)

Use `pgloader` or the Python data migration script below:

```python
"""
data_migrate.py — one-shot SQLite-to-PostgreSQL data copy.

Run with both DATABASE_URL_SQLITE and DATABASE_URL_PG environment variables set.
Copies all rows table by table, preserving IDs.
"""
import os
import sqlite3
import psycopg2
import psycopg2.extras

SQLITE_PATH = os.environ.get('DATABASE_URL_SQLITE', 'instance/proam.db')
PG_URL      = os.environ['DATABASE_URL_PG']

# Tables in dependency order (parents before children)
TABLE_ORDER = [
    'users',
    'tournaments',
    'teams',
    'college_competitors',
    'pro_competitors',
    'events',
    'flights',
    'heats',
    'heat_assignments',
    'event_results',
    'wood_configs',
    'school_captains',
    'pro_event_ranks',
    'payout_templates',
    'audit_logs',
]

def migrate():
    lite = sqlite3.connect(SQLITE_PATH)
    lite.row_factory = sqlite3.Row
    pg   = psycopg2.connect(PG_URL)

    for table in TABLE_ORDER:
        rows = lite.execute(f'SELECT * FROM {table}').fetchall()
        if not rows:
            print(f'  {table}: 0 rows — skipped')
            continue
        cols = rows[0].keys()
        placeholders = ', '.join(['%s'] * len(cols))
        col_names    = ', '.join(cols)
        sql = f'INSERT INTO {table} ({col_names}) VALUES ({placeholders}) ON CONFLICT DO NOTHING'
        data = [tuple(r) for r in rows]
        with pg.cursor() as cur:
            psycopg2.extras.execute_batch(cur, sql, data, page_size=500)
        pg.commit()
        print(f'  {table}: {len(rows)} rows copied')

    # Reset sequences so future INSERTs get correct next IDs
    with pg.cursor() as cur:
        for table in TABLE_ORDER:
            cur.execute(f"""
                SELECT setval(
                    pg_get_serial_sequence('{table}', 'id'),
                    COALESCE(MAX(id), 1)
                ) FROM {table}
            """)
    pg.commit()
    lite.close()
    pg.close()
    print('Migration complete.')

if __name__ == '__main__':
    migrate()
```

---

## 5. Known Compatibility Considerations

### 5.1 JSON columns

`Heat.competitors`, `Heat.stand_assignments`, `Event.payouts`, and all other
JSON fields are stored as `TEXT` in SQLite. On PostgreSQL they remain `TEXT`
(SQLAlchemy `db.Text`). This is functional but loses PostgreSQL JSON query
operators. If JSON querying becomes necessary in future, alter those columns to
`JSONB` via a Flask-Migrate migration:

```python
# Example migration snippet (do NOT run db.create_all())
op.alter_column('heats', 'competitors', type_=postgresql.JSONB(), postgresql_using='competitors::jsonb')
```

This is an optional future improvement; not required for V2.3.0 production.

### 5.2 SQLite PRAGMA

`app.py` applies `PRAGMA foreign_keys=ON` only when the URI starts with `sqlite`.
No changes are needed for PostgreSQL — foreign key enforcement is on by default.

### 5.3 Case-sensitive LIKE

SQLite `LIKE` is case-insensitive by default; PostgreSQL `LIKE` is case-sensitive.
The app does not use raw `LIKE` queries — all filtering is via exact SQLAlchemy
`.filter_by()` or Python-level string comparisons. No changes are needed.

### 5.4 Auto-increment sequences

PostgreSQL uses `SERIAL` / `SEQUENCE` for primary keys. After a bulk data import
(Section 4), always reset sequences (see `setval` call in Section 4.3) so that
new rows receive IDs above the imported maximum.

### 5.5 `RETURNING` clause

SQLAlchemy 2.0 uses `RETURNING id` automatically on PostgreSQL for flush operations.
This is transparent — no application code changes are needed.

### 5.6 `instance/` directory

SQLite writes to `instance/proam.db`. PostgreSQL eliminates that file. The
`instance/` directory is still used for:
- `friday_feature_{tid}.json` (Friday Night Feature config)
- `saturday_priority_{tid}.json` (Saturday priority overrides)
- `strathmark_sync_cache.json` (STRATHMARK last-push timestamps)
- `strathmark_skipped.json` (skipped college competitor log)
- `backups/` (local SQLite backups — irrelevant on PostgreSQL)

On Railway, `instance/` is an ephemeral filesystem. The Friday Feature and
Saturday Priority JSON files will be lost on each deploy. These are low-stakes
config files that can be re-entered through the UI after deploy. Document this
behavior in the operator runbook.

---

## 6. Railway Deployment Checklist

- [ ] PostgreSQL add-on provisioned and `DATABASE_URL` set in Railway variables
- [ ] `SECRET_KEY` set (strong random string, not 'dev' or 'test')
- [ ] `FLASK_APP=app.py` set
- [ ] `railway.toml` `releaseCommand = "flask db upgrade"` is present
- [ ] `postgres://` → `postgresql://` prefix fix applied in `config.py` if needed
- [ ] First deploy: `flask db upgrade` runs automatically, creates all tables
- [ ] Data migration script run (if moving existing data from SQLite)
- [ ] Sequences reset after bulk import
- [ ] Login to `/auth/bootstrap` to create the first admin account
- [ ] `/health` endpoint returns `{"status": "ok", "db": true}`
- [ ] Smoke-test at least one tournament create + event setup + heat generate cycle
- [ ] STRATHMARK env vars set if live sync is desired (`STRATHMARK_API_URL`, etc.)
- [ ] Twilio env vars set if SMS notifications are desired
- [ ] `BACKUP_S3_BUCKET` + `AWS_*` env vars set if S3 backup is desired

---

## 7. Rollback Plan

If the PostgreSQL deployment fails:

1. Change `DATABASE_URL` back to `sqlite:///instance/proam.db` (or unset it).
2. Re-deploy — the app falls back to SQLite automatically.
3. No data is lost from the SQLite file because it was never modified during the
   Postgres migration attempt.

---

*End of PostgreSQL Migration Guide.*
