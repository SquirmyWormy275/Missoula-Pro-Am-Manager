"""Regression coverage for locally trusted sheets during remote degradation."""

from tests.test_shadow_operator_workflow import calculated_run  # noqa: F401


def test_calculated_sheet_remains_visible_when_shadow_service_is_unconfigured(
    calculated_run,
    auth_client,
    monkeypatch,
):
    tournament, event, _results, _run = calculated_run
    for name in (
        "STRATHMARK_SHADOW_URL",
        "STRATHMARK_SHADOW_CONSUMER_ID",
        "STRATHMARK_SHADOW_SERVICE_TOKEN",
        "STRATHMARK_SHADOW_ATTESTATION_KEY",
    ):
        monkeypatch.delitem(auth_client.application.config, name, raising=False)

    response = auth_client.get(
        f"/scheduling/{tournament.id}/events/{event.id}/shadow-marks"
    )

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Shadow handicap sheet" in body
    assert "Action required" in body
    assert "<title>500" not in body
