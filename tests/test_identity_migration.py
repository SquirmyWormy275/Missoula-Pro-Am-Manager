"""Guards on the competitor identity substrate landed by `n4c5d6e7f8a9`.

Three things are pinned here, all of which a future change could break silently:

1. **CHECK constraint counts survive the batch rebuild.**  `college_competitors`
   and `pro_competitors` are rebuilt by Alembic batch mode in that migration.
   Under SQLAlchemy 2.0.23 / Alembic 1.18.5 the SQLite dialect reflects named
   CHECK constraints and batch mode carries them into the rebuilt table, so the
   counts stay at 3 and 4.  Nothing else in this repo compares CHECK
   constraints.  If a SQLAlchemy upgrade changed that reflection behaviour, the
   database would quietly stop enforcing gender, status, and the non-negative
   money invariants while CI stayed green.  Asserting the exact count also
   catches the opposite failure, which was the one that actually happened
   during development: passing the constraints again via `table_args` doubled
   every CHECK to 6 and 8.

2. **The identity allocator fires on every insert path.**  `uid` is NOT NULL and
   there are 188 non-test `db.session.commit()` sites.  The allocator is a
   mapper-level `before_insert` event precisely so no insert site has to know
   about identities.  These tests insert competitors the ordinary way and assert
   a uid appeared.

3. **A college id and a pro id can collide; their uids cannot.**  This is the
   whole point of the phase, so it gets a direct assertion rather than being
   implied by the schema.
"""
import pytest
import sqlalchemy as sa

from models.competitor import ProCompetitor
from models.competitor_identity import Competitor
from services.entity_key import COLLEGE, PRO, EntityKey, resolve_uid, resolve_uids
from tests.conftest import make_college_competitor, make_pro_competitor, make_team, make_tournament
from tests.db_test_utils import skip_unless_migrated


def _inspector(session):
    """Dialect-neutral schema reflection.

    These guards used to read raw DDL out of `sqlite_master` and regex the
    constraint names out of the text. That worked, but it was SQLite-only, so
    the whole class silently became unrunnable the moment the suite gained a
    PostgreSQL backend (D14-B). The inspector answers the same questions on
    both engines, and it reads the constraints the database actually holds
    rather than the string it happened to be created with, which is a
    stronger claim than the original made.
    """
    return sa.inspect(session.get_bind())


def _check_names(session, table_name):
    """Names of CHECK constraints on a table, one entry per constraint."""
    insp = _inspector(session)
    assert table_name in insp.get_table_names(), f'table {table_name} not found'
    return sorted(c['name'] for c in insp.get_check_constraints(table_name))


class TestCheckConstraintsSurviveBatchRebuild:
    """The batch rebuild must neither drop nor duplicate CHECK constraints."""

    def test_college_competitors_has_exactly_three_checks(self, db_session):
        names = _check_names(db_session, 'college_competitors')
        assert len(names) == 3, (
            f'expected exactly 3 CHECK constraints on college_competitors, '
            f'got {len(names)}: {names}. '
            f'More than 3 means something is emitting them twice (batch '
            f'reflection plus an explicit table_args). Fewer than 3 means the '
            f'batch rebuild dropped them and the database is no longer '
            f'enforcing gender, status, or non-negative points.'
        )
        assert set(names) == {
            'ck_college_competitors_gender_valid',
            'ck_college_competitors_status_valid',
            'ck_college_competitors_points_nonnegative',
        }

    def test_pro_competitors_has_exactly_four_checks(self, db_session):
        names = _check_names(db_session, 'pro_competitors')
        assert len(names) == 4, (
            f'expected exactly 4 CHECK constraints on pro_competitors, '
            f'got {len(names)}: {names}'
        )
        assert set(names) == {
            'ck_pro_competitors_gender_valid',
            'ck_pro_competitors_status_valid',
            'ck_pro_competitors_earnings_nonnegative',
            'ck_pro_competitors_total_fees_nonnegative',
        }

    def test_checks_are_actually_enforced(self, db_session):
        """Counting names in DDL is not the same as the constraint working.

        Written as an UPDATE on a legitimately-created row rather than a raw
        INSERT.  A raw INSERT has to name every NOT NULL column on the table or
        it fails on the wrong constraint and the test passes for the wrong
        reason, which is exactly what the first draft of this test did (it hit
        `NOT NULL constraint failed: pro_competitors.is_ala_member` and never
        reached the gender CHECK at all).  An UPDATE touches one column, so a
        failure here can only be the CHECK.
        """
        t = make_tournament(db_session)
        p = make_pro_competitor(db_session, t, 'Check Enforcement')
        db_session.flush()

        with pytest.raises(sa.exc.IntegrityError) as exc:
            db_session.execute(
                sa.text('UPDATE pro_competitors SET gender = :g WHERE id = :i'),
                {'g': 'X', 'i': p.id},
            )
        assert 'ck_pro_competitors_gender_valid' in str(exc.value), (
            f'expected the gender CHECK to reject it, got: {exc.value}'
        )
        db_session.rollback()

    def test_status_check_is_enforced(self, db_session):
        """Second constraint, second table: the rebuild did not disarm college."""
        t = make_tournament(db_session)
        team = make_team(db_session, t)
        c = make_college_competitor(db_session, t, team, 'Check Enforcement C')
        db_session.flush()

        with pytest.raises(sa.exc.IntegrityError) as exc:
            db_session.execute(
                sa.text('UPDATE college_competitors SET status = :s WHERE id = :i'),
                {'s': 'nonsense', 'i': c.id},
            )
        assert 'ck_college_competitors_status_valid' in str(exc.value), (
            f'expected the status CHECK to reject it, got: {exc.value}'
        )
        db_session.rollback()

    def test_uid_constraints_landed(self, db_session):
        # `uid` is declared inline on the model (unique=True,
        # db.ForeignKey(...)), so db.create_all() emits the constraints
        # ANONYMOUSLY while the migration that added them named them
        # uq_/fk_<table>_uid. The three sibling tests in this class pass on
        # both paths only because their CHECKs carry explicit name= in
        # __table_args__. The real fix is a naming_convention on the model
        # MetaData, which changes autogenerate output and has not been
        # approved; until then this guard is a statement about the migrated
        # schema, which is the one production runs.
        skip_unless_migrated(db_session, 'the uid identity constraints')
        insp = _inspector(db_session)
        for table in ('college_competitors', 'pro_competitors'):
            uniques = {u['name'] for u in insp.get_unique_constraints(table)}
            assert f'uq_{table}_uid' in uniques, (
                f'{table} lost its uid UNIQUE constraint; have {sorted(uniques)}'
            )
            fks = {f['name'] for f in insp.get_foreign_keys(table)}
            assert f'fk_{table}_uid' in fks, (
                f'{table} lost its uid FOREIGN KEY; have {sorted(map(str, fks))}'
            )
            uid_col = [c for c in insp.get_columns(table) if c['name'] == 'uid']
            assert uid_col, f'{table} has no uid column at all'
            assert uid_col[0]['nullable'] is False, f'{table}.uid is not NOT NULL'


class TestIdentityAllocator:
    """`uid` must be filled by ordinary inserts that know nothing about it."""

    def test_college_insert_allocates_uid(self, db_session):
        t = make_tournament(db_session)
        team = make_team(db_session, t)
        c = make_college_competitor(db_session, t, team, 'Allocator College')
        db_session.flush()
        assert c.uid is not None
        identity = db_session.get(Competitor, c.uid)
        assert identity is not None
        assert identity.kind == 'college'
        assert identity.tournament_id == t.id

    def test_pro_insert_allocates_uid(self, db_session):
        t = make_tournament(db_session)
        p = make_pro_competitor(db_session, t, 'Allocator Pro')
        db_session.flush()
        assert p.uid is not None
        identity = db_session.get(Competitor, p.uid)
        assert identity is not None
        assert identity.kind == 'pro'

    def test_explicit_uid_is_honoured(self, db_session):
        """The backfill and any future merge path supply their own uid."""
        t = make_tournament(db_session)
        db_session.flush()
        identity = Competitor(kind='pro', tournament_id=t.id)
        db_session.add(identity)
        db_session.flush()

        p = ProCompetitor(tournament_id=t.id, name='Explicit Uid', gender='M')
        p.uid = identity.uid
        db_session.add(p)
        db_session.flush()
        assert p.uid == identity.uid

    def test_bulk_insert_allocates_distinct_uids(self, db_session):
        t = make_tournament(db_session)
        team = make_team(db_session, t)
        made = [
            make_college_competitor(db_session, t, team, f'Bulk {i}')
            for i in range(10)
        ]
        db_session.flush()
        uids = [c.uid for c in made]
        assert all(u is not None for u in uids)
        assert len(set(uids)) == 10


class TestCrossDisciplineUniqueness:
    """The reason the phase exists."""

    def test_colliding_ids_get_distinct_uids(self, db_session):
        t = make_tournament(db_session)
        team = make_team(db_session, t)
        c = make_college_competitor(db_session, t, team, 'Collision College')
        p = make_pro_competitor(db_session, t, 'Collision Pro')
        db_session.flush()

        # Force the legacy ids to collide, which is the real production
        # condition: 21 such collisions exist in the mirror.
        db_session.execute(
            sa.text('UPDATE pro_competitors SET id = :new WHERE id = :old'),
            {'new': c.id + 100000, 'old': p.id},
        )
        db_session.execute(
            sa.text('UPDATE college_competitors SET id = :new WHERE id = :old'),
            {'new': c.id + 100000, 'old': c.id},
        )
        colliding_id = c.id + 100000

        rows = db_session.execute(
            sa.text(
                'SELECT uid FROM college_competitors WHERE id = :i '
                'UNION ALL SELECT uid FROM pro_competitors WHERE id = :i'
            ),
            {'i': colliding_id},
        ).all()
        uids = [r[0] for r in rows]
        assert len(uids) == 2, 'expected the id to exist in both tables'
        assert uids[0] != uids[1], 'colliding legacy ids produced the same uid'


class TestEntityKey:
    """The transitional wrapper. Imported by nothing in production yet."""

    def test_rejects_unknown_kind(self):
        with pytest.raises(ValueError):
            EntityKey(kind='amateur', id=1)

    def test_rejects_bool_id(self):
        with pytest.raises(TypeError):
            EntityKey(kind=PRO, id=True)

    def test_hashable_and_comparable(self):
        assert EntityKey(COLLEGE, 5) == EntityKey(COLLEGE, 5)
        assert EntityKey(COLLEGE, 5) != EntityKey(PRO, 5)
        assert len({EntityKey(COLLEGE, 5), EntityKey(PRO, 5)}) == 2

    def test_from_legacy_round_trip(self):
        k = EntityKey.from_legacy(7, 'pro')
        assert k == EntityKey(PRO, 7)
        assert k.as_legacy() == (7, 'pro')

    def test_from_legacy_none_on_missing_half(self):
        assert EntityKey.from_legacy(None, 'pro') is None
        assert EntityKey.from_legacy(7, None) is None

    def test_resolve_uid(self, db_session):
        t = make_tournament(db_session)
        p = make_pro_competitor(db_session, t, 'Resolvable')
        db_session.flush()
        assert resolve_uid(db_session, EntityKey(PRO, p.id)) == p.uid
        assert resolve_uid(db_session, None) is None
        assert resolve_uid(db_session, EntityKey(PRO, 999999)) is None

    def test_resolve_uids_batches_and_omits_misses(self, db_session):
        t = make_tournament(db_session)
        team = make_team(db_session, t)
        c = make_college_competitor(db_session, t, team, 'Batch College')
        p = make_pro_competitor(db_session, t, 'Batch Pro')
        db_session.flush()

        keys = [
            EntityKey(COLLEGE, c.id),
            EntityKey(PRO, p.id),
            EntityKey(PRO, 999999),
        ]
        out = resolve_uids(db_session, keys)
        assert out == {
            EntityKey(COLLEGE, c.id): c.uid,
            EntityKey(PRO, p.id): p.uid,
        }


class TestEventLogTable:
    """`tournament_event` is inert in this phase but must exist and constrain."""

    def test_table_exists_with_unique_seq(self, db_session):
        insp = _inspector(db_session)
        assert 'tournament_event' in insp.get_table_names()
        uniques = {u['name'] for u in
                   insp.get_unique_constraints('tournament_event')}
        assert 'uq_tournament_event_tournament_seq' in uniques, (
            f'have {sorted(uniques)}'
        )

    def test_duplicate_seq_within_tournament_rejected(self, db_session):
        from models.tournament_event import TournamentEvent

        t = make_tournament(db_session)
        db_session.flush()
        db_session.add(TournamentEvent(
            tournament_id=t.id, seq=1, kind='test', payload='{}'))
        db_session.flush()
        db_session.add(TournamentEvent(
            tournament_id=t.id, seq=1, kind='test', payload='{}'))
        with pytest.raises(Exception):
            db_session.flush()
        db_session.rollback()

    def test_same_seq_in_different_tournaments_allowed(self, db_session):
        from models.tournament_event import TournamentEvent

        t1 = make_tournament(db_session, name='T One')
        t2 = make_tournament(db_session, name='T Two')
        db_session.flush()
        db_session.add(TournamentEvent(
            tournament_id=t1.id, seq=1, kind='test', payload='{}'))
        db_session.add(TournamentEvent(
            tournament_id=t2.id, seq=1, kind='test', payload='{}'))
        db_session.flush()  # must not raise
