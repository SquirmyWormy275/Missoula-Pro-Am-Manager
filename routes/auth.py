"""
Authentication and user-management routes.
"""
from urllib.parse import urlsplit
from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user
from database import db
from models import Tournament
from models.competitor import CollegeCompetitor, ProCompetitor
from models.user import User
from models.audit_log import AuditLog
from services.audit import log_action

auth_bp = Blueprint('auth', __name__)


def _safe_redirect_target(target: str | None):
    if not target:
        return None
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc:
        return None
    if not target.startswith('/'):
        return None
    return target


def _role_default_redirect(user: User):
    if user.is_judge or user.can_register or user.can_score:
        return url_for('main.judge_dashboard')
    if user.is_competitor:
        return url_for('portal.competitor_dashboard')
    if user.tournament_id:
        return url_for('portal.spectator_dashboard', tournament_id=user.tournament_id)
    return url_for('portal.index')


def _require_judge():
    if not current_user.is_authenticated:
        return redirect(url_for('auth.login', next=request.path))
    if not getattr(current_user, 'is_judge', False):
        abort(403)
    return None


def _require_admin():
    if not current_user.is_authenticated:
        return redirect(url_for('auth.login', next=request.path))
    if not getattr(current_user, 'can_manage_users', False):
        abort(403)
    return None


@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    """Log in an existing user."""
    if current_user.is_authenticated:
        return redirect(_role_default_redirect(current_user))

    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        password = request.form.get('password') or ''
        next_page = _safe_redirect_target(request.form.get('next'))

        user = User.query.filter_by(username=username).first()
        if not user or not user.check_password(password):
            flash('Invalid username or password.', 'error')
            return render_template('auth/login.html', next_page=next_page)
        if not user.is_active_user:
            flash('This account is disabled. Contact a judge.', 'error')
            return render_template('auth/login.html', next_page=next_page)

        login_user(user)
        log_action('login', 'user', user.id, {'username': user.username, 'role': user.role})
        db.session.commit()
        flash(f'Welcome, {user.username}.', 'success')
        return redirect(next_page or _role_default_redirect(user))

    next_page = _safe_redirect_target(request.args.get('next'))
    return render_template('auth/login.html', next_page=next_page)


@auth_bp.route('/logout', methods=['POST'])
@login_required
def logout():
    """Log out current user."""
    log_action('logout', 'user', current_user.id, {'username': current_user.username})
    db.session.commit()
    logout_user()
    flash('You have been logged out.', 'success')
    return redirect(url_for('main.index'))


@auth_bp.route('/bootstrap', methods=['GET', 'POST'])
def bootstrap():
    """Create the first judge/admin account when DB is empty."""
    if User.query.count() > 0:
        flash('Bootstrap is disabled because users already exist.', 'warning')
        return redirect(url_for('auth.login'))

    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        password = request.form.get('password') or ''
        role = request.form.get('role', User.ROLE_ADMIN)
        if role not in {User.ROLE_ADMIN, User.ROLE_JUDGE}:
            role = User.ROLE_ADMIN

        if len(username) < 3:
            flash('Username must be at least 3 characters.', 'error')
            return render_template('auth/bootstrap.html')
        if len(password) < 8:
            flash('Password must be at least 8 characters.', 'error')
            return render_template('auth/bootstrap.html')

        user = User(username=username, role=role, display_name='Head Judge')
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        log_action('bootstrap_user_created', 'user', user.id, {'role': role})
        db.session.commit()

        flash('Initial judge account created. Please log in.', 'success')
        return redirect(url_for('auth.login'))

    return render_template('auth/bootstrap.html')


@auth_bp.route('/users', methods=['GET', 'POST'])
def manage_users():
    """Judge-facing user administration."""
    denied = _require_admin()
    if denied:
        return denied

    tournaments = Tournament.query.order_by(Tournament.year.desc()).all()
    selected_tournament_id = request.values.get('tournament_id', type=int)

    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        password = request.form.get('password') or ''
        role = request.form.get('role', User.ROLE_SPECTATOR)
        display_name = (request.form.get('display_name') or '').strip() or None
        tournament_id = request.form.get('tournament_id', type=int)
        competitor_type = (request.form.get('competitor_type') or '').strip() or None
        competitor_id = request.form.get('competitor_id', type=int)

        selected_tournament_id = tournament_id

        if role not in {
            User.ROLE_ADMIN,
            User.ROLE_JUDGE,
            User.ROLE_SCORER,
            User.ROLE_REGISTRAR,
            User.ROLE_COMPETITOR,
            User.ROLE_SPECTATOR,
            User.ROLE_VIEWER,
        }:
            flash('Invalid role selection.', 'error')
            return redirect(url_for('auth.manage_users', tournament_id=selected_tournament_id))
        if len(username) < 3:
            flash('Username must be at least 3 characters.', 'error')
            return redirect(url_for('auth.manage_users', tournament_id=selected_tournament_id))
        if len(password) < 8:
            flash('Password must be at least 8 characters.', 'error')
            return redirect(url_for('auth.manage_users', tournament_id=selected_tournament_id))
        if User.query.filter_by(username=username).first():
            flash('That username is already in use.', 'error')
            return redirect(url_for('auth.manage_users', tournament_id=selected_tournament_id))

        if role == User.ROLE_COMPETITOR:
            if not tournament_id:
                flash('Competitor accounts must be tied to a tournament.', 'error')
                return redirect(url_for('auth.manage_users', tournament_id=selected_tournament_id))
            if competitor_type not in {'college', 'pro'} or not competitor_id:
                flash('Competitor accounts must be linked to a competitor record.', 'error')
                return redirect(url_for('auth.manage_users', tournament_id=selected_tournament_id))
            linked = _load_competitor(tournament_id, competitor_type, competitor_id)
            if not linked:
                flash('Selected competitor could not be found.', 'error')
                return redirect(url_for('auth.manage_users', tournament_id=selected_tournament_id))
            if not display_name:
                display_name = linked.name
        elif role in {User.ROLE_SPECTATOR, User.ROLE_VIEWER}:
            competitor_type = None
            competitor_id = None
        else:
            tournament_id = None
            competitor_type = None
            competitor_id = None

        user = User(
            username=username,
            role=role,
            display_name=display_name,
            tournament_id=tournament_id,
            competitor_type=competitor_type,
            competitor_id=competitor_id,
            is_active_user=True,
        )
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        log_action('user_created', 'user', user.id, {'role': role, 'username': username})
        db.session.commit()

        flash(f'Created user "{username}" ({role}).', 'success')
        return redirect(url_for('auth.manage_users', tournament_id=selected_tournament_id))

    college_competitors = []
    pro_competitors = []
    if selected_tournament_id:
        college_competitors = CollegeCompetitor.query.filter_by(
            tournament_id=selected_tournament_id,
            status='active'
        ).order_by(CollegeCompetitor.name).all()
        pro_competitors = ProCompetitor.query.filter_by(
            tournament_id=selected_tournament_id,
            status='active'
        ).order_by(ProCompetitor.name).all()

    users = User.query.order_by(User.created_at.desc()).all()
    return render_template(
        'auth/users.html',
        users=users,
        tournaments=tournaments,
        selected_tournament_id=selected_tournament_id,
        college_competitors=college_competitors,
        pro_competitors=pro_competitors,
    )


@auth_bp.route('/users/<int:user_id>/toggle-active', methods=['POST'])
def toggle_user_active(user_id):
    """Enable or disable a non-self account."""
    denied = _require_admin()
    if denied:
        return denied

    user = User.query.get_or_404(user_id)
    if user.id == current_user.id:
        flash('You cannot disable your own account.', 'error')
        return redirect(url_for('auth.manage_users'))

    user.is_active_user = not user.is_active_user
    db.session.commit()
    log_action('user_toggled_active', 'user', user.id, {'active': user.is_active_user})
    db.session.commit()

    state = 'enabled' if user.is_active_user else 'disabled'
    flash(f'User "{user.username}" is now {state}.', 'success')
    return redirect(url_for('auth.manage_users', tournament_id=request.form.get('tournament_id', type=int)))


@auth_bp.route('/users/reset-competitor-pin', methods=['POST'])
def reset_competitor_pin():
    """Admin-only reset for competitor portal PIN."""
    denied = _require_admin()
    if denied:
        return denied

    tournament_id = request.form.get('tournament_id', type=int)
    competitor_type = (request.form.get('competitor_type') or '').strip()
    competitor_id = request.form.get('competitor_id', type=int)
    user_id = request.form.get('user_id', type=int)

    competitor = _load_competitor(tournament_id, competitor_type, competitor_id)
    if not competitor:
        flash('Competitor record not found for PIN reset.', 'error')
        return redirect(url_for('auth.manage_users', tournament_id=tournament_id))

    competitor.portal_pin_hash = None
    db.session.commit()
    log_action(
        'competitor_pin_reset',
        'user',
        user_id,
        {
            'tournament_id': tournament_id,
            'competitor_type': competitor_type,
            'competitor_id': competitor_id,
            'competitor_name': competitor.name,
        }
    )
    db.session.commit()

    flash(f'PIN reset for {competitor.name}. They must set a new PIN on next access.', 'success')
    return redirect(url_for('auth.manage_users', tournament_id=tournament_id))


def _load_competitor(tournament_id: int, competitor_type: str, competitor_id: int):
    if competitor_type == 'college':
        return CollegeCompetitor.query.filter_by(
            id=competitor_id,
            tournament_id=tournament_id
        ).first()
    if competitor_type == 'pro':
        return ProCompetitor.query.filter_by(
            id=competitor_id,
            tournament_id=tournament_id
        ).first()
    return None


# ---------------------------------------------------------------------------
# #9 — Audit log viewer
# ---------------------------------------------------------------------------

@auth_bp.route('/audit')
@login_required
def audit_log():
    """View audit log entries (admin only)."""
    if not current_user.is_admin:
        abort(403)

    action_filter = request.args.get('action', '').strip()
    entity_type_filter = request.args.get('entity_type', '').strip()
    user_id_filter = request.args.get('user_id', '').strip()
    from_filter = request.args.get('from', '').strip()
    to_filter = request.args.get('to', '').strip()
    page = max(1, int(request.args.get('page', 1) or 1))
    per_page = 50

    query = AuditLog.query.order_by(AuditLog.created_at.desc())

    if action_filter:
        query = query.filter(AuditLog.action.ilike(f'%{action_filter}%'))
    if entity_type_filter:
        query = query.filter(AuditLog.entity_type == entity_type_filter)
    if user_id_filter:
        try:
            query = query.filter(AuditLog.actor_user_id == int(user_id_filter))
        except ValueError:
            pass
    if from_filter:
        try:
            from datetime import datetime
            query = query.filter(AuditLog.created_at >= datetime.fromisoformat(from_filter))
        except ValueError:
            pass
    if to_filter:
        try:
            from datetime import datetime
            query = query.filter(AuditLog.created_at <= datetime.fromisoformat(to_filter))
        except ValueError:
            pass

    total = query.count()
    entries = query.offset((page - 1) * per_page).limit(per_page).all()
    total_pages = max(1, (total + per_page - 1) // per_page)

    # Distinct actions and entity types for filter dropdowns
    distinct_actions = [
        r[0] for r in db.session.query(AuditLog.action).distinct().order_by(AuditLog.action).all()
    ]
    distinct_entity_types = [
        r[0] for r in db.session.query(AuditLog.entity_type).distinct().order_by(AuditLog.entity_type).all()
        if r[0]
    ]

    return render_template(
        'auth/audit_log.html',
        entries=entries,
        page=page,
        total_pages=total_pages,
        total=total,
        action_filter=action_filter,
        entity_type_filter=entity_type_filter,
        user_id_filter=user_id_filter,
        from_filter=from_filter,
        to_filter=to_filter,
        distinct_actions=distinct_actions,
        distinct_entity_types=distinct_entity_types,
    )
