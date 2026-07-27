"""Add first-pass database integrity check constraints.

Revision ID: f5a6b7c8d9e0
Revises: e4f5a6b7c8d9
Create Date: 2026-04-20
"""

from alembic import op


revision = 'f5a6b7c8d9e0'
down_revision = 'e4f5a6b7c8d9'
branch_labels = None
depends_on = None

def upgrade():
    with op.batch_alter_table('teams') as batch_op:
        batch_op.create_check_constraint('ck_teams_status_valid', "status IN ('active', 'scratched', 'invalid')")
        batch_op.create_check_constraint('ck_teams_total_points_nonnegative', 'total_points >= 0')

    with op.batch_alter_table('college_competitors') as batch_op:
        batch_op.create_check_constraint('ck_college_competitors_gender_valid', "gender IN ('M', 'F')")
        batch_op.create_check_constraint('ck_college_competitors_status_valid', "status IN ('active', 'scratched')")
        batch_op.create_check_constraint('ck_college_competitors_points_nonnegative', 'individual_points >= 0')

    with op.batch_alter_table('pro_competitors') as batch_op:
        batch_op.create_check_constraint('ck_pro_competitors_gender_valid', "gender IN ('M', 'F')")
        batch_op.create_check_constraint('ck_pro_competitors_status_valid', "status IN ('active', 'scratched')")
        batch_op.create_check_constraint('ck_pro_competitors_earnings_nonnegative', 'total_earnings >= 0')
        batch_op.create_check_constraint('ck_pro_competitors_total_fees_nonnegative', 'total_fees >= 0')

    with op.batch_alter_table('events') as batch_op:
        batch_op.create_check_constraint('ck_events_event_type_valid', "event_type IN ('college', 'pro')")
        batch_op.create_check_constraint(
            'ck_events_scoring_order_valid',
            "scoring_order IN ('lowest_wins', 'highest_wins')",
        )
        batch_op.create_check_constraint(
            'ck_events_status_valid',
            "status IN ('pending', 'in_progress', 'completed')",
        )
        batch_op.create_check_constraint(
            'ck_events_max_stands_valid',
            '(max_stands IS NULL) OR (max_stands >= 1)',
        )

    with op.batch_alter_table('event_results') as batch_op:
        batch_op.create_check_constraint(
            'ck_event_results_competitor_type_valid',
            "competitor_type IN ('college', 'pro')",
        )
        batch_op.create_check_constraint(
            'ck_event_results_status_valid',
            "status IN ('pending', 'completed', 'scratched', 'dnf', 'dq', 'partial')",
        )
        batch_op.create_check_constraint(
            'ck_event_results_final_position_valid',
            '(final_position IS NULL) OR (final_position >= 1)',
        )
        batch_op.create_check_constraint('ck_event_results_points_nonnegative', 'points_awarded >= 0')
        batch_op.create_check_constraint('ck_event_results_payout_nonnegative', 'payout_amount >= 0')

    with op.batch_alter_table('heats') as batch_op:
        batch_op.create_check_constraint('ck_heats_heat_number_positive', 'heat_number >= 1')
        batch_op.create_check_constraint('ck_heats_run_number_positive', 'run_number >= 1')
        batch_op.create_check_constraint(
            'ck_heats_status_valid',
            "status IN ('pending', 'in_progress', 'completed')",
        )

    with op.batch_alter_table('flights') as batch_op:
        batch_op.create_check_constraint('ck_flights_flight_number_positive', 'flight_number >= 1')
        batch_op.create_check_constraint(
            'ck_flights_status_valid',
            "status IN ('pending', 'in_progress', 'completed')",
        )


def downgrade():
    with op.batch_alter_table('flights') as batch_op:
        batch_op.drop_constraint('ck_flights_status_valid', type_='check')
        batch_op.drop_constraint('ck_flights_flight_number_positive', type_='check')

    with op.batch_alter_table('heats') as batch_op:
        batch_op.drop_constraint('ck_heats_status_valid', type_='check')
        batch_op.drop_constraint('ck_heats_run_number_positive', type_='check')
        batch_op.drop_constraint('ck_heats_heat_number_positive', type_='check')

    with op.batch_alter_table('event_results') as batch_op:
        batch_op.drop_constraint('ck_event_results_payout_nonnegative', type_='check')
        batch_op.drop_constraint('ck_event_results_points_nonnegative', type_='check')
        batch_op.drop_constraint('ck_event_results_final_position_valid', type_='check')
        batch_op.drop_constraint('ck_event_results_status_valid', type_='check')
        batch_op.drop_constraint('ck_event_results_competitor_type_valid', type_='check')

    with op.batch_alter_table('events') as batch_op:
        batch_op.drop_constraint('ck_events_max_stands_valid', type_='check')
        batch_op.drop_constraint('ck_events_status_valid', type_='check')
        batch_op.drop_constraint('ck_events_scoring_order_valid', type_='check')
        batch_op.drop_constraint('ck_events_event_type_valid', type_='check')

    with op.batch_alter_table('pro_competitors') as batch_op:
        batch_op.drop_constraint('ck_pro_competitors_total_fees_nonnegative', type_='check')
        batch_op.drop_constraint('ck_pro_competitors_earnings_nonnegative', type_='check')
        batch_op.drop_constraint('ck_pro_competitors_status_valid', type_='check')
        batch_op.drop_constraint('ck_pro_competitors_gender_valid', type_='check')

    with op.batch_alter_table('college_competitors') as batch_op:
        batch_op.drop_constraint('ck_college_competitors_points_nonnegative', type_='check')
        batch_op.drop_constraint('ck_college_competitors_status_valid', type_='check')
        batch_op.drop_constraint('ck_college_competitors_gender_valid', type_='check')

    with op.batch_alter_table('teams') as batch_op:
        batch_op.drop_constraint('ck_teams_total_points_nonnegative', type_='check')
        batch_op.drop_constraint('ck_teams_status_valid', type_='check')
