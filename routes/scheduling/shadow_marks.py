"""Judge/admin workflow for scoring-inert STRATHMARK shadow recommendations."""

from __future__ import annotations

import hashlib
import json
from datetime import date

from flask import Response, abort, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user
from sqlalchemy.orm.exc import StaleDataError

from database import db
from models import Event, ShadowHandicapRun, Tournament, User
from services.audit import log_action
from services.shadow_handicap_state import ShadowConcurrencyError, transition_shadow_run
from services.shadow_operator import (
    ShadowIssueBlocked,
    ShadowReviewError,
    build_shadow_operator_view,
    build_shadow_schedule_fingerprint,
    issue_shadow_sheet,
    review_shadow_sheet,
    verify_shadow_export,
)
from services.shadow_settlement import (
    CORRECTION_REASON_CODES,
    build_shadow_standings,
    deliver_shadow_settlement_outbox,
    outcome_state_token,
    reconcile_shadow_outcomes,
)
from services.strathmark_shadow import (
    OBSERVATION_SCHEMA_VERSION,
    ShadowAdapterError,
    ShadowClientConfig,
    StrathmarkShadowClient,
    calculate_or_recover_shadow_run,
    prepare_shadow_run,
)

from . import scheduling_bp


def _require_shadow_operator():
    if not getattr(current_user, "is_authenticated", False) or current_user.role not in {
        User.ROLE_ADMIN,
        User.ROLE_JUDGE,
    }:
        abort(403)


def _shadow_client() -> StrathmarkShadowClient:
    return StrathmarkShadowClient(ShadowClientConfig.from_mapping(current_app.config))


def _remote_status(run: ShadowHandicapRun):
    return _shadow_client().status(run, current_user)


def _deliver_pending_outcomes() -> None:
    """Best-effort delivery after local commit; local evidence remains authoritative."""

    try:
        from services.strathmark_shadow import shadow_configuration_status

        if shadow_configuration_status(current_app.config) == "configured":
            deliver_shadow_settlement_outbox(client=_shadow_client(), limit=25, commit=True)
    except Exception:
        current_app.logger.exception(
            "STRATHMARK shadow settlement delivery failed; durable outbox retained"
        )


def _latest_run(event_id: int):
    return (
        ShadowHandicapRun.query.filter_by(event_id=event_id)
        .order_by(ShadowHandicapRun.created_at.desc(), ShadowHandicapRun.id.desc())
        .first()
    )


def _unknown_observation_fingerprint() -> str:
    value = {
        "schema_version": OBSERVATION_SCHEMA_VERSION,
        "state": "explicit-unknown",
        "factors": [],
    }
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _load_event(tournament_id: int, event_id: int):
    tournament = db.get_or_404(Tournament, tournament_id)
    event = Event.query.filter_by(id=event_id, tournament_id=tournament.id).first_or_404()
    return tournament, event


@scheduling_bp.route(
    "/<int:tournament_id>/events/<int:event_id>/shadow-marks",
    methods=["GET", "POST"],
)
def shadow_marks(tournament_id: int, event_id: int):
    """Prepare, calculate, review, and issue one whole-field shadow sheet."""

    _require_shadow_operator()
    tournament, event = _load_event(tournament_id, event_id)
    run = _latest_run(event.id)

    if request.method == "POST":
        action = (request.form.get("action") or "").strip()
        try:
            if action == "prepare":
                cutoff_raw = (request.form.get("prediction_as_of") or "").strip()
                if not cutoff_raw:
                    raise ValueError("Choose the exclusive UTC prediction cutoff date.")
                previous_run = run
                run = prepare_shadow_run(
                    event,
                    actor=current_user,
                    prediction_as_of=date.fromisoformat(cutoff_raw),
                    schedule_fingerprint=build_shadow_schedule_fingerprint(event),
                    observation_schema_version=OBSERVATION_SCHEMA_VERSION,
                    observation_fingerprint=_unknown_observation_fingerprint(),
                    supersedes_run=previous_run,
                )
                if previous_run is not None and previous_run.lifecycle not in {
                    "superseded",
                    "cancelled",
                }:
                    transition_shadow_run(
                        previous_run,
                        expected_version=previous_run.lifecycle_version,
                        lifecycle="superseded",
                        actor_id=current_user.id,
                        reason_code="new_field_revision_prepared",
                    )
                db.session.commit()
                log_action(
                    "shadow_run_prepared",
                    "event",
                    event.id,
                    {"run_id": run.run_id, "request_id": run.request_id},
                )
                flash("Shadow field snapshot prepared. Review the preflight summary.", "success")
            elif run is None:
                raise ValueError("Prepare a shadow field snapshot first.")
            elif action == "approve_preflight":
                transition_shadow_run(
                    run,
                    expected_version=int(request.form["expected_version"]),
                    lifecycle="preflight-approved",
                    actor_id=current_user.id,
                    reason_code="operator_preflight_approved",
                )
                db.session.commit()
                flash("Preflight approved for this exact field revision.", "success")
            elif action == "calculate":
                calculate_or_recover_shadow_run(run, client=_shadow_client())
                db.session.commit()
                flash("Trusted STRATHMARK receipt recorded.", "success")
            elif action == "review":
                review_shadow_sheet(
                    run,
                    actor=current_user,
                    expected_version=int(request.form["expected_version"]),
                    reviewed_prediction_ids=set(request.form.getlist("review_prediction")),
                    remote_status=_remote_status(run),
                )
                db.session.commit()
                flash("Every recommendation, including zero, was explicitly reviewed.", "success")
            elif action == "issue":
                issue_shadow_sheet(
                    run,
                    actor=current_user,
                    expected_version=int(request.form["expected_version"]),
                    remote_status=_remote_status(run),
                )
                db.session.commit()
                flash("Entire shadow sheet issued and checksummed.", "success")
            elif action == "reconcile_outcomes":
                if request.form.get("confirm_outcome_reconciliation") != "yes":
                    raise ValueError("Confirm the outcome reconciliation before recording it.")
                result = reconcile_shadow_outcomes(
                    run,
                    event=event,
                    actor=current_user,
                    expected_outcome_token=request.form.get("expected_outcome_token", ""),
                    reason_code=request.form.get("reason_code", ""),
                )
                db.session.commit()
                _deliver_pending_outcomes()
                flash(
                    (
                        f"Recorded {result.outcome_count} outcome correction(s); "
                        f"{result.numeric_action_count} numeric settle/void action(s) queued."
                    ),
                    "success",
                )
            else:
                raise ValueError("Unknown shadow workflow action.")
        except (
            KeyError,
            ValueError,
            ShadowAdapterError,
            ShadowConcurrencyError,
            StaleDataError,
        ) as exc:
            db.session.rollback()
            flash(str(exc), "error")
        return redirect(
            url_for(
                "scheduling.shadow_marks",
                tournament_id=tournament.id,
                event_id=event.id,
            )
        )

    remote_status = None
    if run is not None and run.receipts:
        try:
            remote_status = _remote_status(run)
        except ShadowAdapterError:
            remote_status = None
    view = build_shadow_operator_view(run, remote_status=remote_status) if run else None
    outcomes = build_shadow_standings(run) if run and run.receipts else ()
    return render_template(
        "scheduling/shadow_marks.html",
        tournament=tournament,
        event=event,
        run=run,
        view=view,
        outcomes=outcomes,
        outcome_state_token=outcome_state_token(run) if run else "",
        correction_reason_codes=tuple(sorted(CORRECTION_REASON_CODES)),
    )


@scheduling_bp.route("/<int:tournament_id>/events/<int:event_id>/shadow-marks/<int:run_pk>/export")
def shadow_marks_export(tournament_id: int, event_id: int, run_pk: int):
    """Download the verified, non-importable, pseudonymous issue artifact."""

    _require_shadow_operator()
    _tournament, _event = _load_event(tournament_id, event_id)
    run = ShadowHandicapRun.query.filter_by(id=run_pk, event_id=event_id).first_or_404()
    if not run.issue_artifacts:
        abort(404)
    artifact = run.issue_artifacts[-1]
    try:
        verify_shadow_export(artifact)
    except ShadowIssueBlocked:
        abort(409)
    log_action(
        "shadow_export_downloaded",
        "event",
        event_id,
        {"run_id": run.run_id, "export_sha256": artifact.export_sha256},
    )
    return Response(
        artifact.export_json + "\n",
        mimetype="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="shadow-sheet-{run.id}.json"',
            "X-Content-SHA256": artifact.export_sha256,
            "X-Shadow-Importable": "false",
        },
    )
