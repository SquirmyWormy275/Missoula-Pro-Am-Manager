"""Authenticated, CSRF-protected rehearsal through the operator HTTP forms."""
import io
from html.parser import HTMLParser

import openpyxl

from models import EventResult, User
from services.gear_sharing import gear_partner_names
from tests.conftest import make_event, make_tournament


class _FormInputs(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.values = {}
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'input' and attrs.get('name') and (
            attrs.get('type') == 'hidden'
            or (attrs.get('type') == 'checkbox' and 'checked' in attrs)
        ):
            self.values[attrs['name']] = attrs.get('value', 'on')


def _inputs(response):
    assert response.status_code == 200
    assert response.mimetype == 'text/html'
    return _FormInputs(response.get_data(as_text=True)).values


class _OperatorBrowser:
    """Give each HTTP request fresh Flask globals, as a deployed request has."""
    def __init__(self, app, session):
        self.app = app
        self.session = session
        self.client = app.test_client()

    def get(self, *args, **kwargs):
        kwargs['headers'] = {'Accept': 'text/html', **kwargs.get('headers', {})}
        with self.app.app_context():
            response = self.client.get(*args, **kwargs)
        self.session.expire_all()
        return response

    def post(self, *args, **kwargs):
        kwargs['headers'] = {'Accept': 'text/html', **kwargs.get('headers', {})}
        with self.app.app_context():
            response = self.client.post(*args, **kwargs)
        self.session.expire_all()
        return response


def test_import_gear_generate_scratch_score_and_finalize(app, db_session, tmp_path, monkeypatch):
    monkeypatch.setitem(app.config, 'WTF_CSRF_ENABLED', True)
    monkeypatch.setitem(app.config, 'WTF_CSRF_CHECK_DEFAULT', True)
    monkeypatch.setitem(app.config, 'UPLOAD_FOLDER', str(tmp_path))
    # The rehearsal ends at local finalization; external synchronization has
    # its own pinned-contract verification and must not receive synthetic data.
    monkeypatch.setattr('routes.scoring._push_strathmark_results', lambda *args: None)
    tournament = make_tournament(db_session, '[REHEARSAL] Operator workflow')
    underhand = make_event(db_session, tournament, 'Underhand', gender='M', max_stands=4)
    springboard = make_event(db_session, tournament, 'Springboard',
                            stand_type='springboard', max_stands=4)
    climb = make_event(db_session, tournament, 'Pole Climb',
                      stand_type='speed_climb', max_stands=2, requires_dual_runs=True)
    user = User(username=f'rehearsal_admin_{tournament.id}', role='admin')
    user.set_password('synthetic-rehearsal-password')
    db_session.add(user)
    db_session.commit()
    client = _OperatorBrowser(app, db_session)
    csrf = _inputs(client.get('/auth/login'))['csrf_token']
    login = client.post('/auth/login', data={
        'csrf_token': csrf, 'username': user.username, 'password': 'synthetic-rehearsal-password',
    })
    assert login.status_code == 302

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    headers = ['Full Name', 'Gender', 'Email Address', "Men's Underhand",
               'Springboard (L)', 'Springboard (R)', 'Speed Climb', 'Are you sharing gear?',
               "If yes, provide your gear sharing partner's name and the events you are sharing..."]
    sheet.append(headers)
    names = ['Rehearsal Owner', 'Rehearsal First', 'Rehearsal Second', 'Rehearsal Newcomer']
    for index, name in enumerate(names):
        sheet.append([name, 'Male', f'rehearsal-{index}@example.invalid', 'Yes',
                      'Yes' if index == 1 else 'No', 'No' if index == 1 else 'Yes',
                      'Yes', 'Yes' if index == 0 else 'No',
                      'SHARING Underhand with Rehearsal First and Rehearsal Second' if index == 0 else ''])
    upload = io.BytesIO()
    workbook.save(upload)
    upload.seek(0)
    import_url = f'/import/{tournament.id}/pro-entries'
    csrf = _inputs(client.get(import_url))['csrf_token']
    response = client.post(import_url, data={'csrf_token': csrf, 'file': (upload, 'rehearsal.xlsx')})
    assert response.status_code == 302
    review = client.get(response.location)
    assert all(name in review.get_data(as_text=True) for name in names)
    response = client.post(import_url + '/confirm', data={'csrf_token': _inputs(review)['csrf_token']})
    assert response.status_code == 302
    roster = {comp.name: comp for comp in tournament.pro_competitors.all()}
    assert set(roster) == set(names)
    assert EventResult.query.filter_by(event_id=underhand.id).count() == 4
    owner, first, second, newcomer = (roster[name] for name in names)
    assert first.is_left_handed_springboard is True
    assert set(gear_partner_names(owner.get_gear_sharing()[str(underhand.id)])) == {first.name, second.name}

    response = client.post(f'/registration/{tournament.id}/pro/gear-sharing/update-ajax', data={
        'csrf_token': csrf, 'competitor_id': newcomer.id,
        'event_key': str(underhand.id), 'partner_name': owner.name,
    })
    assert response.status_code == 200 and response.get_json()['ok']
    assert set(gear_partner_names(owner.get_gear_sharing()[str(underhand.id)])) == {
        first.name, second.name, newcomer.name,
    }

    for event in (underhand, springboard, climb):
        response = client.post(
            f'/scheduling/{tournament.id}/event/{event.id}/generate-heats', data={'csrf_token': csrf},
        )
        assert response.status_code == 302
        assert event.heats.count() > 0
    for heat in springboard.heats.all():
        if first.id in heat.get_competitors():
            assert heat.get_stand_for_competitor(first.id) == 4
    climb_stands = {comp.id: {} for comp in roster.values()}
    for heat in climb.heats.all():
        for comp_id in heat.get_competitors():
            climb_stands[comp_id][heat.run_number] = heat.get_stand_for_competitor(comp_id)
    assert all(stands[1] != stands[2] for stands in climb_stands.values())

    preview = client.get(
        f'/scoring/{tournament.id}/competitor/{second.id}/scratch-preview?competitor_type=pro',
    )
    scratch_fields = _inputs(preview)
    response = client.post(
        f'/scoring/{tournament.id}/competitor/{second.id}/scratch-confirm', data=scratch_fields,
    )
    assert response.status_code == 302
    db_session.expire_all()
    with client.client.session_transaction() as browser_session:
        messages = browser_session.get('_flashes', [])
    assert second.status == 'scratched', (response.location, messages)
    cleaned = client.post(
        f'/registration/{tournament.id}/pro/gear-sharing/cleanup-scratched',
        data={'csrf_token': scratch_fields['csrf_token']},
    )
    assert cleaned.status_code == 302
    assert set(gear_partner_names(owner.get_gear_sharing()[str(underhand.id)])) == {first.name, newcomer.name}
    assert all(second.id not in heat.get_competitors()
               for event in (underhand, springboard, climb) for heat in event.heats.all())

    async_headers = {'X-Requested-With': 'XMLHttpRequest', 'Accept': 'application/json'}

    def save(heat, values):
        url = f'/scoring/{tournament.id}/heat/{heat.id}/enter'
        fields = _inputs(client.get(url))
        assert fields['request_id'] and fields['heat_identity'] and fields['scoring_state_digest']
        return client.post(url, data={**fields, **values}, headers=async_headers)

    multi_heat = next(heat for heat in underhand.heats.all() if len(heat.get_competitors()) > 1)
    comp_id = multi_heat.get_competitors()[0]
    saved = save(multi_heat, {f't1_run1_{comp_id}': '10', f't2_run1_{comp_id}': '10'})
    assert saved.status_code == 200 and saved.get_json()['category'] == 'warning'
    assert multi_heat.status != 'completed' and not underhand.is_finalized
    refused = client.post(f'/scoring/{tournament.id}/event/{underhand.id}/finalize',
                          data={'csrf_token': csrf}, headers=async_headers)
    assert refused.status_code == 409 and refused.get_json()['ok'] is False

    for event in (underhand, springboard, climb):
        for heat in event.heats.all():
            values = {}
            run = 'run2' if heat.run_number == 2 else 'run1'
            for competitor_id in heat.get_competitors():
                if event.id == underhand.id and competitor_id == newcomer.id:
                    values.update({f'status_{competitor_id}': 'dnf',
                                   f'reason_{competitor_id}': 'Synthetic non-finish'})
                else:
                    values.update({f't1_{run}_{competitor_id}': '12', f't2_{run}_{competitor_id}': '12'})
            saved = save(heat, values)
            assert saved.status_code == 200 and saved.get_json()['ok']
            assert heat.status == 'completed'
        assert event.is_finalized
        results_page = client.get(f'/scoring/{tournament.id}/event/{event.id}/results')
        assert results_page.status_code == 200
        assert 'Rehearsal Owner' in results_page.get_data(as_text=True)
        finalized = client.post(f'/scoring/{tournament.id}/event/{event.id}/finalize',
                                data={'csrf_token': csrf}, headers=async_headers)
        assert finalized.status_code == 200 and finalized.get_json()['ok']
    non_finish = EventResult.query.filter_by(event_id=underhand.id, competitor_id=newcomer.id).one()
    assert non_finish.status == 'dnf' and non_finish.result_value is None
    from scripts.audit_tournament_workflows import audit_session
    assert audit_session(db_session)['findings'] == []
