"""Make flight numbers and assigned heat positions unique.

Revision ID: z5d6e7f8a9b0
Revises: y4c5d6e7f8a9
Create Date: 2026-08-14

Only pending flights that actually contain duplicate occupied positions are
repaired. Valid gaps and historical flight layouts are left byte-for-byte
unchanged. If an ambiguous flight contains in-progress or completed history,
the migration fails with the affected IDs instead of inventing a show order.
Duplicate flight numbers also fail closed because a migration cannot infer
which number the operator intended.
"""

from alembic import op
import sqlalchemy as sa


revision = "z5d6e7f8a9b0"
down_revision = "y4c5d6e7f8a9"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # Railway runs migrations before replacing the old application. Block
        # its schedule writers for the entire scan/repair/constraint sequence
        # so the rows validated below cannot change between those phases.
        bind.execute(sa.text(
            "LOCK TABLE flights, heats IN SHARE ROW EXCLUSIVE MODE"
        ))

    duplicate_slots = bind.execute(sa.text("""
        SELECT flight_id, flight_position, COUNT(*) AS duplicate_count
        FROM heats
        WHERE flight_id IS NOT NULL AND flight_position IS NOT NULL
        GROUP BY flight_id, flight_position
        HAVING COUNT(*) > 1
        ORDER BY flight_id, flight_position
    """)).mappings().all()
    duplicate_flight_numbers = bind.execute(sa.text("""
        SELECT tournament_id, flight_number, COUNT(*) AS duplicate_count
        FROM flights
        GROUP BY tournament_id, flight_number
        HAVING COUNT(*) > 1
        ORDER BY tournament_id, flight_number
    """)).mappings().all()

    if duplicate_flight_numbers:
        details = ', '.join(
            f"tournament {row['tournament_id']} number {row['flight_number']}"
            for row in duplicate_flight_numbers
        )
        raise RuntimeError(
            'Cannot enforce unique flight numbers; resolve duplicate '
            f'operator-defined numbers first: {details}.'
        )

    affected_flight_ids = sorted({row['flight_id'] for row in duplicate_slots})
    protected_flights = []
    for flight_id in affected_flight_ids:
        protected = bind.execute(sa.text("""
            SELECT DISTINCT f.id AS flight_id, f.status AS flight_status,
                            h.id AS heat_id, h.status AS heat_status
            FROM flights AS f
            JOIN heats AS h ON h.flight_id = f.id
            WHERE f.id = :flight_id
              AND (f.status <> 'pending' OR h.status <> 'pending')
            ORDER BY h.id
        """), {'flight_id': flight_id}).mappings().all()
        protected_flights.extend(protected)

    if protected_flights:
        details = ', '.join(
            f"flight {row['flight_id']} ({row['flight_status']}), "
            f"heat {row['heat_id']} ({row['heat_status']})"
            for row in protected_flights
        )
        raise RuntimeError(
            'Cannot repair duplicate flight positions because historical '
            f'or active placements would be rewritten: {details}.'
        )

    for flight_id in affected_flight_ids:
        ordered_heat_ids = bind.execute(sa.text("""
            SELECT id
            FROM heats
            WHERE flight_id = :flight_id
            ORDER BY
                CASE WHEN flight_position IS NULL THEN 1 ELSE 0 END,
                flight_position,
                id
        """), {'flight_id': flight_id}).scalars().all()
        bind.execute(sa.text("""
            UPDATE heats
            SET flight_position = NULL
            WHERE flight_id = :flight_id
        """), {'flight_id': flight_id})
        for repaired_position, heat_id in enumerate(ordered_heat_ids, start=1):
            bind.execute(sa.text("""
                UPDATE heats
                SET flight_position = :position
                WHERE id = :heat_id
            """), {
                'position': repaired_position,
                'heat_id': heat_id,
            })

    with op.batch_alter_table("heats", schema=None) as batch_op:
        batch_op.create_unique_constraint(
            "uq_heats_flight_position",
            ["flight_id", "flight_position"],
        )
    with op.batch_alter_table("flights", schema=None) as batch_op:
        batch_op.create_unique_constraint(
            "uq_flights_tournament_number",
            ["tournament_id", "flight_number"],
        )


def downgrade():
    with op.batch_alter_table("flights", schema=None) as batch_op:
        batch_op.drop_constraint(
            "uq_flights_tournament_number",
            type_="unique",
        )
    with op.batch_alter_table("heats", schema=None) as batch_op:
        batch_op.drop_constraint(
            "uq_heats_flight_position",
            type_="unique",
        )
