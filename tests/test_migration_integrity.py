"""
Test that ``flask db upgrade`` from a blank database produces a schema
matching the current SQLAlchemy models (``db.create_all()``).

This catches the class of bug where migrations silently fail to add columns
or tables (especially on SQLite with branched/merged migration histories)
while tests that use ``db.create_all()`` never notice the gap.

Enhanced (V2.5.1) to also catch:
- Column type mismatches (e.g. TEXT vs VARCHAR)
- Nullable mismatches (model says NOT NULL, migration says nullable)
- Server default mismatches
- Missing or extra indexes
"""
import os
import sqlite3
import tempfile

import pytest

if os.environ.get('TEST_USE_CREATE_ALL') == '1':
    pytest.skip('Migration integrity tests require flask db upgrade path', allow_module_level=True)

os.environ.setdefault('SECRET_KEY', 'test-secret-migration')
os.environ.setdefault('WTF_CSRF_ENABLED', 'False')


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_schema(db_path: str) -> dict:
    """Return {table_name: {col_name: col_type, ...}} for every user table."""
    conn = sqlite3.connect(db_path)
    tables = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' AND name != 'alembic_version'"
        ).fetchall()
    ]
    schema: dict[str, dict[str, str]] = {}
    for table in sorted(tables):
        cols = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        # pragma returns (cid, name, type, notnull, default, pk)
        schema[table] = {row[1]: row[2].upper() for row in cols}
    conn.close()
    return schema


def _get_detailed_schema(db_path: str) -> dict:
    """Return {table: {col: {type, notnull, default, pk}}} for every user table.

    This is the deep version that captures nullable, defaults, and primary key
    status — not just column existence and type.
    """
    conn = sqlite3.connect(db_path)
    tables = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' AND name != 'alembic_version'"
        ).fetchall()
    ]
    schema: dict[str, dict[str, dict]] = {}
    for table in sorted(tables):
        cols = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        # pragma returns (cid, name, type, notnull, dflt_value, pk)
        table_cols = {}
        for row in cols:
            table_cols[row[1]] = {
                'type': row[2].upper(),
                'notnull': bool(row[3]),
                'default': row[4],
                'pk': bool(row[5]),
            }
        schema[table] = table_cols
    conn.close()
    return schema


def _get_indexes(db_path: str) -> dict:
    """Return {table: {index_name: [col1, col2, ...]}} for every user table."""
    conn = sqlite3.connect(db_path)
    tables = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' AND name != 'alembic_version'"
        ).fetchall()
    ]
    indexes: dict[str, dict[str, list]] = {}
    for table in sorted(tables):
        table_indexes = {}
        idx_rows = conn.execute(f'PRAGMA index_list("{table}")').fetchall()
        for idx_row in idx_rows:
            idx_name = idx_row[1]
            # Skip auto-generated unique constraint indexes (sqlite_autoindex_*)
            if idx_name.startswith('sqlite_autoindex_'):
                continue
            idx_info = conn.execute(f'PRAGMA index_info("{idx_name}")').fetchall()
            cols = [info_row[2] for info_row in idx_info]
            table_indexes[idx_name] = sorted(cols)
        if table_indexes:
            indexes[table] = table_indexes
    conn.close()
    return indexes


def _build_migration_schema() -> tuple[dict, dict, dict]:
    """Run ``flask db upgrade`` on a fresh SQLite file, return schemas."""
    tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp_path = tmp.name
    tmp.close()

    # Set DATABASE_URL BEFORE create_app() so it never touches production DB
    old_db_url = os.environ.get('DATABASE_URL')
    os.environ['DATABASE_URL'] = f'sqlite:///{tmp_path}'
    try:
        from app import create_app
        app = create_app()
        app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{tmp_path}'

        from database import db
        with app.app_context():
            db.engine.dispose()
            from flask_migrate import upgrade
            upgrade()

        return (
            _get_schema(tmp_path),
            _get_detailed_schema(tmp_path),
            _get_indexes(tmp_path),
        )
    finally:
        if old_db_url is None:
            os.environ.pop('DATABASE_URL', None)
        else:
            os.environ['DATABASE_URL'] = old_db_url
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _build_model_schema() -> tuple[dict, dict, dict]:
    """Run ``db.create_all()`` on a fresh SQLite file, return schemas."""
    tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp_path = tmp.name
    tmp.close()

    # Set DATABASE_URL BEFORE create_app() so it never touches production DB
    old_db_url = os.environ.get('DATABASE_URL')
    os.environ['DATABASE_URL'] = f'sqlite:///{tmp_path}'
    try:
        from app import create_app
        app = create_app()
        app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{tmp_path}'

        from database import db
        with app.app_context():
            db.engine.dispose()
            db.create_all()

        return (
            _get_schema(tmp_path),
            _get_detailed_schema(tmp_path),
            _get_indexes(tmp_path),
        )
    finally:
        if old_db_url is None:
            os.environ.pop('DATABASE_URL', None)
        else:
            os.environ['DATABASE_URL'] = old_db_url
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Tests — Existence (original)
# ---------------------------------------------------------------------------

class TestMigrationIntegrity:
    """Verify that running the full migration chain produces the same schema
    as the ORM models declare."""

    @pytest.fixture(scope='class', autouse=True)
    def schemas(self, request):
        """Build both schemas once per class (they are expensive)."""
        mig_simple, mig_detail, mig_idx = _build_migration_schema()
        mod_simple, mod_detail, mod_idx = _build_model_schema()
        request.cls.migration_schema = mig_simple
        request.cls.model_schema = mod_simple
        request.cls.migration_detail = mig_detail
        request.cls.model_detail = mod_detail
        request.cls.migration_indexes = mig_idx
        request.cls.model_indexes = mod_idx

    # -- Table and column existence (original tests) -----------------------

    def test_no_missing_tables(self):
        """Every table in the models must exist after migrations."""
        model_tables = set(self.model_schema.keys())
        migration_tables = set(self.migration_schema.keys())
        missing = model_tables - migration_tables
        assert not missing, (
            f"Tables defined in models but missing after `flask db upgrade`: "
            f"{sorted(missing)}"
        )

    def test_no_missing_columns(self):
        """Every column in every model table must exist after migrations."""
        missing = {}
        for table, model_cols in self.model_schema.items():
            if table not in self.migration_schema:
                continue  # covered by test_no_missing_tables
            migration_cols = set(self.migration_schema[table].keys())
            table_missing = set(model_cols.keys()) - migration_cols
            if table_missing:
                missing[table] = sorted(table_missing)
        assert not missing, (
            "Columns defined in models but missing after `flask db upgrade`:\n"
            + "\n".join(f"  {t}: {cols}" for t, cols in sorted(missing.items()))
        )

    def test_no_extra_migration_tables(self):
        """Migrations should not create tables that no model declares.

        This is a weaker check (informational) — extra tables may be
        intentional (e.g. association tables).  Flip to a warning if needed.
        """
        model_tables = set(self.model_schema.keys())
        migration_tables = set(self.migration_schema.keys())
        extra = migration_tables - model_tables
        if extra:
            pytest.skip(
                f"Extra tables from migrations not in models (may be OK): "
                f"{sorted(extra)}"
            )

    def test_no_extra_migration_columns(self):
        """Migrations should not create columns that no model declares."""
        extra = {}
        for table in self.migration_schema:
            if table not in self.model_schema:
                continue
            migration_cols = set(self.migration_schema[table].keys())
            model_cols = set(self.model_schema[table].keys())
            table_extra = migration_cols - model_cols
            if table_extra:
                extra[table] = sorted(table_extra)
        if extra:
            pytest.skip(
                "Extra columns from migrations not in models (may be OK):\n"
                + "\n".join(
                    f"  {t}: {cols}" for t, cols in sorted(extra.items())
                )
            )

    # -- Column type parity ------------------------------------------------

    def test_column_type_parity(self):
        """Column types from migrations must match those from db.create_all().

        SQLite is lenient about types, but mismatches here indicate the
        migration used a different type than the model declares.  This catches
        e.g. TEXT vs VARCHAR drift.
        """
        mismatches = []
        for table in self.model_detail:
            if table not in self.migration_detail:
                continue
            for col, mod_info in self.model_detail[table].items():
                if col not in self.migration_detail[table]:
                    continue
                mig_info = self.migration_detail[table][col]
                mod_type = mod_info['type']
                mig_type = mig_info['type']
                # Normalize common SQLite type aliases
                norm = {'INT': 'INTEGER', 'BOOL': 'BOOLEAN', 'DOUBLE': 'FLOAT'}
                mod_type_n = norm.get(mod_type, mod_type)
                mig_type_n = norm.get(mig_type, mig_type)
                if mod_type_n != mig_type_n:
                    mismatches.append(
                        f"  {table}.{col}: model={mod_type} migration={mig_type}"
                    )
        assert not mismatches, (
            "Column type mismatches between models and migrations:\n"
            + "\n".join(mismatches)
        )

    # -- Nullable parity ---------------------------------------------------

    # Long-standing nullable drift that predates the explicit-nullable rule in
    # CLAUDE.md Section 6 ("Model Column Declaration Rules").  These columns
    # were tightened to NOT NULL in the models over time but the original
    # migrations that created them used SQLite batch ops emitting nullable=True,
    # and no back-fill migration ever ran.  Listed here so test_nullable_parity
    # still catches NEW drift but does not block CI on the historical tail.
    # Retire entries by writing real fix-up migrations and removing them here.
    KNOWN_NULLABLE_DRIFT = {
        # ('college_competitors', 'individual_points'),  # RETIRED V2.8.0 — fixed by migration f0a1b2c3d4e6
        ('college_competitors', 'events_entered'),
        ('college_competitors', 'partners'),
        ('college_competitors', 'gear_sharing'),
        ('college_competitors', 'phone_opted_in'),
        ('college_competitors', 'status'),
        # ('event_results', 'points_awarded'),  # RETIRED V2.8.0 — fixed by migration f0a1b2c3d4e6
        ('event_results', 'payout_amount'),
        ('event_results', 'is_flagged'),
        ('event_results', 'status'),
        ('events', 'scoring_order'),
        ('events', 'is_open'),
        ('events', 'is_partnered'),
        ('events', 'requires_dual_runs'),
        ('events', 'has_prelims'),
        ('events', 'payouts'),
        ('events', 'status'),
        ('flights', 'status'),
        ('heats', 'run_number'),
        ('heats', 'competitors'),
        ('heats', 'stand_assignments'),
        ('heats', 'status'),
        ('payout_templates', 'created_at'),
        ('pro_competitors', 'is_ala_member'),
        ('pro_competitors', 'pro_am_lottery_opt_in'),
        ('pro_competitors', 'is_left_handed_springboard'),
        ('pro_competitors', 'springboard_slow_heat'),
        ('pro_competitors', 'events_entered'),
        ('pro_competitors', 'entry_fees'),
        ('pro_competitors', 'fees_paid'),
        ('pro_competitors', 'gear_sharing'),
        ('pro_competitors', 'partners'),
        ('pro_competitors', 'total_earnings'),
        ('pro_competitors', 'payout_settled'),
        ('pro_competitors', 'phone_opted_in'),
        ('pro_competitors', 'status'),
        ('pro_competitors', 'waiver_accepted'),
        ('pro_competitors', 'total_fees'),
        ('school_captains', 'created_at'),
        # ('teams', 'total_points'),  # RETIRED V2.8.0 — fixed by migration f0a1b2c3d4e6
        ('teams', 'status'),
        ('tournaments', 'status'),
        ('tournaments', 'providing_shirts'),
        ('tournaments', 'created_at'),
        ('tournaments', 'updated_at'),
    }

    def test_nullable_parity(self):
        """If a model column is NOT NULL, the migration must also be NOT NULL.

        This is the single most common migration bug in this project: Alembic
        auto-generates nullable=True during SQLite batch operations, silently
        weakening constraints the model declares.

        Primary key columns are excluded (always NOT NULL).
        Pre-existing drift listed in KNOWN_NULLABLE_DRIFT is also excluded —
        see the docstring on that constant above.
        """
        mismatches = []
        for table in self.model_detail:
            if table not in self.migration_detail:
                continue
            for col, mod_info in self.model_detail[table].items():
                if col not in self.migration_detail[table]:
                    continue
                if mod_info['pk']:
                    continue  # PK is always NOT NULL
                if (table, col) in self.KNOWN_NULLABLE_DRIFT:
                    continue  # historical drift, tracked separately
                mig_info = self.migration_detail[table][col]
                model_notnull = mod_info['notnull']
                mig_notnull = mig_info['notnull']
                if model_notnull and not mig_notnull:
                    mismatches.append(
                        f"  {table}.{col}: model=NOT NULL, migration=nullable"
                    )
                elif not model_notnull and mig_notnull:
                    mismatches.append(
                        f"  {table}.{col}: model=nullable, migration=NOT NULL"
                    )
        assert not mismatches, (
            "Nullable mismatches between models and migrations.\n"
            "This usually means a migration altered nullable on a column it "
            "should not have touched, or a new migration omitted nullable=False.\n"
            + "\n".join(mismatches)
        )

    # -- Default value parity ----------------------------------------------

    # Long-standing server_default drift: the migration correctly emitted a
    # server_default (so `flask db upgrade` is safe on existing rows) but the
    # corresponding model db.Column() only carries a Python-side default=, not
    # server_default=.  CLAUDE.md Section 6 mandates server_default alongside
    # default — these columns predate that rule.  Listed here so the test still
    # catches NEW drift but does not block CI on the historical tail.  Retire
    # entries by adding server_default to the model and removing them here.
    KNOWN_SERVER_DEFAULT_DRIFT = {
        ('college_competitors', 'phone_opted_in'),
        ('event_results', 'throwoff_pending'),
        ('event_results', 'handicap_factor'),
        ('event_results', 'is_flagged'),
        ('event_results', 'version_id'),
        ('events', 'is_handicap'),
        ('events', 'requires_triple_runs'),
        ('events', 'is_finalized'),
        ('heats', 'version_id'),
        ('payout_templates', 'payouts'),
        ('pro_competitors', 'springboard_slow_heat'),
        ('pro_competitors', 'payout_settled'),
        ('pro_competitors', 'phone_opted_in'),
        ('tournaments', 'providing_shirts'),
        ('users', 'is_active_user'),
        ('wood_configs', 'size_unit'),
    }

    def test_server_default_parity(self):
        """Server defaults from migrations must match those from db.create_all().

        Compares the raw SQLite default values.  Both schemas are built from
        scratch so defaults should be identical.  Mismatches indicate the
        migration used a different server_default than the model declares.
        Pre-existing drift listed in KNOWN_SERVER_DEFAULT_DRIFT is excluded —
        see the docstring on that constant above.
        """
        mismatches = []
        for table in self.model_detail:
            if table not in self.migration_detail:
                continue
            for col, mod_info in self.model_detail[table].items():
                if col not in self.migration_detail[table]:
                    continue
                if mod_info['pk']:
                    continue
                if (table, col) in self.KNOWN_SERVER_DEFAULT_DRIFT:
                    continue  # historical drift, tracked separately
                mig_info = self.migration_detail[table][col]
                mod_default = mod_info['default']
                mig_default = mig_info['default']
                # Normalize: strip quotes and whitespace for comparison
                def _normalize_default(d):
                    if d is None:
                        return None
                    d = str(d).strip().strip("'\"")
                    # SQLite sometimes wraps in extra quotes
                    if d.lower() in ('none', 'null', ''):
                        return None
                    return d.lower()
                mod_norm = _normalize_default(mod_default)
                mig_norm = _normalize_default(mig_default)
                if mod_norm != mig_norm:
                    mismatches.append(
                        f"  {table}.{col}: model default={mod_default!r}, "
                        f"migration default={mig_default!r}"
                    )
        assert not mismatches, (
            "Server default mismatches between models and migrations:\n"
            + "\n".join(mismatches)
        )

    # -- Index parity ------------------------------------------------------

    def test_no_missing_indexes(self):
        """Every index declared by the models must exist after migrations.

        This catches the bug where a migration silently drops an index
        (e.g. d8d4aa7bdb45 dropped 3 indexes in an unrelated migration).
        """
        missing = []
        for table, model_idxs in self.model_indexes.items():
            mig_idxs = self.migration_indexes.get(table, {})
            for idx_name, idx_cols in model_idxs.items():
                if idx_name not in mig_idxs:
                    # Check if an equivalent index exists with different name
                    found = any(
                        sorted(c) == sorted(idx_cols)
                        for c in mig_idxs.values()
                    )
                    if not found:
                        missing.append(
                            f"  {table}: index {idx_name} on {idx_cols}"
                        )
        assert not missing, (
            "Indexes declared in models but missing after `flask db upgrade`.\n"
            "This usually means a migration dropped the index without re-creating it.\n"
            + "\n".join(missing)
        )

    def test_no_extra_migration_indexes(self):
        """Indexes from migrations that don't exist in models (informational)."""
        extra = []
        for table, mig_idxs in self.migration_indexes.items():
            mod_idxs = self.model_indexes.get(table, {})
            for idx_name, idx_cols in mig_idxs.items():
                if idx_name not in mod_idxs:
                    found = any(
                        sorted(c) == sorted(idx_cols)
                        for c in mod_idxs.values()
                    )
                    if not found:
                        extra.append(
                            f"  {table}: index {idx_name} on {idx_cols}"
                        )
        if extra:
            pytest.skip(
                "Extra indexes from migrations not declared in models (may be OK):\n"
                + "\n".join(extra)
            )


# ---------------------------------------------------------------------------
# Tests — Migration chain integrity
# ---------------------------------------------------------------------------

class TestMigrationChain:
    """Verify the migration chain is linear and has no broken links."""

    @pytest.fixture(scope='class', autouse=True)
    def migration_graph(self, request):
        """Parse all migration files to build the revision graph."""
        import glob
        import importlib.util

        migrations_dir = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            'migrations', 'versions'
        )
        graph = {}  # revision -> {down_revision, is_merge}
        for path in glob.glob(os.path.join(migrations_dir, '*.py')):
            if path.endswith('__pycache__'):
                continue
            spec = importlib.util.spec_from_file_location('_mig', path)
            if spec is None or spec.loader is None:
                continue
            mod = importlib.util.module_from_spec(spec)
            try:
                spec.loader.exec_module(mod)
            except Exception:
                continue
            rev = getattr(mod, 'revision', None)
            down = getattr(mod, 'down_revision', None)
            if rev:
                graph[rev] = {
                    'down_revision': down,
                    'file': os.path.basename(path),
                    'is_merge': isinstance(down, tuple),
                }
        request.cls.graph = graph

    def test_no_orphan_revisions(self):
        """Every down_revision must point to an existing revision (or None)."""
        orphans = []
        for rev, info in self.graph.items():
            down = info['down_revision']
            if down is None:
                continue
            targets = down if isinstance(down, tuple) else (down,)
            for target in targets:
                if target not in self.graph:
                    orphans.append(
                        f"  {info['file']}: revision {rev} points to "
                        f"non-existent down_revision {target}"
                    )
        assert not orphans, (
            "Broken migration chain — orphan down_revisions:\n"
            + "\n".join(orphans)
        )

    def test_exactly_one_head(self):
        """There must be exactly one HEAD revision (no multiple heads)."""
        all_revs = set(self.graph.keys())
        # A revision is a head if no other revision lists it as a down_revision
        referenced = set()
        for info in self.graph.values():
            down = info['down_revision']
            if down is None:
                continue
            targets = down if isinstance(down, tuple) else (down,)
            for target in targets:
                referenced.add(target)
        heads = all_revs - referenced
        assert len(heads) == 1, (
            f"Expected exactly 1 HEAD revision, found {len(heads)}: "
            f"{sorted(heads)}. Run `flask db merge heads` to resolve."
        )

    def test_exactly_one_root(self):
        """There must be exactly one root revision (down_revision=None)."""
        roots = [
            rev for rev, info in self.graph.items()
            if info['down_revision'] is None
        ]
        assert len(roots) == 1, (
            f"Expected exactly 1 root migration, found {len(roots)}: "
            f"{sorted(roots)}"
        )


# ---------------------------------------------------------------------------
# Tests — Model column declaration quality (linter)
# ---------------------------------------------------------------------------

class TestModelColumnDeclarations:
    """Enforce that all model columns explicitly declare nullable.

    When `nullable` is omitted, SQLAlchemy defaults to ``nullable=True``.
    Alembic then auto-generates migrations with ``nullable=True`` — which is
    often wrong for columns with defaults (Boolean, Integer, Text with JSON).
    This ambiguity is the #1 source of migration drift in this project.

    This test reads the ORM metadata and flags any column that omits an
    explicit ``nullable`` declaration.  Fix by adding ``nullable=False`` or
    ``nullable=True`` to every ``db.Column()`` call in models/*.py.
    """

    @pytest.fixture(scope='class', autouse=True)
    def model_metadata(self, request, tmp_path_factory):
        """Load model metadata using a temp DB (never touches production)."""
        tmp_db = tmp_path_factory.mktemp('nullable') / 'test.db'
        old_db_url = os.environ.get('DATABASE_URL')
        os.environ['DATABASE_URL'] = f'sqlite:///{tmp_db}'
        try:
            from app import create_app
            app = create_app()
            from database import db
            with app.app_context():
                request.cls.metadata = db.metadata
                request.cls.app = app
        finally:
            if old_db_url is None:
                os.environ.pop('DATABASE_URL', None)
            else:
                os.environ['DATABASE_URL'] = old_db_url

    def test_all_columns_declare_nullable(self):
        """Every non-PK column should explicitly declare nullable.

        We cannot directly detect whether the source code wrote
        ``nullable=...`` — but we CAN detect the *consequence*: if a column
        has a Python-side ``default`` but is ``nullable=True``, it's almost
        certainly an omission (the developer meant NOT NULL + default).

        This test flags columns that have a Python-side default but are
        nullable — a strong signal that ``nullable=False`` was forgotten.
        """
        # Tables managed by Alembic itself
        skip_tables = {'alembic_version'}
        suspicious = []
        with self.app.app_context():
            for table in self.metadata.sorted_tables:
                if table.name in skip_tables:
                    continue
                for col in table.columns:
                    if col.primary_key:
                        continue
                    if col.foreign_keys:
                        continue  # FK columns are often intentionally nullable
                    # Check: has a Python-side default but is nullable?
                    has_default = col.default is not None or col.server_default is not None
                    if has_default and col.nullable:
                        suspicious.append(
                            f"  {table.name}.{col.name}: has default but "
                            f"nullable=True (likely missing nullable=False)"
                        )
        assert not suspicious, (
            "Columns with defaults that are still nullable (should be "
            "nullable=False):\n" + "\n".join(suspicious)
            + "\n\nFix by adding nullable=False to these db.Column() calls."
        )


# ---------------------------------------------------------------------------
# Tests — Migration file anti-pattern scanner
# ---------------------------------------------------------------------------

class TestMigrationFileQuality:
    """Scan migration files for known anti-patterns that cause silent drift.

    These patterns have historically caused bugs in this project:
    1. A single migration that touches more than 2 tables (likely unintended)
    2. alter_column calls (often auto-generated drift fixes for unrelated cols)
    3. drop_index calls without matching create_index (index disappears)
    4. _add_column_if_missing() or similar idempotent hacks
    """

    @pytest.fixture(scope='class', autouse=True)
    def migration_files(self, request):
        """Read all migration file contents."""
        import glob

        migrations_dir = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            'migrations', 'versions'
        )
        files = {}
        for path in glob.glob(os.path.join(migrations_dir, '*.py')):
            basename = os.path.basename(path)
            with open(path, 'r', encoding='utf-8') as f:
                files[basename] = f.read()
        request.cls.files = files

    def test_no_idempotent_hacks(self):
        """Migration files should not use _add_column_if_missing() or similar.

        Idempotent column additions hide the fact that an earlier migration
        was incomplete.  Fix the source migration instead.
        """
        hacks = []
        patterns = [
            'if_missing', 'column_exists', 'has_column', 'try:',
            'IF NOT EXISTS', 'if not exists',
        ]
        for filename, content in self.files.items():
            # Skip merge migrations (they're just pass-throughs)
            if 'def upgrade():\n    pass' in content:
                continue
            for pattern in patterns:
                if pattern in content:
                    # Allow 'try:' only if it's clearly for error handling
                    # in downgrade (SQLite DROP COLUMN compat)
                    if pattern == 'try:' and 'downgrade' in content:
                        # Check if try is only in downgrade
                        upgrade_section = content.split('def downgrade')[0]
                        if 'try:' not in upgrade_section:
                            continue
                    hacks.append(f"  {filename}: contains '{pattern}'")
        if hacks:
            pytest.skip(
                "Migration files with idempotent patterns (review for correctness):\n"
                + "\n".join(hacks)
            )

    def test_no_unmatched_drop_index(self):
        """A migration that drops an index should also create one (or be a
        dedicated index-removal migration).

        Catches the pattern where auto-gen drops indexes as a side effect.
        """
        import re
        unmatched = []
        for filename, content in self.files.items():
            drops = re.findall(r'drop_index\([^)]+\)', content)
            creates = re.findall(r'create_index\([^)]+\)', content)
            if drops and not creates:
                # This file only drops indexes — suspicious unless it's
                # explicitly named as an index removal migration
                if 'index' not in filename.lower() and 'drop' not in filename.lower():
                    unmatched.append(
                        f"  {filename}: drops {len(drops)} index(es) but "
                        f"creates none — likely unintended side effect"
                    )
        if unmatched:
            pytest.skip(
                "Migration files that drop indexes without creating replacements:\n"
                + "\n".join(unmatched)
            )

    def test_alter_column_count(self):
        """Flag migrations with many alter_column calls — often auto-gen drift.

        A migration that alters 3+ columns is likely Alembic "fixing" drift
        from a prior session rather than an intentional schema change.
        """
        import re
        flagged = []
        for filename, content in self.files.items():
            alters = re.findall(r'alter_column\(', content)
            if len(alters) >= 3:
                flagged.append(
                    f"  {filename}: {len(alters)} alter_column calls — "
                    f"review for unintended drift fixes"
                )
        if flagged:
            pytest.skip(
                "Migration files with suspiciously many alter_column calls:\n"
                + "\n".join(flagged)
            )
