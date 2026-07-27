"""Fix historical nullable schema drift.

Revision ID: c2d3e4f5a6b7
Revises: b1c2d3e4f5a6
Create Date: 2026-04-19
"""

from alembic import op
import sqlalchemy as sa


revision = 'c2d3e4f5a6b7'
down_revision = 'b1c2d3e4f5a6'
branch_labels = None
depends_on = None


NULLABLE_FIXES = {
    'college_competitors': [
        ('events_entered', sa.Text(), '[]'),
        ('gear_sharing', sa.Text(), '{}'),
        ('partners', sa.Text(), '{}'),
        ('phone_opted_in', sa.Boolean(), False),
        ('status', sa.String(length=20), 'active'),
    ],
    'event_results': [
        ('is_flagged', sa.Boolean(), False),
        ('payout_amount', sa.Float(), 0.0),
        ('status', sa.String(length=20), 'pending'),
    ],
    'events': [
        ('has_prelims', sa.Boolean(), False),
        ('is_open', sa.Boolean(), False),
        ('is_partnered', sa.Boolean(), False),
        ('payouts', sa.Text(), '{}'),
        ('requires_dual_runs', sa.Boolean(), False),
        ('scoring_order', sa.String(length=20), 'lowest_wins'),
        ('status', sa.String(length=20), 'pending'),
    ],
    'flights': [
        ('status', sa.String(length=20), 'pending'),
    ],
    'heats': [
        ('competitors', sa.Text(), '[]'),
        ('run_number', sa.Integer(), 1),
        ('stand_assignments', sa.Text(), '{}'),
        ('status', sa.String(length=20), 'pending'),
    ],
    'payout_templates': [
        ('created_at', sa.DateTime(), sa.func.now()),
    ],
    'pro_competitors': [
        ('entry_fees', sa.Text(), '{}'),
        ('events_entered', sa.Text(), '[]'),
        ('fees_paid', sa.Text(), '{}'),
        ('gear_sharing', sa.Text(), '{}'),
        ('is_ala_member', sa.Boolean(), False),
        ('is_left_handed_springboard', sa.Boolean(), False),
        ('partners', sa.Text(), '{}'),
        ('payout_settled', sa.Boolean(), False),
        ('phone_opted_in', sa.Boolean(), False),
        ('pro_am_lottery_opt_in', sa.Boolean(), False),
        ('springboard_slow_heat', sa.Boolean(), False),
        ('status', sa.String(length=20), 'active'),
        ('total_earnings', sa.Float(), 0.0),
        ('total_fees', sa.Integer(), 0),
        ('waiver_accepted', sa.Boolean(), False),
    ],
    'school_captains': [
        ('created_at', sa.DateTime(), sa.func.now()),
    ],
    'teams': [
        ('status', sa.String(length=20), 'active'),
    ],
    'tournaments': [
        ('created_at', sa.DateTime(), sa.func.now()),
        ('providing_shirts', sa.Boolean(), False),
        ('status', sa.String(length=50), 'setup'),
        ('updated_at', sa.DateTime(), sa.func.now()),
    ],
}


def _backfill_nulls(table_name: str, column_name: str, column_type, fill_value) -> None:
    table = sa.table(table_name, sa.column(column_name, column_type))
    op.execute(
        table.update()
        .where(getattr(table.c, column_name).is_(None))
        .values({column_name: fill_value})
    )


def _set_not_null(table_name: str, column_name: str, column_type) -> None:
    bind = op.get_bind()
    if bind.dialect.name == 'sqlite':
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.alter_column(
                column_name,
                existing_type=column_type,
                nullable=False,
            )
        return

    op.alter_column(
        table_name,
        column_name,
        existing_type=column_type,
        nullable=False,
    )


def upgrade():
    for table_name, columns in NULLABLE_FIXES.items():
        for column_name, column_type, fill_value in columns:
            _backfill_nulls(table_name, column_name, column_type, fill_value)
            _set_not_null(table_name, column_name, column_type)


def downgrade():
    bind = op.get_bind()
    for table_name, columns in reversed(list(NULLABLE_FIXES.items())):
        for column_name, column_type, _fill_value in reversed(columns):
            if bind.dialect.name == 'sqlite':
                with op.batch_alter_table(table_name) as batch_op:
                    batch_op.alter_column(
                        column_name,
                        existing_type=column_type,
                        nullable=True,
                    )
                continue

            op.alter_column(
                table_name,
                column_name,
                existing_type=column_type,
                nullable=True,
            )
