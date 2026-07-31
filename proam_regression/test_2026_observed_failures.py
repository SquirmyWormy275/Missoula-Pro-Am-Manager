"""
Regression lock for the failures actually observed at the 2026 Missoula Pro-Am.

Program O7 in PROAM_2027_BASELINE_ROADMAP.md. Every other regression module in
this suite locks a defect found by AUDIT. This one locks the defects found by
the EVENT: the four things that went wrong in front of operators on April 24-25,
2026 and cost the app its trust. Audit findings and operational damage are not
the same population, and until this module existed the second one was the less
covered of the two.

Each test asserts CORRECT behavior against the real 2026 mirror.

Item 1 of 4: the ability-rankings wipe.

    Reported symptom: "I set my rankings, save, and they go back to whatever
    your preset was."

    First cause, fixed in V2.14.15: the delete-stale loop passed Python ``True``
    to ``.filter()`` when ranked_ids was empty, which SQLAlchemy emits as
    ``WHERE TRUE``, deleting every rank in the category. Covered by
    tests/test_ability_rankings_post_wipe_edgecase.py on a synthetic three-pro
    SQLite fixture.

    Second cause, found by writing this module and fixed alongside it: ranks are
    stored on (tournament_id, competitor_id, event_category) with NO gender,
    while the form submits one ``order_<category>_<gender>`` list PER GENDER.
    The V2.14.15 fix unioned every gender's list into one per-category set and
    skipped the cleanup only when that whole union was empty. A non-empty men's
    list therefore made the union non-empty, the cleanup ran across the entire
    category, and every woman's rank in it was deleted. The synthetic fixture
    could not see this: it seeds one gender.

    Measured on the real mirror before the fix, category ``underhand``, seeding
    four men and four women and posting the men's list with an empty women's
    list:

        ranks before   8
        men survived   [1, 3, 4, 20]
        women survived []
        women wiped    Kate Page, Brianna Kvinge, Chrissy Marcellus, Emma Macon

POPULATION FACTS, measured on proam_prod_mirror_p0, not assumed:

    active pro competitors        49   (33 M, 16 F, zero NULL gender)
    tournaments                    1   (id 2, "Missoula Pro Am")
    pro_event_ranks rows           0   (the 2026 event stored no ability ranks
                                        at all, which is consistent with the
                                        operators abandoning the tool)

    rank categories with BOTH genders entered:
        doublebuck    F 12  M 26
        obstacle_pole F  6  M 20
        pro_1board    F  2  M  8
        singlebuck    F  7  M 22
        underhand     F 13  M 25
    single-gender categories:
        springboard   M  9
        standing_block F 10

    Five of seven categories are cross-gender, so this was not a corner case on
    this dataset. It was the common case.

THE 'open' BUCKET. Jack & Jill is mixed-gender, so both genders submit through
one ``order_jack_jill_open`` list. The 2026 tournament ran no Jack & Jill, and
``ProCompetitor.gender`` is NOT NULL with a CHECK constraint limiting it to 'M'
and 'F', so on this dataset there is no other route into that bucket. That made
the first draft of this module blind to it: a fix that compared
``ProCompetitor.gender`` to the form's gender segment directly passed every test
here and still left the Jack & Jill ladder permanently un-editable. It survived
the mutation battery as M7.

It is covered now. The POST handler validates the category against
RANKED_CATEGORIES and never looks up an Event, so
test_the_mixed_gender_jack_jill_ladder_can_still_be_unranked seeds jack_jill
ranks directly and drives the real code path without staging an event. M7 dies
to it. The broader point stands and is filed in the roadmap: the real 2026 data
is not a sufficient oracle, and the cases it does not contain have to be built
rather than sampled.
"""

import pytest
import rig

TID = rig.TOURNAMENT_ID

RANKINGS_URL = f"/scheduling/{TID}/pro/ability-rankings"

# Measured populations. If the mirror is reseeded these assertions fail loudly
# rather than letting the tests below quietly run on a different world.
EXPECTED_ACTIVE_MEN = 33
EXPECTED_ACTIVE_WOMEN = 16


def _pros(gender, limit):
    """The first `limit` active pros of `gender`, in id order."""
    from models.competitor import ProCompetitor

    return (
        ProCompetitor.query.filter_by(
            tournament_id=TID, status="active", gender=gender
        )
        .order_by(ProCompetitor.id)
        .limit(limit)
        .all()
    )


def _seed(category, comps, start=1):
    """Give every competitor in `comps` a rank in `category`, 1-based from `start`."""
    from database import db
    from models.pro_event_rank import ProEventRank

    for offset, comp in enumerate(comps):
        db.session.add(
            ProEventRank(
                tournament_id=TID,
                competitor_id=comp.id,
                event_category=category,
                rank=start + offset,
            )
        )
    db.session.commit()


def _ranked(category):
    """{competitor_id: rank} currently stored for `category`, read fresh."""
    from database import db
    from models.pro_event_rank import ProEventRank

    db.session.expire_all()
    return {
        r.competitor_id: r.rank
        for r in ProEventRank.query.filter_by(
            tournament_id=TID, event_category=category
        ).all()
    }


def _csv(comps):
    return ",".join(str(c.id) for c in comps)


# ---------------------------------------------------------------------------
# Population guard. Everything below reads competitors out of the mirror by
# gender, so a reseed that changed the mix would silently weaken these tests.
# ---------------------------------------------------------------------------

@pytest.mark.sev1
def test_the_mirror_still_holds_the_population_these_tests_assume(app):
    from models.competitor import ProCompetitor

    men = ProCompetitor.query.filter_by(
        tournament_id=TID, status="active", gender="M"
    ).count()
    women = ProCompetitor.query.filter_by(
        tournament_id=TID, status="active", gender="F"
    ).count()
    assert (men, women) == (EXPECTED_ACTIVE_MEN, EXPECTED_ACTIVE_WOMEN), (
        f"mirror holds {men} active men and {women} active women; this module "
        f"was written against {EXPECTED_ACTIVE_MEN} and {EXPECTED_ACTIVE_WOMEN}"
    )


# ---------------------------------------------------------------------------
# THE DEFECT
# ---------------------------------------------------------------------------

@pytest.mark.sev1
def test_saving_the_mens_ladder_does_not_wipe_the_womens_ranks(app, client):
    """A men's underhand save must leave every woman's underhand rank standing.

    Ranks carry no gender, the form submits one list per gender, and the
    cleanup was scoped to the category. So the men's list, merely by being
    non-empty, authorised the deletion of the women's ladder.
    """
    men = _pros("M", 4)
    women = _pros("F", 4)
    assert len(men) == 4 and len(women) == 4
    _seed("underhand", men, start=1)
    _seed("underhand", women, start=5)
    assert len(_ranked("underhand")) == 8

    resp = client.post(
        RANKINGS_URL,
        data={"order_underhand_M": _csv(men), "order_underhand_F": ""},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)

    after = _ranked("underhand")
    survived = [w.id for w in women if w.id in after]
    wiped = [w.name for w in women if w.id not in after]
    assert not wiped, (
        f"saving the men's underhand ladder deleted {len(wiped)} women's "
        f"ranks: {wiped}"
    )
    assert len(survived) == 4
    # And the men's own save still landed.
    assert all(m.id in after for m in men)


@pytest.mark.sev1
def test_saving_the_womens_ladder_does_not_wipe_the_mens_ranks(app, client):
    """The same defect in the other direction.

    Stated separately rather than folded into the test above because a fix that
    scopes only the men's zone, or that scopes only the first list it processes,
    is a plausible half-fix and passes the one-direction version.
    """
    men = _pros("M", 4)
    women = _pros("F", 4)
    _seed("underhand", men, start=1)
    _seed("underhand", women, start=5)

    resp = client.post(
        RANKINGS_URL,
        data={"order_underhand_M": "", "order_underhand_F": _csv(women)},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)

    after = _ranked("underhand")
    wiped = [m.name for m in men if m.id not in after]
    assert not wiped, (
        f"saving the women's underhand ladder deleted {len(wiped)} men's "
        f"ranks: {wiped}"
    )
    assert all(w.id in after for w in women)


# ---------------------------------------------------------------------------
# POSITIVE CONTROLS
#
# Every one of these passes against the UNFIXED route on purpose. Their job is
# not to detect the defect above, it is to stop a fix that trades it for
# something worse. The obvious bad fix here is "stop deleting", which makes the
# defect test green and breaks unranking entirely.
# ---------------------------------------------------------------------------

@pytest.mark.sev1
def test_an_empty_ranked_zone_does_not_wipe_its_own_ladder(app, client):
    """The V2.14.15 regression, re-locked against real data.

    Submitting nothing but empty order_* inputs must delete nothing. This is
    the WHERE TRUE bug and it is covered only by a synthetic three-pro SQLite
    fixture today.
    """
    men = _pros("M", 5)
    _seed("underhand", men)
    assert len(_ranked("underhand")) == 5

    resp = client.post(
        RANKINGS_URL,
        data={"order_underhand_M": "", "order_underhand_F": ""},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)
    assert _ranked("underhand") == {m.id: i for i, m in enumerate(men, start=1)}


@pytest.mark.sev1
def test_dragging_one_man_out_of_the_ranked_zone_unranks_only_him(app, client):
    """The cleanup must still do its job. A fix that never deletes fails here."""
    men = _pros("M", 5)
    _seed("underhand", men)
    dropped = men[0]
    kept = men[1:]

    resp = client.post(
        RANKINGS_URL,
        data={"order_underhand_M": _csv(kept)},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)

    after = _ranked("underhand")
    assert dropped.id not in after, "the unranked man kept his rank"
    assert sorted(after) == sorted(k.id for k in kept)


@pytest.mark.sev1
def test_a_woman_can_be_unranked_from_her_own_ladder(app, client):
    """Symmetry control.

    A fix that scopes the cleanup to men, or that only ever runs on the first
    submitted list, leaves the women's ladder permanently un-editable. That is
    a quieter failure than the wipe and would otherwise ship unnoticed.
    """
    women = _pros("F", 5)
    _seed("underhand", women)
    dropped = women[0]
    kept = women[1:]

    resp = client.post(
        RANKINGS_URL,
        data={"order_underhand_F": _csv(kept)},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)

    after = _ranked("underhand")
    assert dropped.id not in after, "a woman dragged out of her ladder kept her rank"
    assert sorted(after) == sorted(k.id for k in kept)


@pytest.mark.sev1
def test_the_mixed_gender_jack_jill_ladder_can_still_be_unranked(app, client):
    """The 'open' bucket, where men and women legitimately share one ladder.

    This is the case that makes the fix a zone lookup rather than a gender
    comparison. Jack & Jill is mixed, so both genders submit through
    order_jack_jill_open, and a cleanup that compared ProCompetitor.gender to
    the form's gender segment would never match 'open' and would leave the
    ladder permanently un-editable.

    No Event row is created: the POST handler validates the category against
    RANKED_CATEGORIES and never looks up an event, so seeding the ranks
    directly exercises the real code path. That matters because the 2026
    tournament ran no Jack & Jill, and ProCompetitor.gender is NOT NULL with a
    CHECK constraint limiting it to 'M' and 'F', which together mean the 'open'
    bucket has no other way to be reached on this dataset.
    """
    man = _pros("M", 1)[0]
    woman = _pros("F", 1)[0]
    _seed("jack_jill", [man, woman])
    assert len(_ranked("jack_jill")) == 2

    resp = client.post(
        RANKINGS_URL,
        data={"order_jack_jill_open": str(man.id)},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)

    after = _ranked("jack_jill")
    assert woman.id not in after, (
        "a competitor dragged out of the mixed Jack & Jill ladder kept her rank"
    )
    assert after == {man.id: 1}


@pytest.mark.sev1
def test_saving_underhand_does_not_disturb_singlebuck(app, client):
    """Cross-category isolation. A fix that widened the delete instead of
    narrowing it fails here."""
    men = _pros("M", 4)
    women = _pros("F", 4)
    _seed("underhand", men, start=1)
    _seed("singlebuck", women, start=1)
    before_sb = _ranked("singlebuck")
    assert len(before_sb) == 4

    resp = client.post(
        RANKINGS_URL,
        data={"order_underhand_M": _csv(men)},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)
    assert _ranked("singlebuck") == before_sb


@pytest.mark.sev1
def test_submitted_order_is_stored_as_one_based_positions(app, client):
    """Rank is position in the submitted list, counting from 1, per gender.

    Men and women share a category key, so both ladders legitimately start at
    rank 1. A fix that tried to make ranks unique across the category by
    offsetting one gender would corrupt the draft order and fails here.
    """
    men = _pros("M", 4)
    women = _pros("F", 3)
    reversed_men = list(reversed(men))

    resp = client.post(
        RANKINGS_URL,
        data={
            "order_underhand_M": _csv(reversed_men),
            "order_underhand_F": _csv(women),
        },
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)

    after = _ranked("underhand")
    assert [after[m.id] for m in reversed_men] == [1, 2, 3, 4]
    assert [after[w.id] for w in women] == [1, 2, 3]
