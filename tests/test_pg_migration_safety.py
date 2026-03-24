"""
PostgreSQL Migration Safety Tests
==================================

These tests exist because of a multi-hour Railway deployment failure caused by
Alembic auto-generated migrations that contained SQLite-specific patterns.
When deployed to Railway's PostgreSQL, these patterns caused hard crashes:

- `batch_alter_table` — Alembic's SQLite workaround for ALTER TABLE limitations.
  PostgreSQL supports ALTER TABLE natively and batch_alter_table often fails or
  produces broken DDL on PG.

- `server_default='0'` / `server_default=sa.text('0')` on Boolean columns —
  PostgreSQL requires 'false'/'true', not '0'/'1'. Using integer literals causes
  "invalid input syntax for type boolean" errors.

- `PRAGMA` statements — SQLite-only commands that cause "syntax error" on PG.

- Raw SQL like `WHERE is_active = 0` — PostgreSQL Boolean columns need
  `= false` / `= true`, not integer comparisons.

These tests scan every migration file in migrations/versions/ and flag
violations BEFORE they reach production. They require no database connection.

Run with: pytest tests/test_pg_migration_safety.py -v
"""

import ast
import os
import re
from pathlib import Path

import pytest

# ── Locate migration files ──────────────────────────────────────────────────

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = PROJECT_ROOT / "migrations" / "versions"


def _migration_files():
    """Return list of (path, filename) tuples for all .py migration files."""
    if not MIGRATIONS_DIR.is_dir():
        pytest.skip(f"migrations/versions/ not found at {MIGRATIONS_DIR}")
    files = sorted(MIGRATIONS_DIR.glob("*.py"))
    if not files:
        pytest.skip("No migration files found")
    return files


def _extract_upgrade_source(filepath: Path) -> list[tuple[int, str]]:
    """Extract lines belonging to the upgrade() function body.

    Returns a list of (line_number, line_text) tuples.
    Uses AST to find the exact line range of the upgrade function,
    then reads those raw lines for string-level scanning.
    """
    source = filepath.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    # Find the upgrade() function definition
    upgrade_node = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "upgrade":
            upgrade_node = node
            break

    if upgrade_node is None:
        return []

    lines = source.splitlines()
    start = upgrade_node.lineno  # 1-indexed, this is the "def upgrade():" line
    end = upgrade_node.end_lineno if hasattr(upgrade_node, "end_lineno") and upgrade_node.end_lineno else len(lines)

    # Return body lines (skip the def line itself)
    return [(i + 1, lines[i]) for i in range(start, end) if i < len(lines)]


def _read_full_source(filepath: Path) -> list[tuple[int, str]]:
    """Return all lines as (line_number, line_text) tuples."""
    source = filepath.read_text(encoding="utf-8")
    return [(i + 1, line) for i, line in enumerate(source.splitlines())]


# ── Tests ────────────────────────────────────────────────────────────────────


class TestNoBatchAlterTableInUpgrades:
    """batch_alter_table is Alembic's SQLite compatibility shim.

    PostgreSQL does not need it, and it frequently produces broken DDL
    (especially with index operations and column alterations). All upgrade()
    functions should use direct op.add_column / op.alter_column / op.drop_column
    instead.
    """

    def test_no_batch_alter_table_in_upgrades(self):
        violations = []
        for filepath in _migration_files():
            upgrade_lines = _extract_upgrade_source(filepath)
            for lineno, line in upgrade_lines:
                if "batch_alter_table" in line:
                    violations.append(
                        f"  {filepath.name}:{lineno}  {line.strip()}"
                    )

        if violations:
            msg = (
                "batch_alter_table found in upgrade() functions.\n"
                "PostgreSQL does not need batch mode and it causes deployment failures.\n"
                "Use op.add_column / op.alter_column / op.drop_column directly.\n\n"
                "Violations:\n" + "\n".join(violations)
            )
            pytest.fail(msg)


class TestNoIntegerBooleanDefaults:
    """PostgreSQL Boolean columns need server_default='false'/'true'.

    SQLite treats Booleans as integers, so Alembic auto-generates
    server_default='0' or server_default=sa.text('0'). On PostgreSQL this
    causes: "invalid input syntax for type boolean: 0".
    """

    # Patterns that indicate integer-style boolean defaults
    BAD_PATTERNS = [
        # server_default='0' or server_default='1'
        re.compile(r"""server_default\s*=\s*['"]([01])['"]"""),
        # server_default=sa.text('0') or sa.text('1') or sa.text("'0'") etc.
        re.compile(r"""server_default\s*=\s*sa\.text\(\s*['"]'?([01])'?['"]\s*\)"""),
    ]

    def test_no_integer_boolean_defaults(self):
        violations = []
        for filepath in _migration_files():
            upgrade_lines = _extract_upgrade_source(filepath)
            for lineno, line in upgrade_lines:
                # Only flag lines that also reference BOOLEAN or Boolean
                # to avoid false positives on Integer columns with default 0
                is_boolean_context = (
                    "BOOLEAN" in line.upper()
                    or "Boolean" in line
                    # Also check surrounding lines for boolean context
                )

                for pattern in self.BAD_PATTERNS:
                    match = pattern.search(line)
                    if match and is_boolean_context:
                        violations.append(
                            f"  {filepath.name}:{lineno}  {line.strip()}"
                        )

        # Also do a broader check: any server_default='0' on a line near
        # a Boolean column (within 3 lines)
        for filepath in _migration_files():
            upgrade_lines = _extract_upgrade_source(filepath)
            for idx, (lineno, line) in enumerate(upgrade_lines):
                for pattern in self.BAD_PATTERNS:
                    if pattern.search(line) and not any(
                        v.startswith(f"  {filepath.name}:{lineno}") for v in violations
                    ):
                        # Check nearby lines for BOOLEAN
                        context_start = max(0, idx - 3)
                        context_end = min(len(upgrade_lines), idx + 4)
                        context = " ".join(
                            l for _, l in upgrade_lines[context_start:context_end]
                        )
                        if "BOOLEAN" in context.upper() or "Boolean" in context:
                            violations.append(
                                f"  {filepath.name}:{lineno}  {line.strip()}"
                            )

        if violations:
            # Deduplicate
            violations = sorted(set(violations))
            msg = (
                "Integer-style server_default on Boolean columns in upgrade().\n"
                "PostgreSQL needs server_default='false' or server_default='true',\n"
                "not '0'/'1' or sa.text('0')/sa.text('1').\n\n"
                "Violations:\n" + "\n".join(violations)
            )
            pytest.fail(msg)


class TestNoSqlitePragma:
    """PRAGMA is a SQLite-only command. It causes a syntax error on PostgreSQL."""

    def test_no_sqlite_pragma(self):
        # e9f0a1b2c3d4 intentionally uses PRAGMA behind a dialect == 'sqlite' guard
        ALLOWLIST = {'e9f0a1b2c3d4_schema_parity_fix.py'}
        violations = []
        for filepath in _migration_files():
            if filepath.name in ALLOWLIST:
                continue
            all_lines = _read_full_source(filepath)
            for lineno, line in all_lines:
                # Skip comments
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if re.search(r"\bPRAGMA\b", line, re.IGNORECASE):
                    violations.append(
                        f"  {filepath.name}:{lineno}  {line.strip()}"
                    )

        if violations:
            msg = (
                "PRAGMA statements found in migration files.\n"
                "PRAGMA is SQLite-only and will cause syntax errors on PostgreSQL.\n\n"
                "Violations:\n" + "\n".join(violations)
            )
            pytest.fail(msg)


class TestNoIntegerBooleanInRawSql:
    """Raw SQL using = 0 or = 1 for Boolean comparisons fails on PostgreSQL.

    PostgreSQL requires `= false` / `= true` for Boolean column comparisons.
    Check op.execute() calls for integer-style boolean tests.
    """

    # Match patterns like: column_name = 0, column_name = 1, = '0', = '1'
    # inside op.execute() or conn.execute() calls
    BOOL_INT_PATTERN = re.compile(
        r"""(?:op|conn|connection)\.execute\(.*"""
        r"""(?:=\s*[01]\b|=\s*'[01]')""",
        re.IGNORECASE,
    )

    # Known boolean column names in the schema
    BOOLEAN_COLUMNS = {
        "is_open", "is_partnered", "requires_dual_runs", "is_finalized",
        "requires_triple_runs", "throwoff_pending", "is_flagged",
        "is_ala_member", "pro_am_lottery_opt_in", "is_left_handed_springboard",
        "springboard_slow_heat", "payout_settled", "waiver_accepted",
        "providing_shirts", "is_handicap",
    }

    def test_no_integer_boolean_in_raw_sql(self):
        violations = []
        for filepath in _migration_files():
            upgrade_lines = _extract_upgrade_source(filepath)
            for lineno, line in upgrade_lines:
                # Look for execute() calls with integer boolean comparisons
                if "execute" not in line.lower():
                    continue

                # Check if any known boolean column is compared with 0 or 1
                for col in self.BOOLEAN_COLUMNS:
                    pattern = re.compile(
                        rf"""\b{col}\b\s*=\s*['"]?[01]['"]?""",
                        re.IGNORECASE,
                    )
                    if pattern.search(line):
                        violations.append(
                            f"  {filepath.name}:{lineno}  {line.strip()}"
                        )
                        break  # One violation per line is enough

        if violations:
            msg = (
                "Raw SQL in upgrade() uses integer comparison on Boolean columns.\n"
                "PostgreSQL requires `= false` / `= true`, not `= 0` / `= 1`.\n\n"
                "Violations:\n" + "\n".join(violations)
            )
            pytest.fail(msg)


class TestAllMigrationsHaveDownRevision:
    """Every migration must declare a down_revision for Alembic chain integrity.

    A missing or None down_revision (except for the initial migration) breaks
    the migration chain and makes `flask db upgrade` fail on fresh databases.
    """

    def test_all_migrations_have_down_revision(self):
        none_count = 0
        missing = []

        for filepath in _migration_files():
            source = filepath.read_text(encoding="utf-8")
            try:
                tree = ast.parse(source)
            except SyntaxError:
                missing.append(f"  {filepath.name}: SyntaxError — cannot parse")
                continue

            found_down_revision = False
            down_revision_value = None

            for node in ast.walk(tree):
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name) and target.id == "down_revision":
                            found_down_revision = True
                            # Check if value is None
                            if isinstance(node.value, ast.Constant) and node.value.value is None:
                                none_count += 1
                                down_revision_value = None
                            else:
                                down_revision_value = "set"

            if not found_down_revision:
                missing.append(
                    f"  {filepath.name}: no down_revision variable declared"
                )

        # Exactly one migration should have down_revision = None (the initial one)
        if none_count > 1:
            missing.append(
                f"  {none_count} migrations have down_revision = None "
                f"(expected exactly 1 — the initial migration)"
            )

        if missing:
            msg = (
                "Migration chain integrity issues:\n" + "\n".join(missing)
            )
            pytest.fail(msg)


class TestNoRenderAsInUpgrades:
    """render_as_batch=True in migration context is another batch mode indicator.

    Some Alembic environments set render_as_batch=True in env.py, which causes
    all auto-generated migrations to use batch_alter_table. This test catches
    the pattern in individual migration files.
    """

    def test_no_render_as_batch_in_upgrades(self):
        violations = []
        for filepath in _migration_files():
            upgrade_lines = _extract_upgrade_source(filepath)
            for lineno, line in upgrade_lines:
                if "render_as_batch" in line:
                    violations.append(
                        f"  {filepath.name}:{lineno}  {line.strip()}"
                    )

        if violations:
            msg = (
                "render_as_batch found in upgrade() functions.\n"
                "This triggers SQLite batch mode which fails on PostgreSQL.\n\n"
                "Violations:\n" + "\n".join(violations)
            )
            pytest.fail(msg)
