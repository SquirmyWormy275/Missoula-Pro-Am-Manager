"""Admin review board for domain-contract conflicts."""
from __future__ import annotations

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from services.domain_conflicts import (
    VALID_ACTIONS,
    VALID_SEVERITIES,
    VALID_STATUSES,
    list_conflicts,
    update_conflict,
)

bp = Blueprint('domain_conflicts', __name__, url_prefix='/admin/domain-conflicts')


def _require_admin():
    if not getattr(current_user, 'is_authenticated', False):
        return abort(403)
    if not getattr(current_user, 'is_admin', False):
        return abort(403)
    return None


@bp.route('/', methods=['GET'])
@login_required
def index():
    denied = _require_admin()
    if denied:
        return denied

    selected_status = (request.args.get('status') or '').strip()
    selected_category = (request.args.get('category') or '').strip()
    selected_severity = (request.args.get('severity') or '').strip()
    query = (request.args.get('q') or '').strip()

    conflicts, summary = list_conflicts(
        status=selected_status or None,
        category=selected_category or None,
        severity=selected_severity or None,
        query=query or None,
    )

    return render_template(
        'admin/domain_conflicts.html',
        conflicts=conflicts,
        summary=summary,
        statuses=VALID_STATUSES,
        actions=VALID_ACTIONS,
        severities=VALID_SEVERITIES,
        selected_status=selected_status,
        selected_category=selected_category,
        selected_severity=selected_severity,
        query=query,
    )


@bp.route('/<conflict_id>', methods=['POST'])
@login_required
def update(conflict_id):
    denied = _require_admin()
    if denied:
        return denied

    status = (request.form.get('status') or '').strip() or None
    action = (request.form.get('action') or '').strip() or None
    decision = request.form.get('decision')
    decision_note = request.form.get('decision_note')

    try:
        updated = update_conflict(
            conflict_id,
            status=status,
            action=action,
            decision=decision,
            decision_note=decision_note,
            actor=getattr(current_user, 'username', None),
        )
    except KeyError:
        abort(404)
    except ValueError as exc:
        flash(str(exc), 'error')
    else:
        flash(f"Updated conflict: {updated.get('title', conflict_id)}", 'success')

    return redirect(url_for(
        'domain_conflicts.index',
        status=request.form.get('filter_status') or None,
        category=request.form.get('filter_category') or None,
        severity=request.form.get('filter_severity') or None,
        q=request.form.get('filter_q') or None,
    ))
