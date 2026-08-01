"""Move competitor contact onto the identity spine.

Revision ID: q6e7f8a0b2c3
Revises: p5d6e7f8a0b2
Create Date: 2026-08-01

PG-safety: no batch_alter_table (every operation is an `add_column` or a data
UPDATE, neither of which SQLite needs a table rebuild for). No integer-style
Boolean server_default. No SQLite-only introspection statements.

What this migration does
========================
1. Adds `address`, `phone`, `email` and `phone_opted_in` to `competitors`.
2. Copies the existing values off `pro_competitors` and `college_competitors`
   onto the matching spine row.

It drops nothing. The per-kind columns still hold their values when this
migration finishes; revision `r7f8a0b2c3d4` drops them, and only after a guard
proves the spine holds everything they did.

Why contact belongs here and not on the per-kind tables
=======================================================
Be precise about what the spine is, because it is easy to oversell. A
`competitors` row is one per competitor registration, not one per human: a
person who enters both the college and the pro side gets a `kind='college'`
row and a `kind='pro'` row, with different uids. This migration does not merge
those two people into one, and it is not a deduplication.

What it does is make contact a property every competitor has, in one shape, at
one address, regardless of discipline. Today `pro_competitors` carries
`address`/`phone`/`email`/`phone_opted_in` and `college_competitors` carries
only `phone_opted_in`. Any code that wants to reach a competitor has to know
which table it is looking at and has to know that one of the two answers does
not exist. That asymmetry is not a data-modelling opinion, it is the direct
cause of the bug below.

The concrete bug this closes: `routes/scheduling/flights.py` builds its SMS
recipient list by querying `ProCompetitor.phone_opted_in` and
`CollegeCompetitor.phone_opted_in` separately. `college_competitors` has a
`phone_opted_in` column and no `phone` column, so the college branch could
never send anything. It looped over its query results and did nothing.

The same missing column silently disabled the feature one layer up.
`templates/portal/competitor_dashboard.html` gates its SMS card on
`competitor.phone or competitor_type == 'pro'`, and this app does not configure
Jinja's StrictUndefined, so on a college competitor `competitor.phone` resolved
to Undefined, evaluated falsy, and the card disappeared without an error. A
college competitor could not reach the opt-in toggle from the UI at all, while
`portal.sms_opt_in_toggle` accepted `competitor_type='college'` and wrote the
flag happily. Three layers agreed to do nothing and not one of them said so.

Nothing visible changes for the April 2026 data: no college competitor in it
has a phone number, so that card stays hidden. What changes is that it stays
hidden because the value is absent, not because the attribute is.

Why the backfill uses correlated subqueries
===========================================
`UPDATE ... FROM` is PostgreSQL syntax. SQLite has supported it since 3.33 and
this container runs 3.45, but the migration chain has to replay on whatever
SQLite a developer happens to have, and a correlated subquery is the form both
dialects have always accepted. Speed is irrelevant here: the production
database holds 113 competitor rows.

Why phone_opted_in is NOT NULL and the other three are not
==========================================================
`phone_opted_in` feeds a boolean predicate. Making it nullable would force
every read site to decide what a NULL opt-in means, which is a third state
nobody wants. It defaults false: a competitor who has never seen the toggle has
not consented.

The other three are genuinely absent for most rows. No college competitor in
the production data has an address, phone or email, and a pro can register
without an email. NULL is the honest value.
"""
import sqlalchemy as sa
from alembic import op

revision = "q6e7f8a0b2c3"
down_revision = "p5d6e7f8a0b2"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("competitors", sa.Column("address", sa.Text(), nullable=True))
    op.add_column("competitors", sa.Column("phone", sa.String(length=50), nullable=True))
    op.add_column("competitors", sa.Column("email", sa.String(length=200), nullable=True))
    op.add_column(
        "competitors",
        sa.Column(
            "phone_opted_in",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )

    connection = op.get_bind()

    # Pros carry all four facts.
    connection.execute(sa.text("""
        UPDATE competitors SET
            address = (SELECT p.address FROM pro_competitors p
                        WHERE p.uid = competitors.uid),
            phone = (SELECT p.phone FROM pro_competitors p
                      WHERE p.uid = competitors.uid),
            email = (SELECT p.email FROM pro_competitors p
                      WHERE p.uid = competitors.uid),
            phone_opted_in = COALESCE(
                (SELECT p.phone_opted_in FROM pro_competitors p
                  WHERE p.uid = competitors.uid), false)
        WHERE kind = 'pro'
          AND EXISTS (SELECT 1 FROM pro_competitors p WHERE p.uid = competitors.uid)
    """))

    # Colleges carry only the opt-in flag. There has never been a college
    # phone column for it to govern; see the docstring.
    connection.execute(sa.text("""
        UPDATE competitors SET
            phone_opted_in = COALESCE(
                (SELECT c.phone_opted_in FROM college_competitors c
                  WHERE c.uid = competitors.uid), false)
        WHERE kind = 'college'
          AND EXISTS (SELECT 1 FROM college_competitors c WHERE c.uid = competitors.uid)
    """))


def downgrade():
    op.drop_column("competitors", "phone_opted_in")
    op.drop_column("competitors", "email")
    op.drop_column("competitors", "phone")
    op.drop_column("competitors", "address")
