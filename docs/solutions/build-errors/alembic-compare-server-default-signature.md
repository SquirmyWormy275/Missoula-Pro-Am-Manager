---
module: migrations
date: 2026-04-15
problem_type: build_error
component: tooling
severity: high
root_cause: wrong_api
resolution_type: code_fix
symptoms:
  - "TypeError: _compare_server_default() takes 5 positional arguments but 6 were given"
  - "flask db migrate crashes before writing any file"
tags:
  - "alembic"
  - "flask-migrate"
  - "migrations"
---

# Alembic `_compare_server_default` hook broke on Alembic >=1.5

## Problem
`flask db migrate` crashed with `TypeError: _compare_server_default() takes 5 positional arguments but 6 were given`. Every developer had to hand-write migrations, bypassing autogeneration entirely for months.

## Root Cause
Modern Alembic (>=1.5) passes SIX positional arguments to user-defined `compare_server_default` callbacks:

```
context, inspected_column, metadata_column,
inspected_default, metadata_default, rendered_metadata_default
```

The custom hook in `migrations/env.py` was pinned to the old 5-arg signature.

## Solution
Update the signature in `migrations/env.py`:

```python
def _compare_server_default(
    context, inspected_column, metadata_column,
    inspected_default, metadata_default, rendered_metadata_default
):
    # body unchanged — same SQLite-vs-PostgreSQL suppression rule
    ...
```

Or accept `*args, **kwargs` if forward-compatibility with future Alembic versions matters.

## Prevention
- When pinning/upgrading Alembic or Flask-Migrate, run `flask db migrate --dry-run` (or create a throwaway column on a model and attempt a migrate) to exercise the hook path.
- Custom `env.py` hooks are fragile — prefer `*args, **kwargs` tails on any user-defined Alembic callback.
