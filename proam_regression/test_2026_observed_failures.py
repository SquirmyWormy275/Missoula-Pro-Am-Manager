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


# ===========================================================================
# Item 2 of 4: the opaque "Placed: 37 / 64" panel.
#
#     Reported symptom: the Events page status card said "Placed: 37 / 64
#     competitors" and nothing else. Operators could not tell whether heat
#     generation had skipped someone, or who, or why. It was replaced
#     post-event (V2.14.15) with four categorized buckets: placed,
#     non_heat_only, no_events, missing_from_heats, plus a name sample.
#
#     Measured on the real mirror: the panel reproduces the race-day number
#     EXACTLY. Friday reports placed 37 of 64 with 27 active college
#     competitors in missing_from_heats. Those 27 are genuinely absent: each
#     is entered in college events that have heats and appears in none of
#     them, and their ids cluster at the top of the table (66+), which is
#     consistent with late registrants added after heat generation with no
#     rebuild. The race-day number was TRUE. The failure was showing it with
#     no names attached.
#
#     These tests lock the replacement panel's arithmetic and its naming
#     behavior against the real data, so the panel can never again report a
#     number whose people cannot be listed.
#
#     Known limits, stated: missing_sample is capped at 10 names of the 27,
#     and the competitor query behind it carries no ORDER BY, so WHICH 10
#     appear is row-order dependent (O3 territory). The tests therefore
#     assert sample membership against the independently computed missing
#     set, never an exact sample list. Also filed: missing_from_heats is
#     commented "BUG surface" in schedule_status.py yet feeds neither the
#     warnings list nor overall_severity, so a fully-heated schedule with 27
#     stranded competitors still reads "Schedule ready". That is the c23
#     dead-detector shape and is a product decision (D3/D5), not silently
#     changed here.
# ===========================================================================

# Measured on proam_prod_mirror_p0. The guard test asserts these so a reseed
# fails loudly instead of silently rebasing every assertion below.
EXPECTED_ACTIVE_COLLEGE = 64
EXPECTED_FRIDAY_PLACED = 37
EXPECTED_FRIDAY_MISSING = 27
SAMPLE_CAP = 10


def _college_ground_truth():
    """Independently computed (placed_ids, missing_ids) for active college
    competitors, straight from the heat tables, no schedule_status code."""
    from models.competitor import CollegeCompetitor
    from models.event import Event
    from models.heat import Heat

    active = CollegeCompetitor.query.filter_by(
        tournament_id=TID, status="active"
    ).all()
    active_ids = {c.id for c in active}
    in_heats: set = set()
    heats = (
        Heat.query.join(Event)
        .filter(Event.tournament_id == TID, Event.event_type == "college")
        .all()
    )
    for h in heats:
        for cid in h.get_competitors():
            if int(cid) in active_ids:
                in_heats.add(int(cid))
    return in_heats, active_ids - in_heats


def _status():
    from flask import current_app

    from database import db
    from models import Tournament
    from services.schedule_status import build_schedule_status

    # build_schedule_status calls url_for for the drill-down links, which
    # needs a request context. The production caller is a GET route, so a
    # test request context is the honest equivalent, not a workaround.
    with current_app.test_request_context():
        return build_schedule_status(db.session.get(Tournament, TID))


@pytest.mark.sev1
def test_the_real_2026_placed_panel_names_its_missing_27(app):
    """The race-day number, now with every one of its people accounted for.

    Friday must report placed 37 of 64 and missing 27, those two sets must
    match an independent read of the heat tables, and every sampled name
    must belong to the measured missing population.
    """
    from models.competitor import CollegeCompetitor

    placed_ids, missing_ids = _college_ground_truth()
    assert len(placed_ids) == EXPECTED_FRIDAY_PLACED
    assert len(missing_ids) == EXPECTED_FRIDAY_MISSING

    f = _status()["friday"]
    assert f["competitors_total"] == EXPECTED_ACTIVE_COLLEGE
    assert f["competitors_placed"] == EXPECTED_FRIDAY_PLACED
    assert f["competitors_missing_from_heats"] == EXPECTED_FRIDAY_MISSING

    missing_names = {
        c.name
        for c in CollegeCompetitor.query.filter(
            CollegeCompetitor.id.in_(missing_ids)
        ).all()
    }
    sample = f["competitors_missing_sample"]
    assert len(sample) == SAMPLE_CAP, (
        f"27 missing must fill the {SAMPLE_CAP}-name sample, got {len(sample)}"
    )
    strangers = [n for n in sample if n not in missing_names]
    assert not strangers, (
        f"panel sampled names that are not in the measured missing set: {strangers}"
    )


@pytest.mark.sev1
def test_the_four_buckets_sum_to_the_total_on_both_days(app):
    """placed + non_heat_only + no_events + missing_from_heats == total.

    This is the arithmetic contract that makes the panel readable at a
    glance. The 2026 panel had no buckets at all; a replacement whose
    buckets overlap or leak would be opacity with more numbers.
    """
    s = _status()
    for day in ("friday", "saturday"):
        d = s[day]
        total = (
            d["competitors_placed"]
            + d["competitors_non_heat_only"]
            + d["competitors_no_events"]
            + d["competitors_missing_from_heats"]
        )
        assert total == d["competitors_total"], (
            f"{day}: buckets sum to {total}, total is {d['competitors_total']}"
        )


@pytest.mark.sev1
def test_saturday_reports_every_pro_placed(app):
    """The pro side of the real data is fully placed: 49 of 49, empty
    buckets. Locks the healthy case so a classifier change that starts
    leaking placed pros into a bucket is caught by the day that is clean."""
    d = _status()["saturday"]
    assert d["competitors_total"] == EXPECTED_ACTIVE_MEN + EXPECTED_ACTIVE_WOMEN
    assert d["competitors_placed"] == d["competitors_total"]
    assert d["competitors_missing_from_heats"] == 0
    assert d["competitors_non_heat_only"] == 0
    assert d["competitors_no_events"] == 0
    assert d["competitors_missing_sample"] == []


@pytest.mark.sev1
def test_a_competitor_pulled_from_every_heat_is_counted_and_named(app):
    """The panel's entire reason to exist: lose someone, and it says WHO.

    Removes one placed college competitor from every heat they stand in,
    rebuilds the status, and requires them counted in missing_from_heats.
    The name must be retrievable through the missing set; the 10-name
    display sample is order-dependent with 28 missing, so membership is
    asserted against the population, not the sample.
    """
    import json as _json

    from database import db
    from models.competitor import CollegeCompetitor
    from models.event import Event
    from models.heat import Heat

    placed_ids, _ = _college_ground_truth()
    victim_id = min(placed_ids)
    victim = db.session.get(CollegeCompetitor, victim_id)

    heats = (
        Heat.query.join(Event)
        .filter(Event.tournament_id == TID, Event.event_type == "college")
        .all()
    )
    removed_from = 0
    for h in heats:
        ids = [c for c in h.get_competitors() if int(c) != victim_id]
        if len(ids) != len(h.get_competitors()):
            h.competitors = _json.dumps(ids)
            removed_from += 1
    assert removed_from > 0, "victim was not standing in any heat"
    db.session.commit()

    f = _status()["friday"]
    assert f["competitors_placed"] == EXPECTED_FRIDAY_PLACED - 1
    assert f["competitors_missing_from_heats"] == EXPECTED_FRIDAY_MISSING + 1

    _, missing_now = _college_ground_truth()
    assert victim_id in missing_now, f"{victim.name} vanished without being counted"


@pytest.mark.sev1
def test_a_scratched_competitor_left_in_heats_does_not_inflate_placed(app):
    """The '38 / 37' regression, on real data.

    Scratching a placed competitor without cleaning their heats must shrink
    both the numerator and the denominator, never push placed above total.
    The active-population bound in _day_status is what this locks.
    """
    from database import db
    from models.competitor import CollegeCompetitor

    placed_ids, _ = _college_ground_truth()
    victim = db.session.get(CollegeCompetitor, max(placed_ids))
    victim.status = "scratched"
    db.session.commit()

    f = _status()["friday"]
    assert f["competitors_total"] == EXPECTED_ACTIVE_COLLEGE - 1
    assert f["competitors_placed"] == EXPECTED_FRIDAY_PLACED - 1
    assert f["competitors_placed"] <= f["competitors_total"]
    assert f["competitors_missing_from_heats"] == EXPECTED_FRIDAY_MISSING


# The three tests below stage conditions the 2026 data happens not to contain.
# Each corresponds to a mutant that survived the battery's first run purely
# because the mirror is data-equivalent on that path: no heat stores string
# ids, no active competitor has an empty entry list, and nobody is entered
# only in events that do not exist on their day. Same lesson as item 1's M7:
# the real data is not a sufficient oracle, so the missing cases are built.


@pytest.mark.sev1
def test_string_ids_in_a_heats_json_do_not_unplace_its_competitors(app):
    """Heat.competitors is free-form JSON and the id-shape is not enforced;
    events_entered already mixes strings and numbers on this very database
    (c22). A reader that stops coercing would silently unplace every
    competitor in any heat written with string ids, with no error anywhere.
    """
    import json as _json

    from database import db
    from models.event import Event
    from models.heat import Heat

    heats = (
        Heat.query.join(Event)
        .filter(Event.tournament_id == TID, Event.event_type == "college")
        .all()
    )
    # Every heat, not a sample: a competitor standing in six events survives
    # a partial rewrite through their other heats, and the mutant this test
    # exists to kill (int() coercion dropped) walks free.
    rewritten = 0
    for h in heats:
        ids = h.get_competitors()
        if ids:
            h.competitors = _json.dumps([str(i) for i in ids])
            rewritten += 1
    assert rewritten > 0
    db.session.commit()

    f = _status()["friday"]
    assert f["competitors_placed"] == EXPECTED_FRIDAY_PLACED, (
        "string ids in heat JSON unplaced competitors who are standing in heats"
    )
    assert f["competitors_missing_from_heats"] == EXPECTED_FRIDAY_MISSING


@pytest.mark.sev1
def test_an_empty_entry_list_reads_as_no_events_not_as_a_scheduling_bug(app):
    """A competitor who entered nothing is a registration fact, not a heat
    generation failure. Filing them under missing_from_heats would page the
    operator about a bug that does not exist.
    """
    from database import db
    from models.competitor import CollegeCompetitor

    _, missing_ids = _college_ground_truth()
    comp = db.session.get(CollegeCompetitor, min(missing_ids))
    comp.events_entered = "[]"
    db.session.commit()

    f = _status()["friday"]
    assert f["competitors_no_events"] == 1
    assert f["competitors_missing_from_heats"] == EXPECTED_FRIDAY_MISSING - 1
    assert f["competitors_placed"] == EXPECTED_FRIDAY_PLACED
    assert (
        f["competitors_placed"]
        + f["competitors_non_heat_only"]
        + f["competitors_no_events"]
        + f["competitors_missing_from_heats"]
        == f["competitors_total"]
    )


@pytest.mark.sev1
def test_entries_matching_no_event_on_this_day_read_as_no_events(app):
    """The else branch: entered events exist but none of them belong to this
    day (the service's own example is a pro-only name tallied on Friday).
    Misfiling these as missing_from_heats would make the bug-surface count
    unreadable at any tournament where the days share a registration form.
    """
    import json as _json

    from database import db
    from models.competitor import CollegeCompetitor

    _, missing_ids = _college_ground_truth()
    comp = db.session.get(CollegeCompetitor, max(missing_ids))
    comp.events_entered = _json.dumps(["Springboard"])  # pro-only event name
    db.session.commit()

    f = _status()["friday"]
    assert f["competitors_no_events"] == 1
    assert f["competitors_missing_from_heats"] == EXPECTED_FRIDAY_MISSING - 1
    assert f["competitors_placed"] == EXPECTED_FRIDAY_PLACED


# ===========================================================================
# Item 3 of 4: Stock Saw solos stranded on stand 8.
#
#     Reported symptom: the race-day printout showed six straight solo heats
#     on stand 8, so the judges set up the same physical stand every heat
#     with no off-stand to prepare. V2.14.13 wired the solo-stand rebalance
#     into five route sites and missed the authoritative one:
#     services/scratch_cascade.py::execute_cascade. V2.14.15 wired it there.
#
#     Measured on the real mirror: the race-day layout is STILL LIVE DATA.
#     College Stock Saw Men (event 20) holds six consecutive solos, heats 1
#     through 6, every one on stand 8, with three pairs behind them on 7+8.
#     The women's event (21) holds five solos, all on stand 8. The rebalance
#     fix runs at generation and mutation time and nothing has mutated these
#     rows since it shipped, so the 2026 pattern sits in production exactly
#     as it was printed. Same disease as the stale birling brackets, item 4:
#     a generator fix is not a data fix.
#
#     Verified by probe before writing these tests: scratching Mateo Angel
#     (id 30, the stand-7 seat of pair heat 7) through the real cascade
#     route removed him, left Dustin Haley as a solo, and re-alternated
#     every solo in the event to 7,8,7,8,7,8,7. One real scratch repairs the
#     whole event retroactively. The wiring is what these tests lock.
# ===========================================================================

# Measured layout of college Stock Saw Men (event 20) on the mirror.
STOCK_SAW_M = 20
STOCK_SAW_F = 21
SINGLE_BUCK_M = 15
# heats 1-6: solo competitor ids, in heat order, every one assigned stand 8.
SS_M_SOLOS_BEFORE = {1: 100062, 2: 100059, 3: 100058, 4: 100051, 5: 100046, 6: 100044}
# heats 7-9: pairs (stand-7 seat, stand-8 seat).
SS_M_PAIRS_BEFORE = {7: (100030, 100043), 8: (100035, 100041), 9: (100037, 100038)}
SS_SCRATCH_ID = 100030   # Mateo Angel, stand-7 seat of heat 7
SS_SURVIVOR_ID = 100043  # Dustin Haley, stand-8 seat of heat 7


def _stock_saw_layout(event_id):
    """[(heat_number, [competitor ids], {id: stand})] in run+heat order."""
    from database import db
    from models.heat import Heat

    db.session.expire_all()
    out = []
    for h in (
        Heat.query.filter_by(event_id=event_id)
        .order_by(Heat.run_number, Heat.heat_number)
        .all()
    ):
        out.append(
            (h.heat_number, [int(c) for c in h.get_competitors()],
             {int(k): v for k, v in h.get_stand_assignments().items()})
        )
    return out


def _scratch_through_the_real_route(client, comp_id, comp_type):
    """GET the preview, check every effect, POST the confirm. The route
    aborts 400 without competitor_type because pro and college ids collide
    on this database; both real entry points send it."""
    r = client.get(
        f"/scoring/{TID}/competitor/{comp_id}/scratch-preview"
        f"?competitor_type={comp_type}",
        headers={"Accept": "application/json"},
    )
    assert r.status_code == 200, r.status_code
    effects = r.get_json()["effects"]
    form = {"effect_count": str(len(effects)), "competitor_type": comp_type}
    for i, e in enumerate(effects):
        form[f"effect_type_{i}"] = e["effect_type"]
        form[f"affected_entity_id_{i}"] = str(e["affected_entity_id"])
        form[f"effect_checked_{i}"] = "on"
    r2 = client.post(
        f"/scoring/{TID}/competitor/{comp_id}/scratch-confirm", data=form
    )
    assert r2.status_code in (200, 302), r2.status_code


def _assert_solo_alternation(layout, event_label):
    """The DOMAIN_CONTRACT: solos alternate 7,8,7,8... in heat order within a
    run, starting at 7, pairs always occupy exactly {7, 8}, and no stand
    outside [7, 8] ever appears in Stock Saw."""
    expected_next = 7
    for heat_number, comps, stands in layout:
        assert set(stands.values()) <= {7, 8}, (
            f"{event_label} heat {heat_number} uses a stand outside 7/8: {stands}"
        )
        if len(comps) == 1:
            got = stands.get(comps[0])
            assert got == expected_next, (
                f"{event_label} heat {heat_number}: solo on stand {got}, "
                f"alternation expected {expected_next}"
            )
            expected_next = 15 - expected_next  # 7 <-> 8
        elif len(comps) == 2:
            assert set(stands.values()) == {7, 8}, (
                f"{event_label} heat {heat_number}: pair not on 7+8: {stands}"
            )


@pytest.mark.sev1
def test_the_race_day_stand_8_pattern_is_still_live_production_data(app):
    """Population guard and a finding in its own right: the mirror holds the
    exact printout pattern from April 2026. Six consecutive Stock Saw solos,
    all on stand 8. If this test starts failing because the data was
    repaired, update the constants; if it fails any other way, the mirror
    is not the world these tests were written against.
    """
    layout = {hn: (c, s) for hn, c, s in _stock_saw_layout(STOCK_SAW_M)}
    for hn, comp_id in SS_M_SOLOS_BEFORE.items():
        comps, stands = layout[hn]
        assert comps == [comp_id]
        assert stands == {comp_id: 8}, (
            f"heat {hn}: expected the race-day stand-8 stranding, got {stands}"
        )
    for hn, (seat7, seat8) in SS_M_PAIRS_BEFORE.items():
        comps, stands = layout[hn]
        assert sorted(comps) == sorted([seat7, seat8])
        assert stands == {seat7: 7, seat8: 8}


@pytest.mark.sev1
def test_a_cascade_scratch_rebalances_the_whole_stock_saw_event(app, client):
    """THE 2026 WIRING GAP. Scratching through the authoritative cascade
    path must trigger the solo-stand rebalance, exactly as the five wired
    route sites do. Before V2.14.15 this path left the survivor on whatever
    stand the scratched partner left him, and the printout read six straight
    heats on stand 8.

    The rebalance normalizes the entire event, so this single scratch must
    also retroactively repair the six stranded solos. Asserted against the
    documented contract (alternate from 7 within each run), not against a
    hardcoded stand list.
    """
    _scratch_through_the_real_route(client, SS_SCRATCH_ID, "college")

    layout = _stock_saw_layout(STOCK_SAW_M)
    by_heat = {hn: (c, s) for hn, c, s in layout}

    survivor_comps, survivor_stands = by_heat[7]
    assert survivor_comps == [SS_SURVIVOR_ID], (
        f"heat 7 after scratching {SS_SCRATCH_ID}: {survivor_comps}"
    )
    assert SS_SCRATCH_ID not in survivor_stands, (
        "the scratched man is gone from the heat but still holds a stand"
    )
    _assert_solo_alternation(layout, "Stock Saw M")

    # The untouched pairs keep their exact seats. A rebalance that churns
    # already-correct pairs passes a set-based check ({7,8} either way) and
    # still reprints every judge sheet for no reason.
    for hn in (8, 9):
        seat7, seat8 = SS_M_PAIRS_BEFORE[hn]
        assert by_heat[hn][1] == {seat7: 7, seat8: 8}, (
            f"heat {hn}: pair orientation churned to {by_heat[hn][1]}"
        )

    # The concrete race-day complaint, stated directly: no two consecutive
    # solo heats on the same physical stand anywhere in the event.
    solo_stands = [
        stands[comps[0]] for _, comps, stands in layout if len(comps) == 1
    ]
    for a, b in zip(solo_stands, solo_stands[1:]):
        assert a != b, f"consecutive solos share stand {a}: {solo_stands}"


@pytest.mark.sev1
def test_a_mens_scratch_does_not_touch_the_womens_stock_saw(app, client):
    """The cascade rebalances the events it MUTATED, not every Stock Saw in
    the tournament. The women's event carries the same five-solos-on-8
    pattern; a men's scratch has no business rewriting it."""
    before = _stock_saw_layout(STOCK_SAW_F)
    _scratch_through_the_real_route(client, SS_SCRATCH_ID, "college")
    assert _stock_saw_layout(STOCK_SAW_F) == before


@pytest.mark.sev1
def test_a_scratch_in_another_event_leaves_stock_saw_and_its_own_stands_alone(
    app, client
):
    """Two scopes at once. Scratching a Single Buck man must not wake the
    Stock Saw rebalance (event scoping), and the rebalance must not touch
    Single Buck's own stands (the _is_stock_saw early return: Single Buck
    legitimately uses stands outside 7/8, and a rebalance that 'repaired'
    it onto 7/8 would corrupt the event while looking like a fix).
    """
    from models.heat import Heat

    ss_before = _stock_saw_layout(STOCK_SAW_M)

    # 61 stands only in Single Buck heat 3 (pair with 41) among heat events.
    sb_before = {
        h.heat_number: dict(h.get_stand_assignments())
        for h in Heat.query.filter_by(event_id=SINGLE_BUCK_M).all()
    }
    _scratch_through_the_real_route(client, 100061, "college")

    assert _stock_saw_layout(STOCK_SAW_M) == ss_before, (
        "a Single Buck scratch rewrote Stock Saw stand assignments"
    )

    from database import db

    db.session.expire_all()
    for h in Heat.query.filter_by(event_id=SINGLE_BUCK_M).all():
        after = {k: v for k, v in h.get_stand_assignments().items() if k != "100061"}
        expected = {k: v for k, v in sb_before[h.heat_number].items() if k != "100061"}
        assert after == expected, (
            f"Single Buck heat {h.heat_number} stands changed for survivors: "
            f"{sb_before[h.heat_number]} -> {h.get_stand_assignments()}"
        )


@pytest.mark.sev1
def test_run_two_alternation_starts_fresh_at_stand_7(app, client):
    """Runs are balanced independently (the generator's documented rule):
    run 2's first solo starts back at 7 no matter where run 1 ended. The
    real event has one run, so the second is staged; a rebalance that
    carries alternation across the run boundary passes every single-run
    test and still hands the crew a back-to-back on race day.
    """
    import json as _json

    from database import db
    from models.heat import Heat

    template = Heat.query.filter_by(event_id=STOCK_SAW_M, heat_number=1).first()
    for hn, comp_id in ((1, 100035), (2, 100037)):
        db.session.add(
            Heat(
                event_id=STOCK_SAW_M,
                heat_number=hn,
                run_number=2,
                competitors=_json.dumps([comp_id]),
                stand_assignments=_json.dumps({str(comp_id): 8}),
                status=template.status,
            )
        )
    db.session.commit()

    _scratch_through_the_real_route(client, SS_SCRATCH_ID, "college")

    layout = _stock_saw_layout(STOCK_SAW_M)
    run2 = [
        (hn, comps, stands)
        for hn, comps, stands in layout[-2:]
    ]
    assert [stands[comps[0]] for _, comps, stands in run2] == [7, 8], (
        f"run 2 solos must restart alternation at 7: {run2}"
    )


@pytest.mark.sev1
def test_completed_heats_keep_their_recorded_stands(app, client):
    """Completed heats are historical record (Codex P2, V2.14.15): their
    stands match what was actually run and the score sheet is keyed to them.
    The rebalance must walk them, advancing the alternation counter, without
    rewriting them. A mid-event scratch that silently edits past stand
    assignments corrupts the paper trail the judges reconcile against.

    Heats 1 and 2 are marked completed where they sit (both solos on stand
    8, the race-day layout). After the scratch: 1 and 2 untouched on 8, and
    the counter they consumed (two flips from 7) leaves the pending solos
    starting back at 7.
    """
    from database import db
    from models.heat import Heat

    for hn in (1, 2):
        h = Heat.query.filter_by(
            event_id=STOCK_SAW_M, heat_number=hn, run_number=1
        ).first()
        h.status = "completed"
    db.session.commit()

    _scratch_through_the_real_route(client, SS_SCRATCH_ID, "college")

    by_heat = {hn: (c, s) for hn, c, s in _stock_saw_layout(STOCK_SAW_M)}
    for hn in (1, 2):
        comp_id = SS_M_SOLOS_BEFORE[hn]
        assert by_heat[hn][1] == {comp_id: 8}, (
            f"completed heat {hn} was rewritten: {by_heat[hn][1]}"
        )
    # Two completed solos consumed 7 then 8; pending solos resume at 7.
    expected = {3: 7, 4: 8, 5: 7, 6: 8}
    for hn, stand in expected.items():
        comp_id = SS_M_SOLOS_BEFORE[hn]
        assert by_heat[hn][1] == {comp_id: stand}, (
            f"pending heat {hn}: expected stand {stand}, got {by_heat[hn][1]}"
        )
    assert by_heat[7][1] == {SS_SURVIVOR_ID: 7}


# ===========================================================================
# Item 4 of 4: stale birling brackets.
#
#     Reported symptom: V2.14.14 (April 23) fixed the generator to produce
#     compact non-power-of-two brackets, but generators do not auto-rerun on
#     deploy, so the existing Event.payouts JSON kept the old power-of-two
#     shape and the printouts still showed seeds 8 and 9 stacked into a
#     phantom W1_8 match. On April 25 the Men's (12) and Women's (9)
#     brackets were hand-built as PDFs. The post-event remedy is
#     rebuild_if_stale_shape: an auto-migration wired into the manage page
#     GET and the print path, gated so it never tears down a bracket with
#     judge-entered results.
#
#     Measured on the real mirror: BOTH brackets are still stale at rest,
#     fifteen months later. Men's: 8 round-1 matches with 4 byes for 12
#     entrants (compact is 6 pairs, 0 byes). Women's: 8 matches with 7 byes
#     for 9 entrants, and W1_8 is the phantom itself, Evvy Chatfield (16)
#     stacked against Teagan Wigen (31), where the compact shape gives the
#     top seed the single bye. The repair exists and runs on touch; nothing
#     repairs at rest. Item 3's stand-8 finding, same disease, second organ.
#
#     Gate 3 before writing tests: one authenticated GET of each manage page
#     rewrote both payouts to the compact shape with seeding preserved.
# ===========================================================================

BIRLING_M = 28   # 12 entrants
BIRLING_F = 29   # 9 entrants
BIRLING_F_PHANTOM = ("W1_8", 16, 100031)  # stacked match; 16 is an era-1 GHOST and does not shift
BIRLING_F_TOP_SEED = 5                 # Mackenzie Breitner, gets the compact bye


def _bracket_shape(event_id):
    """(entrants, round1_matches, byes, seeding, {match_id: (c1, c2)})."""
    import json as _json

    from database import db
    from models.event import Event

    db.session.expire_all()
    d = _json.loads(db.session.get(Event, event_id).payouts or "{}")
    r1 = ((d.get("bracket") or {}).get("winners") or [[]])[0]
    pairs = {
        m["match_id"]: (m.get("competitor1"), m.get("competitor2"))
        for m in r1
    }
    return (
        len(d.get("competitors") or []),
        len(r1),
        sum(1 for m in r1 if m.get("is_bye")),
        d.get("seeding"),
        pairs,
    )


def _write_payouts(event_id, mutate):
    """Load, mutate, and store an event's payouts JSON."""
    import json as _json

    from database import db
    from models.event import Event

    ev = db.session.get(Event, event_id)
    d = _json.loads(ev.payouts or "{}")
    mutate(d)
    ev.payouts = _json.dumps(d)
    db.session.commit()


@pytest.mark.sev1
def test_both_race_day_brackets_are_still_stale_at_rest(app):
    """Population guard and the finding itself: the April 2026 bracket JSON
    sits in production unchanged, phantom match included."""
    n, r1, byes, seeding, pairs = _bracket_shape(BIRLING_M)
    assert (n, r1, byes) == (12, 8, 4), f"men's bracket: {(n, r1, byes)}"

    n, r1, byes, seeding, pairs = _bracket_shape(BIRLING_F)
    assert (n, r1, byes) == (9, 8, 7), f"women's bracket: {(n, r1, byes)}"
    mid, c1, c2 = BIRLING_F_PHANTOM
    assert pairs[mid] == (c1, c2), (
        f"the phantom stacked match is gone from the mirror: {pairs.get(mid)}"
    )


@pytest.mark.sev1
def test_opening_the_manage_page_rebuilds_both_stale_brackets(app, client):
    """THE DATA FIX. A plain GET must migrate the power-of-two JSON to the
    compact shape: 12 entrants become 6 pairs with no byes, 9 entrants
    become 1 bye plus 4 pairs with the bye going to the TOP seed, and the
    seeding order survives so the judges reprint the matchups they expect.
    """
    seed_m_before = _bracket_shape(BIRLING_M)[3]
    seed_f_before = _bracket_shape(BIRLING_F)[3]

    for eid in (BIRLING_M, BIRLING_F):
        r = client.get(f"/scheduling/{TID}/event/{eid}/birling")
        assert r.status_code == 200

    n, r1, byes, seeding, pairs = _bracket_shape(BIRLING_M)
    assert (n, r1, byes) == (12, 6, 0), f"men's not compact: {(n, r1, byes)}"
    assert seeding == seed_m_before, "men's seeding was not preserved"

    n, r1, byes, seeding, pairs = _bracket_shape(BIRLING_F)
    assert (n, r1, byes) == (9, 5, 1), f"women's not compact: {(n, r1, byes)}"
    assert seeding == seed_f_before, "women's seeding was not preserved"
    bye_pairs = [p for p in pairs.values() if p[1] is None]
    assert bye_pairs == [(BIRLING_F_TOP_SEED, None)], (
        f"the compact bye must go to the top seed, got {bye_pairs}"
    )
    assert BIRLING_F_PHANTOM[0] not in pairs or pairs[
        BIRLING_F_PHANTOM[0]
    ] != BIRLING_F_PHANTOM[1:], "the phantom stacked match survived the rebuild"


@pytest.mark.sev1
def test_the_print_path_repairs_the_bracket_too(app, client):
    """The 2026 failure surfaced on PAPER. The print route reaches the same
    auto-migration, so a judge who prints without ever opening the manage
    page still gets the compact bracket, not the phantom."""
    r = client.get(f"/scheduling/{TID}/event/{BIRLING_F}/birling/print-blank")
    assert r.status_code == 200
    n, r1, byes, _seeding, _pairs = _bracket_shape(BIRLING_F)
    assert (n, r1, byes) == (9, 5, 1), (
        f"print route served without repairing the stale bracket: {(n, r1, byes)}"
    )


@pytest.mark.sev1
def test_a_recorded_fall_blocks_the_rebuild(app, client):
    """Judge work is sacred. Birling is best-of-3, so a single recorded fall
    in the phantom match is real state, and the migration must refuse to
    tear the bracket down under it, winner or no winner."""
    def add_fall(d):
        for m in d["bracket"]["winners"][0]:
            if m["match_id"] == BIRLING_F_PHANTOM[0]:
                m["falls"] = [16]
    _write_payouts(BIRLING_F, add_fall)

    r = client.get(f"/scheduling/{TID}/event/{BIRLING_F}/birling")
    assert r.status_code == 200

    n, r1, byes, _seeding, pairs = _bracket_shape(BIRLING_F)
    assert (n, r1, byes) == (9, 8, 7), (
        "a bracket with a recorded fall was rebuilt underneath the judge"
    )
    assert pairs[BIRLING_F_PHANTOM[0]] == BIRLING_F_PHANTOM[1:]


@pytest.mark.sev1
def test_recorded_placements_block_the_rebuild(app, client):
    """Same gate, other end of the day: a bracket with final placements is
    history, not a layout problem."""
    def add_placement(d):
        d["placements"] = {"9": 1}
    _write_payouts(BIRLING_M, add_placement)

    r = client.get(f"/scheduling/{TID}/event/{BIRLING_M}/birling")
    assert r.status_code == 200
    n, r1, byes, _s, _p = _bracket_shape(BIRLING_M)
    assert (n, r1, byes) == (12, 8, 4), (
        "a bracket with recorded placements was rebuilt"
    )


@pytest.mark.sev1
def test_the_repair_happens_once_not_on_every_page_view(app, client):
    """After the migration the bracket is compact, the staleness test is
    False, and further GETs must not rewrite the stored JSON at all. A
    repair that re-fires on every view churns payouts forever and makes
    every page load a write."""
    from database import db
    from models.event import Event

    r = client.get(f"/scheduling/{TID}/event/{BIRLING_F}/birling")
    assert r.status_code == 200
    db.session.expire_all()
    first = db.session.get(Event, BIRLING_F).payouts

    r = client.get(f"/scheduling/{TID}/event/{BIRLING_F}/birling")
    assert r.status_code == 200
    db.session.expire_all()
    assert db.session.get(Event, BIRLING_F).payouts == first, (
        "second GET rewrote payouts after the bracket was already compact"
    )
