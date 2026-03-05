"""
Virtual Woodboss routes — material planning for block prep days.

Protected blueprint (woodboss_bp) — all routes require judge/admin access,
enforced by the MANAGEMENT_BLUEPRINTS before_request hook in app.py.

Unprotected blueprint (woodboss_public_bp) — share-link route only.
Uses HMAC token validation instead of login.

URL prefix: /woodboss  (both blueprints share the same prefix)
"""
from flask import (Blueprint, current_app, render_template, request,
                   redirect, url_for, flash, abort)
from models.tournament import Tournament
from models.wood_config import WoodConfig
from database import db
from services.audit import log_action
import services.woodboss as woodboss_svc

woodboss_bp = Blueprint('woodboss', __name__)
woodboss_public_bp = Blueprint('woodboss_public', __name__)


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@woodboss_bp.route('/<int:tid>')
def dashboard(tid):
    tournament = Tournament.query.get_or_404(tid)
    report = woodboss_svc.get_wood_report(tid)
    share_token = woodboss_svc.generate_share_token(
        tid, current_app.config.get('SECRET_KEY', '')
    )
    return render_template(
        'woodboss/dashboard.html',
        tournament=tournament,
        report=report,
        share_token=share_token,
    )


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@woodboss_bp.route('/<int:tid>/config', methods=['GET'])
def config_form(tid):
    tournament = Tournament.query.get_or_404(tid)
    configs = woodboss_svc._get_configs(tid)
    block_rows = woodboss_svc.calculate_blocks(tid, configs=configs)
    general_cfg = configs.get(woodboss_svc.LOG_GENERAL_KEY)
    stock_cfg = configs.get(woodboss_svc.LOG_STOCK_KEY)
    op_cfg = configs.get(woodboss_svc.LOG_OP_KEY)
    cookie_cfg = configs.get(woodboss_svc.LOG_COOKIE_KEY)

    # All tournaments (for copy-from dropdown), excluding current
    all_tournaments = (
        Tournament.query
        .order_by(Tournament.year.desc(), Tournament.name)
        .all()
    )
    other_tournaments = [t for t in all_tournaments if t.id != tid]

    return render_template(
        'woodboss/config.html',
        tournament=tournament,
        block_rows=block_rows,
        general_cfg=general_cfg,
        stock_cfg=stock_cfg,
        op_cfg=op_cfg,
        cookie_cfg=cookie_cfg,
        configs=configs,
        other_tournaments=other_tournaments,
    )


@woodboss_bp.route('/<int:tid>/config', methods=['POST'])
def save_config(tid):
    tournament = Tournament.query.get_or_404(tid)

    # Gather all unique config_keys from the form
    all_keys = set()
    for field_name in request.form:
        for prefix in ('species_', 'size_value_', 'size_unit_', 'notes_', 'count_override_'):
            if field_name.startswith(prefix):
                all_keys.add(field_name[len(prefix):])

    saved = 0
    for cfg_key in all_keys:
        species = request.form.get(f'species_{cfg_key}', '').strip() or None
        size_raw = request.form.get(f'size_value_{cfg_key}', '').strip()
        size_unit = request.form.get(f'size_unit_{cfg_key}', 'in').strip()
        notes = request.form.get(f'notes_{cfg_key}', '').strip() or None
        override_raw = request.form.get(f'count_override_{cfg_key}', '').strip()

        try:
            size_value = float(size_raw) if size_raw else None
        except (ValueError, TypeError):
            size_value = None

        try:
            count_override = int(override_raw) if override_raw else None
            if count_override is not None and count_override < 0:
                count_override = None
        except (ValueError, TypeError):
            count_override = None

        # Skip rows with nothing at all
        if species is None and size_value is None and notes is None and count_override is None:
            continue

        existing = WoodConfig.query.filter_by(
            tournament_id=tid, config_key=cfg_key
        ).first()
        if existing:
            existing.species = species
            existing.size_value = size_value
            existing.size_unit = size_unit if size_unit in ('in', 'mm') else 'in'
            existing.notes = notes
            existing.count_override = count_override
        else:
            row = WoodConfig(
                tournament_id=tid,
                config_key=cfg_key,
                species=species,
                size_value=size_value,
                size_unit=size_unit if size_unit in ('in', 'mm') else 'in',
                notes=notes,
                count_override=count_override,
            )
            db.session.add(row)
        saved += 1

    db.session.commit()
    log_action('wood_config_saved', 'tournament', tid, {'keys_saved': saved})
    flash(f'Wood specifications saved ({saved} entries updated).', 'success')
    return redirect(url_for('woodboss.config_form', tid=tid))


@woodboss_bp.route('/<int:tid>/config/copy-from', methods=['POST'])
def copy_from(tid):
    """Copy wood specs from another tournament into this one."""
    tournament = Tournament.query.get_or_404(tid)
    try:
        source_tid = int(request.form.get('source_tid', 0))
    except (ValueError, TypeError):
        flash('Invalid source tournament.', 'danger')
        return redirect(url_for('woodboss.config_form', tid=tid))

    if source_tid == tid:
        flash('Cannot copy from the same tournament.', 'warning')
        return redirect(url_for('woodboss.config_form', tid=tid))

    source = Tournament.query.get(source_tid)
    if not source:
        flash('Source tournament not found.', 'danger')
        return redirect(url_for('woodboss.config_form', tid=tid))

    source_configs = woodboss_svc._get_configs(source_tid)
    copied = 0
    for cfg_key, src in source_configs.items():
        existing = WoodConfig.query.filter_by(
            tournament_id=tid, config_key=cfg_key
        ).first()
        if existing:
            existing.species = src.species
            existing.size_value = src.size_value
            existing.size_unit = src.size_unit
            existing.notes = src.notes
            existing.count_override = src.count_override
        else:
            db.session.add(WoodConfig(
                tournament_id=tid,
                config_key=cfg_key,
                species=src.species,
                size_value=src.size_value,
                size_unit=src.size_unit,
                notes=src.notes,
                count_override=src.count_override,
            ))
        copied += 1

    db.session.commit()
    log_action('wood_config_copied', 'tournament', tid, {'source_tid': source_tid, 'copied': copied})
    flash(f'Copied {copied} wood spec entries from {source.name} {source.year}.', 'success')
    return redirect(url_for('woodboss.config_form', tid=tid))


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

@woodboss_bp.route('/<int:tid>/report')
def report(tid):
    tournament = Tournament.query.get_or_404(tid)
    view = request.args.get('view', 'event')
    report_data = woodboss_svc.get_wood_report(tid)
    share_token = woodboss_svc.generate_share_token(
        tid, current_app.config.get('SECRET_KEY', '')
    )
    share_url = url_for('woodboss_public.share', tid=tid, token=share_token, _external=True)
    return render_template(
        'woodboss/report.html',
        tournament=tournament,
        report=report_data,
        view=view,
        share_url=share_url,
    )


@woodboss_bp.route('/<int:tid>/report/print')
def report_print(tid):
    tournament = Tournament.query.get_or_404(tid)
    report_data = woodboss_svc.get_wood_report(tid)
    return render_template(
        'woodboss/report_print.html',
        tournament=tournament,
        report=report_data,
    )


# ---------------------------------------------------------------------------
# Lottery view
# ---------------------------------------------------------------------------

@woodboss_bp.route('/<int:tid>/lottery')
def lottery(tid):
    tournament = Tournament.query.get_or_404(tid)
    columns = woodboss_svc.get_lottery_view(tid)
    return render_template(
        'woodboss/lottery.html',
        tournament=tournament,
        columns=columns,
    )


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------

@woodboss_bp.route('/history')
def history():
    history_data = woodboss_svc.get_history_report()
    return render_template('woodboss/history.html', history=history_data)


# ---------------------------------------------------------------------------
# Public share route (no auth required — validated by HMAC token)
# ---------------------------------------------------------------------------

@woodboss_public_bp.route('/<int:tid>/share')
def share(tid):
    token = request.args.get('token', '')
    expected = woodboss_svc.generate_share_token(
        tid, current_app.config.get('SECRET_KEY', '')
    )
    if not token or not hmac_compare(token, expected):
        abort(403)
    tournament = Tournament.query.get_or_404(tid)
    report_data = woodboss_svc.get_wood_report(tid)
    return render_template(
        'woodboss/report_print.html',
        tournament=tournament,
        report=report_data,
    )


def hmac_compare(a, b):
    """Constant-time string comparison to prevent timing attacks."""
    import hmac as _hmac
    return _hmac.compare_digest(
        a.encode('utf-8') if isinstance(a, str) else a,
        b.encode('utf-8') if isinstance(b, str) else b,
    )
