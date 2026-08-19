"""Regression coverage for scratch undo tokens on visible roster pages."""

from __future__ import annotations

import json
from html.parser import HTMLParser

from sqlalchemy import event as sqlalchemy_event

from database import db
from tests.conftest import (
    make_college_competitor,
    make_event,
    make_event_result,
    make_heat,
    make_pro_competitor,
    make_team,
    make_tournament,
)


class _FormParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.forms = []
        self._current = None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "form":
            self._current = {
                "action": attributes.get("action", ""),
                "inputs": {},
            }
        elif tag == "input" and self._current is not None:
            name = attributes.get("name")
            if name:
                self._current["inputs"][name] = attributes.get("value", "")

    def handle_endtag(self, tag):
        if tag == "form" and self._current is not None:
            self.forms.append(self._current)
            self._current = None


def _undo_forms(response, competitor_id):
    parser = _FormParser()
    parser.feed(response.get_data(as_text=True))
    path_suffix = f"/competitor/{competitor_id}/scratch-undo"
    return [form for form in parser.forms if form["action"].endswith(path_suffix)]


def _undo_form_token(response, competitor_id):
    form = next(iter(_undo_forms(response, competitor_id)))
    return form["inputs"].get("expected_undo_token")


def _seed_scratch_audit(
    session,
    competitor,
    competitor_type,
    *,
    snapshot=None,
):
    from models.audit_log import AuditLog
    from services.scratch_cascade import _scratch_post_state_sha256

    if snapshot is None:
        snapshot = {
            "competitor_type": competitor_type,
            "results": [],
            "heats": [],
            "relay_teams": [],
        }

    entry = AuditLog(
        action="competitor_scratched",
        entity_type="competitor",
        entity_id=competitor.id,
        details_json=json.dumps({
            "scratch_snapshot": snapshot,
            "scratch_post_state_sha256": _scratch_post_state_sha256(
                competitor,
                snapshot,
            ),
        }),
    )
    session.add(entry)
    session.flush()
    return entry


def test_empty_undo_token_batch_preserves_mapping_contract(db_session):
    from services.scratch_cascade import (
        find_undoable_scratch_tokens,
        find_undoable_scratches,
    )

    assert find_undoable_scratch_tokens([], "pro") == {}
    assert find_undoable_scratches([], "pro") == set()


def test_malformed_snapshot_lists_hide_the_undo_control(db_session):
    from models.audit_log import AuditLog
    from services.scratch_cascade import find_undoable_scratch_tokens

    tournament = make_tournament(db_session, status="active")
    competitor = make_pro_competitor(
        db_session,
        tournament,
        "Malformed Scratch Audit",
        status="scratched",
    )
    db_session.add(AuditLog(
        action="competitor_scratched",
        entity_type="competitor",
        entity_id=competitor.id,
        details_json=json.dumps({
            "scratch_snapshot": {
                "competitor_type": "pro",
                "results": None,
                "heats": "not-a-list",
                "relay_teams": {},
            },
            "scratch_post_state_sha256": "0" * 64,
        }),
    ))
    db_session.flush()

    assert find_undoable_scratch_tokens([competitor.id], "pro") == {}


def test_batched_undo_tokens_are_read_only_and_avoid_n_plus_one(db_session):
    from services.scratch_cascade import (
        find_undoable_scratch_tokens,
        find_undoable_scratches,
        scratch_undo_token,
    )

    tournament = make_tournament(db_session, status="active")
    event = make_event(db_session, tournament, "Undo Query Event", event_type="pro")
    competitors = [
        make_pro_competitor(
            db_session,
            tournament,
            f"Scratched Pro {index}",
            status="scratched",
        )
        for index in range(5)
    ]
    snapshots = []
    entries = []
    for index, competitor in enumerate(competitors, start=1):
        result = make_event_result(db_session, event, competitor)
        heat = make_heat(
            db_session,
            event,
            heat_number=index,
            competitors=[competitor.id],
        )
        snapshot = {
            "competitor_type": "pro",
            "results": [{"id": result.id}],
            "heats": [{"heat_id": heat.id}],
            "relay_teams": [],
        }
        snapshots.append(snapshot)
        entries.append(_seed_scratch_audit(
            db_session,
            competitor,
            "pro",
            snapshot=snapshot,
        ))
    entries[0] = _seed_scratch_audit(
        db_session,
        competitors[0],
        "pro",
        snapshot=snapshots[0],
    )
    expected = {
        competitor.id: scratch_undo_token(entry)
        for competitor, entry in zip(competitors, entries)
    }
    db_session.flush()

    def measure(competitor_ids):
        db_session.expire_all()
        statements = []

        def capture_statement(_conn, _cursor, statement, _params, _context, _many):
            statements.append(" ".join(statement.lower().split()))

        sqlalchemy_event.listen(db.engine, "before_cursor_execute", capture_statement)
        try:
            tokens = find_undoable_scratch_tokens(competitor_ids, "pro")
        finally:
            sqlalchemy_event.remove(
                db.engine,
                "before_cursor_execute",
                capture_statement,
            )
        return tokens, [
            statement for statement in statements if statement.startswith("select")
        ], statements

    single_tokens, single_selects, _ = measure([competitors[0].id])
    tokens, all_selects, statements = measure(
        [competitor.id for competitor in competitors]
    )

    audit_selects = [
        statement
        for statement in all_selects
        if " from audit_logs " in f" {statement} "
    ]
    assert single_tokens == {competitors[0].id: expected[competitors[0].id]}
    assert tokens == expected
    assert len(all_selects) == len(single_selects)
    assert len(audit_selects) == 2
    assert not any(
        statement.startswith(("insert", "update", "delete"))
        for statement in statements
    )
    assert find_undoable_scratches(expected, "pro") == set(expected)


def test_undo_rejects_heat_scored_after_scratch(
    admin_user,
    db_session,
):
    from services.scratch_cascade import (
        compute_scratch_effects,
        execute_cascade,
        reverse_cascade,
    )

    tournament = make_tournament(db_session, status="active")
    scratched = make_pro_competitor(
        db_session,
        tournament,
        "Scored Heat Undo",
    )
    survivor = make_pro_competitor(
        db_session,
        tournament,
        "Scored Heat Survivor",
    )
    event = make_event(db_session, tournament, "Underhand", event_type="pro")
    heat = make_heat(
        db_session,
        event,
        competitors=[scratched.id, survivor.id],
        stand_assignments={str(scratched.id): 1, str(survivor.id): 2},
    )
    make_event_result(db_session, event, scratched)
    survivor_result = make_event_result(db_session, event, survivor)
    db_session.flush()

    scratch_result = execute_cascade(
        scratched,
        compute_scratch_effects(scratched, tournament),
        judge_user_id=admin_user.id,
        tournament=tournament,
    )
    db_session.flush()
    assert heat.get_competitors() == [survivor.id]
    assert heat.status == "pending"

    heat.status = "completed"
    survivor_result.status = "completed"
    survivor_result.result_value = 30.0
    db_session.flush()

    undo_result = reverse_cascade(
        scratched.id,
        judge_user_id=admin_user.id,
        tournament=tournament,
        competitor_type="pro",
        expected_undo_token=scratch_result["undo_token"],
    )
    db_session.flush()

    assert undo_result["success"] is False
    assert "changed" in undo_result["message"].lower()
    assert scratched.status == "scratched"
    assert heat.get_competitors() == [survivor.id]
    assert heat.status == "completed"
    assert survivor_result.status == "completed"
    assert survivor_result.result_value == 30.0


def test_undo_still_rejects_a_roster_change_after_scratch(
    admin_user,
    db_session,
):
    from services.scratch_cascade import (
        compute_scratch_effects,
        execute_cascade,
        reverse_cascade,
    )

    tournament = make_tournament(db_session, status="active")
    scratched = make_pro_competitor(db_session, tournament, "Moved Heat Undo")
    survivor = make_pro_competitor(db_session, tournament, "Moved Heat Survivor")
    event = make_event(db_session, tournament, "Standing Block", event_type="pro")
    heat = make_heat(
        db_session,
        event,
        competitors=[scratched.id, survivor.id],
        stand_assignments={str(scratched.id): 1, str(survivor.id): 2},
    )
    make_event_result(db_session, event, scratched)
    make_event_result(db_session, event, survivor)
    db_session.flush()

    scratch_result = execute_cascade(
        scratched,
        compute_scratch_effects(scratched, tournament),
        judge_user_id=admin_user.id,
        tournament=tournament,
    )
    db_session.flush()

    heat.set_roster("pro", [survivor.id], {str(survivor.id): 3})
    db_session.flush()

    undo_result = reverse_cascade(
        scratched.id,
        judge_user_id=admin_user.id,
        tournament=tournament,
        competitor_type="pro",
        expected_undo_token=scratch_result["undo_token"],
    )
    db_session.flush()

    assert undo_result["success"] is False
    assert "changed" in undo_result["message"].lower()
    assert scratched.status == "scratched"
    assert heat.get_competitors() == [survivor.id]
    assert heat.get_stand_assignments() == {str(survivor.id): 3}


def test_pro_dashboard_undo_form_submits_current_token(
    auth_client,
    db_session,
):
    from services.scratch_cascade import scratch_undo_token

    tournament = make_tournament(db_session, status="active")
    competitor = make_pro_competitor(
        db_session,
        tournament,
        "Visible Pro Undo",
        status="scratched",
    )
    entry = _seed_scratch_audit(db_session, competitor, "pro")
    expected_token = scratch_undo_token(entry)

    response = auth_client.get(f"/tournament/{tournament.id}/pro")

    assert response.status_code == 200
    assert _undo_form_token(response, competitor.id) == expected_token


def test_pro_dashboard_hides_undo_after_post_scratch_state_changes(
    auth_client,
    db_session,
):
    tournament = make_tournament(db_session, status="active")
    competitor = make_pro_competitor(
        db_session,
        tournament,
        "Changed Pro Undo",
        status="scratched",
    )
    _seed_scratch_audit(db_session, competitor, "pro")
    competitor.partners = json.dumps({"changed": "after-scratch"})
    db_session.flush()

    response = auth_client.get(f"/tournament/{tournament.id}/pro")

    assert response.status_code == 200
    assert _undo_forms(response, competitor.id) == []


def test_college_team_roster_undo_form_submits_current_token(
    auth_client,
    db_session,
):
    from services.scratch_cascade import scratch_undo_token

    tournament = make_tournament(db_session, status="active")
    team = make_team(db_session, tournament)
    competitor = make_college_competitor(
        db_session,
        tournament,
        team,
        "Visible College Undo",
        status="scratched",
    )
    entry = _seed_scratch_audit(db_session, competitor, "college")
    expected_token = scratch_undo_token(entry)

    response = auth_client.get(
        f"/registration/{tournament.id}/college/team/{team.id}"
    )

    assert response.status_code == 200
    assert _undo_form_token(response, competitor.id) == expected_token


def test_college_team_roster_hides_undo_after_post_scratch_state_changes(
    auth_client,
    db_session,
):
    tournament = make_tournament(db_session, status="active")
    team = make_team(db_session, tournament)
    competitor = make_college_competitor(
        db_session,
        tournament,
        team,
        "Changed College Undo",
        status="scratched",
    )
    _seed_scratch_audit(db_session, competitor, "college")
    competitor.partners = json.dumps({"changed": "after-scratch"})
    db_session.flush()

    response = auth_client.get(
        f"/registration/{tournament.id}/college/team/{team.id}"
    )

    assert response.status_code == 200
    assert _undo_forms(response, competitor.id) == []


def test_college_team_roster_hides_undo_from_registrar(app, db_session):
    from models.user import User

    tournament = make_tournament(db_session, status="active")
    team = make_team(db_session, tournament)
    competitor = make_college_competitor(
        db_session,
        tournament,
        team,
        "Registrar Cannot Undo",
        status="scratched",
    )
    _seed_scratch_audit(db_session, competitor, "college")
    registrar = User(username="undo_registrar", role="registrar")
    registrar.set_password("testpass")
    db_session.add(registrar)
    db_session.flush()

    registrar_client = app.test_client()
    with registrar_client.session_transaction() as session:
        session["_user_id"] = str(registrar.id)

    response = registrar_client.get(
        f"/registration/{tournament.id}/college/team/{team.id}"
    )

    assert response.status_code == 200
    assert _undo_forms(response, competitor.id) == []
