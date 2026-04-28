"""Tests for the domain conflict review board."""
from __future__ import annotations

import json
import uuid
from pathlib import Path


def _sample_registry() -> dict:
    return {
        "schema_version": 1,
        "conflicts": [
            {
                "id": "alpha-conflict",
                "title": "Alpha Conflict",
                "category": "partnered_events",
                "severity": "critical",
                "status": "needs_decision",
                "contract_rule": "Alpha must follow the contract.",
                "conflicting_sources": [
                    {
                        "file": "FlightLogic.md",
                        "line": 10,
                        "text": "Old alpha wording.",
                    }
                ],
                "proposed_resolution": "Accept the contract.",
                "decision": "",
                "decision_note": "",
                "test_coverage": [],
            },
            {
                "id": "beta-conflict",
                "title": "Beta Conflict",
                "category": "production_parity",
                "severity": "high",
                "status": "implemented",
                "contract_rule": "Beta must be verified.",
                "conflicting_sources": [],
                "proposed_resolution": "Keep the implementation.",
                "decision": "Implemented.",
                "decision_note": "",
                "test_coverage": ["tests/test_beta.py"],
            },
        ],
    }


def _write_registry(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_sample_registry(), indent=2) + "\n", encoding="utf-8")


def test_service_filters_and_summarizes_registry(app):
    from services.domain_conflicts import list_conflicts

    path = Path(app.instance_path) / f"domain_conflicts_{uuid.uuid4().hex}.json"
    _write_registry(path)
    try:
        conflicts, summary = list_conflicts(status="needs_decision", path=path)
        assert [item["id"] for item in conflicts] == ["alpha-conflict"]
        assert summary["total"] == 2
        assert summary["by_status"]["needs_decision"] == 1
        assert summary["by_status"]["implemented"] == 1
    finally:
        path.unlink(missing_ok=True)


def test_service_action_updates_status_and_actor(app):
    from services.domain_conflicts import update_conflict

    path = Path(app.instance_path) / f"domain_conflicts_{uuid.uuid4().hex}.json"
    _write_registry(path)
    try:
        updated = update_conflict(
            "alpha-conflict",
            action="mark_stale_doc",
            decision="Old doc is stale.",
            decision_note="Covered by contract.",
            actor="test_admin",
            path=path,
        )
        assert updated["status"] == "stale_doc"
        assert updated["decision"] == "Old doc is stale."
        assert updated["updated_by"] == "test_admin"

        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["conflicts"][0]["status"] == "stale_doc"
    finally:
        path.unlink(missing_ok=True)


def test_admin_can_view_conflict_review_board(app, auth_client):
    path = Path(app.instance_path) / f"domain_conflicts_{uuid.uuid4().hex}.json"
    _write_registry(path)
    app.config["DOMAIN_CONFLICTS_PATH"] = str(path)
    try:
        response = auth_client.get("/admin/domain-conflicts/")
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert "Domain Conflict Review" in body
        assert "Alpha Conflict" in body
        assert "Beta Conflict" in body
    finally:
        path.unlink(missing_ok=True)
        app.config.pop("DOMAIN_CONFLICTS_PATH", None)


def test_judge_cannot_view_conflict_review_board(app, db_session, judge_user):
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(judge_user.id)

    response = client.get("/admin/domain-conflicts/")
    assert response.status_code == 403


def test_admin_can_update_conflict_from_review_board(app, auth_client):
    path = Path(app.instance_path) / f"domain_conflicts_{uuid.uuid4().hex}.json"
    _write_registry(path)
    app.config["DOMAIN_CONFLICTS_PATH"] = str(path)
    try:
        response = auth_client.post(
            "/admin/domain-conflicts/alpha-conflict",
            data={
                "action": "needs_test",
                "decision": "Need test coverage before closing.",
                "decision_note": "Add a workflow test.",
                "filter_status": "",
                "filter_category": "",
                "filter_severity": "",
                "filter_q": "",
            },
            follow_redirects=False,
        )
        assert response.status_code in (302, 303)

        data = json.loads(path.read_text(encoding="utf-8"))
        updated = next(item for item in data["conflicts"] if item["id"] == "alpha-conflict")
        assert updated["status"] == "needs_test"
        assert updated["decision"] == "Need test coverage before closing."
        assert updated["decision_note"] == "Add a workflow test."
    finally:
        path.unlink(missing_ok=True)
        app.config.pop("DOMAIN_CONFLICTS_PATH", None)
