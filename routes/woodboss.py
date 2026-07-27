"""
Virtual Woodboss routes — material planning for block prep days.

Protected blueprint (woodboss_bp) — all routes require judge/admin access,
enforced by the MANAGEMENT_BLUEPRINTS before_request hook in app.py.

Unprotected blueprint (woodboss_public_bp) — share-link route only.
Uses HMAC token validation instead of login.

URL prefix: /woodboss  (both blueprints share the same prefix)
"""
from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for

import services.woodboss as woodboss_svc
from database import db
from models.tournament import Tournament
from models.wood_config import WoodConfig
from services.audit import log_action
from services.print_catalog import record_print

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

    import config as app_config
    presets = woodboss_svc.get_all_presets()
    # Custom preset names are everything NOT in config.WOOD_PRESETS;
    # used to render the per-preset delete button.
    custom_preset_names = sorted(
        n for n in presets.keys() if n not in app_config.WOOD_PRESETS
    )

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
        presets=presets,
        custom_preset_names=custom_preset_names,
        common_species=app_config.COMMON_WOOD_SPECIES,
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

    # Gate block writes to keys this tournament's events actually use.
    # Log keys (log_general, log_stock, log_op, log_cookie, log_relay_doublebuck)
    # and relay block keys are tournament-wide and always allowed.
    active_block_keys = woodboss_svc._active_block_keys(tid)
    def _is_writable(cfg_key):
        if not cfg_key.startswith('block_'):
            return True  # log_* keys
        return cfg_key in active_block_keys

    saved = 0
    cleared = 0
    skipped = 0
    negative_overrides = []
    for cfg_key in all_keys:
        if not _is_writable(cfg_key):
            skipped += 1
            continue
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
        except (ValueError, TypeError):
            count_override = None
        # L1: surface the ignored value instead of silently dropping it
        if count_override is not None and count_override < 0:
            negative_overrides.append(cfg_key)
            count_override = None

        existing = WoodConfig.query.filter_by(
            tournament_id=tid, config_key=cfg_key
        ).first()

        # H2: allow clearing a previously-set row. If the row exists, write
        # through even when every field is None (user blanked it on purpose).
        # For new rows, keep skipping empty submissions so we don't insert
        # ghost rows for categories the user never touched.
        all_empty = (
            species is None and size_value is None
            and notes is None and count_override is None
        )
        if existing is None and all_empty:
            continue

        normalized_unit = size_unit if size_unit in ('in', 'mm') else 'in'
        if existing:
            existing.species = species
            existing.size_value = size_value
            existing.size_unit = normalized_unit
            existing.notes = notes
            existing.count_override = count_override
            if all_empty:
                cleared += 1
            else:
                saved += 1
        else:
            db.session.add(WoodConfig(
                tournament_id=tid,
                config_key=cfg_key,
                species=species,
                size_value=size_value,
                size_unit=normalized_unit,
                notes=notes,
                count_override=count_override,
            ))
            saved += 1

    db.session.commit()
    # One-shot cleanup: delete any ghost block rows planted by older
    # preset/copy runs for events this tournament doesn't actually have.
    pruned = woodboss_svc.prune_stale_block_configs(tid)
    log_action('wood_config_saved', 'tournament', tid, {
        'keys_saved': saved, 'keys_cleared': cleared,
        'keys_skipped': skipped, 'ghost_rows_pruned': pruned,
        'negative_overrides': negative_overrides,
    })
    parts = [f'{saved} entries updated']
    if cleared:
        parts.append(f'{cleared} row(s) cleared')
    if pruned:
        parts.append(f'{pruned} stale row(s) removed')
    if skipped:
        parts.append(f'{skipped} row(s) ignored (event not in tournament)')
    msg = f'Wood specifications saved ({", ".join(parts)}).'
    flash(msg, 'success')
    if negative_overrides:
        flash(
            f'Negative count override ignored on: {", ".join(negative_overrides)}. '
            'Count overrides must be zero or positive.',
            'warning',
        )
    if request.form.get('return_to') == 'setup':
        return redirect(url_for('main.tournament_setup', tournament_id=tid, tab='wood'))
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
    # Gate copies to block keys that the DESTINATION tournament actually
    # uses. Log keys are tournament-wide and always allowed.
    active_block_keys = woodboss_svc._active_block_keys(tid)
    copied = 0
    skipped = 0
    for cfg_key, src in source_configs.items():
        if cfg_key.startswith('block_') and cfg_key not in active_block_keys:
            skipped += 1
            continue
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
    woodboss_svc.prune_stale_block_configs(tid)
    log_action('wood_config_copied', 'tournament', tid, {
        'source_tid': source_tid, 'copied': copied, 'skipped': skipped,
    })
    msg = f'Copied {copied} wood spec entries from {source.name} {source.year}.'
    if skipped:
        msg += f' Skipped {skipped} row(s) for events not in this tournament.'
    flash(msg, 'success')
    if request.form.get('return_to') == 'setup':
        return redirect(url_for('main.tournament_setup', tournament_id=tid, tab='wood'))
    return redirect(url_for('woodboss.config_form', tid=tid))


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------

@woodboss_bp.route('/<int:tid>/config/apply-preset', methods=['POST'])
def apply_preset(tid):
    """Apply a named wood preset to this tournament's config."""
    Tournament.query.get_or_404(tid)
    preset_name = request.form.get('preset_name', '').strip()
    if not preset_name:
        flash('No preset selected.', 'warning')
        return redirect(url_for('woodboss.config_form', tid=tid))

    updated = woodboss_svc.apply_preset(tid, preset_name)
    if updated:
        pruned = woodboss_svc.prune_stale_block_configs(tid)
        log_action('wood_preset_applied', 'tournament', tid, {
            'preset': preset_name, 'updated': updated, 'pruned': pruned,
        })
        msg = f'Applied preset "{preset_name}" ({updated} entries updated).'
        if pruned:
            msg += f' Cleaned up {pruned} stale row(s).'
        flash(msg, 'success')
    else:
        flash(f'Preset "{preset_name}" not found.', 'danger')
    return redirect(url_for('woodboss.config_form', tid=tid))


@woodboss_bp.route('/<int:tid>/config/save-preset', methods=['POST'])
def save_preset(tid):
    """Save current tournament config as a named preset."""
    Tournament.query.get_or_404(tid)
    preset_name = request.form.get('preset_name', '').strip()
    if not preset_name:
        flash('Preset name is required.', 'warning')
        return redirect(url_for('woodboss.config_form', tid=tid))

    # Build from currently-posted form data so unsaved edits on the wood
    # config form are captured. Falls back to DB if the form didn't include
    # wood fields (e.g. posted from a different page).
    preset_data = woodboss_svc.build_preset_from_form(request.form)
    has_form_data = (
        bool(preset_data.get('blocks'))
        or bool(preset_data.get('blocks_by_key'))
        or any(k.startswith('log_') for k in preset_data)
    )
    if not has_form_data:
        preset_data = woodboss_svc.build_preset_from_config(tid)
    try:
        woodboss_svc.save_custom_preset(preset_name, preset_data)
    except ValueError as e:
        # L4: built-in preset name collision
        flash(str(e), 'warning')
        return redirect(url_for('woodboss.config_form', tid=tid))
    log_action('wood_preset_saved', 'tournament', tid, {'preset': preset_name})
    flash(f'Saved current config as preset "{preset_name}".', 'success')
    return redirect(url_for('woodboss.config_form', tid=tid))


@woodboss_bp.route('/<int:tid>/config/delete-preset', methods=['POST'])
def delete_preset(tid):
    """Delete a custom preset."""
    Tournament.query.get_or_404(tid)
    preset_name = request.form.get('preset_name', '').strip()
    if not preset_name:
        flash('No preset specified.', 'warning')
        return redirect(url_for('woodboss.config_form', tid=tid))

    import config as app_config
    if preset_name in app_config.WOOD_PRESETS:
        flash(f'Cannot delete built-in preset "{preset_name}".', 'warning')
        return redirect(url_for('woodboss.config_form', tid=tid))

    woodboss_svc.delete_custom_preset(preset_name)
    log_action('wood_preset_deleted', 'tournament', tid, {'preset': preset_name})
    flash(f'Deleted preset "{preset_name}".', 'success')
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
@record_print('woodboss_report')
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
    if not woodboss_svc.verify_share_token(
        token, tid, current_app.config.get('SECRET_KEY', '')
    ):
        abort(403)
    tournament = Tournament.query.get_or_404(tid)
    report_data = woodboss_svc.get_wood_report(tid)
    return render_template(
        'woodboss/report_print.html',
        tournament=tournament,
        report=report_data,
    )


