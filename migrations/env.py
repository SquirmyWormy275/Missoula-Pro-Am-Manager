import logging
from logging.config import fileConfig

from flask import current_app

from alembic import context
from sqlalchemy import text

# ---------------------------------------------------------------------------
# Autogenerate comparison tuning
# ---------------------------------------------------------------------------
# These hooks reduce false-positive drift detection by Alembic.  Without them,
# ``flask db migrate`` silently injects alter_column calls for nullable,
# server_default, and type changes on columns the developer did NOT touch.
# That is the #1 cause of migration bugs in this project.
# ---------------------------------------------------------------------------


def _compare_type(context, inspected_column, metadata_column, inspected_type, metadata_type):
    """Never auto-generate type changes.

    SQLite reports all types as TEXT/INTEGER/REAL regardless of what the model
    declares (VARCHAR(50), String(20), etc.).  Letting Alembic auto-detect
    type diffs produces noise that gets committed as unintended alter_column
    calls.  Type changes should always be written manually.

    Return False = "types match, skip this diff".
    """
    return False


def _compare_server_default(context, inspected_column, metadata_column, inspected_default, metadata_default):
    """Suppress server_default diffs on SQLite.

    SQLite reports defaults differently than PostgreSQL (e.g. '0' vs
    "'0'" vs "false").  Auto-detecting these diffs produces false positives
    that get committed as unintended alter_column calls.

    Return False = "defaults match, skip this diff".
    """
    # On SQLite, always suppress — too many false positives.
    if context.connection.dialect.name == 'sqlite':
        return False
    # On PostgreSQL, let Alembic compare normally.
    return None  # None = "use default comparison"

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
fileConfig(config.config_file_name)
logger = logging.getLogger('alembic.env')


def get_engine():
    try:
        # this works with Flask-SQLAlchemy<3 and Alchemical
        return current_app.extensions['migrate'].db.get_engine()
    except (TypeError, AttributeError):
        # this works with Flask-SQLAlchemy>=3
        return current_app.extensions['migrate'].db.engine


def get_engine_url():
    try:
        return get_engine().url.render_as_string(hide_password=False).replace(
            '%', '%%')
    except AttributeError:
        return str(get_engine().url).replace('%', '%%')


# add your model's MetaData object here
# for 'autogenerate' support
# from myapp import mymodel
# target_metadata = mymodel.Base.metadata
config.set_main_option('sqlalchemy.url', get_engine_url())
target_db = current_app.extensions['migrate'].db

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def get_metadata():
    if hasattr(target_db, 'metadatas'):
        return target_db.metadatas[None]
    return target_db.metadata


def run_migrations_offline():
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url, target_metadata=get_metadata(), literal_binds=True
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """

    # this callback is used to prevent an auto-migration from being generated
    # when there are no changes to the schema
    # reference: http://alembic.zzzcomputing.com/en/latest/cookbook.html
    def process_revision_directives(context, revision, directives):
        if getattr(config.cmd_opts, 'autogenerate', False):
            script = directives[0]
            if script.upgrade_ops.is_empty():
                directives[:] = []
                logger.info('No changes in schema detected.')

    conf_args = current_app.extensions['migrate'].configure_args
    if conf_args.get("process_revision_directives") is None:
        conf_args["process_revision_directives"] = process_revision_directives

    # Suppress false-positive type and server_default diffs.
    # These cause Alembic to auto-inject alter_column calls for columns the
    # developer did NOT touch — the #1 source of migration drift.
    conf_args.setdefault("compare_type", _compare_type)
    conf_args.setdefault("compare_server_default", _compare_server_default)

    connectable = get_engine()

    with connectable.connect() as connection:
        # SQLite requires FK checks to be disabled before batch_alter_table
        # operations that recreate a table (DROP + CREATE), otherwise FK
        # constraints on referencing tables block the DROP.
        if connectable.dialect.name == 'sqlite':
            connection.execute(text('PRAGMA foreign_keys=OFF'))
            connection.commit()

        context.configure(
            connection=connection,
            target_metadata=get_metadata(),
            **conf_args
        )

        with context.begin_transaction():
            context.run_migrations()

        if connectable.dialect.name == 'sqlite':
            connection.execute(text('PRAGMA foreign_keys=ON'))
            connection.commit()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
