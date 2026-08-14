"""Frozen STRATHMARK V2 shadow consumer and recovery behavior."""

import base64
import hashlib
import hmac
import json
import time
from datetime import date
from unittest.mock import patch

import pytest

from models import (
    CompetitorExternalIdentity,
    ShadowHandicapRun,
    ShadowReceiptRevision,
    WoodConfig,
)
from services.shadow_handicap_state import transition_shadow_run
from services.strathmark_shadow import (
    ShadowClientConfig,
    ShadowIdentityError,
    ShadowRemoteNotFound,
    ShadowRemoteTimeout,
    ShadowUnsupportedEventError,
    StrathmarkShadowClient,
    calculate_or_recover_shadow_run,
    canonical_shadow_request_digest,
    prepare_shadow_run,
    shadow_configuration_status,
)
from tests.conftest import (
    make_event,
    make_event_result,
    make_pro_competitor,
    make_tournament,
)


class FakeTransport:
    def __init__(self):
        self.calls = []
        self.responses = {}

    def queue(self, path, *responses):
        self.responses.setdefault(path, []).extend(responses)

    def post(self, *, base_url, path, payload, headers, timeout_seconds):
        self.calls.append(
            {
                "base_url": base_url,
                "path": path,
                "payload": payload,
                "headers": headers,
                "timeout_seconds": timeout_seconds,
            }
        )
        response = self.responses[path].pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _decode_attestation(value):
    encoded, signature = value.split(".", 1)
    padded_payload = encoded + "=" * (-len(encoded) % 4)
    padded_signature = signature + "=" * (-len(signature) % 4)
    return (
        json.loads(base64.urlsafe_b64decode(padded_payload)),
        base64.urlsafe_b64decode(padded_signature),
        encoded,
    )


def _receipt_response(run, competitor_ids, *, active_fingerprint="a" * 64):
    core = {
        "schema_version": "strathmark.shadow-receipt-core.v1",
        "consumer_id": run.consumer_id,
        "request_id": run.request_id,
        "run_revision": run.run_revision,
        "event_code": "UH",
        "active_input": {"fingerprint": active_fingerprint},
        "predictions": [
            {
                "ordinal": ordinal,
                "competitor_id": competitor_id,
                "prediction_id": f"strathmark:prediction:{ordinal:04d}",
                "assigned_mark": 3 + ordinal,
                "median_seconds": 40.0 + ordinal,
            }
            for ordinal, competitor_id in enumerate(competitor_ids)
        ],
    }
    core_json = json.dumps(core, sort_keys=True, separators=(",", ":"))
    receipt = {
        "core_json": core_json,
        "core": core,
        "status": {
            "trust": "recorded",
            "freshness": "current",
            "mirror": "not-configured",
            "ready_for_review": True,
        },
    }
    return {
        "schema_version": "strathmark.shadow-calculate-response.v1",
        "trusted": True,
        "receipt": receipt,
        "status": receipt["status"],
        "draft_predictions": [],
    }


@pytest.fixture()
def prepared_shadow(db_session, admin_user):
    tournament = make_tournament(db_session, year=2027)
    event = make_event(
        db_session,
        tournament,
        name="Underhand",
        event_type="pro",
        gender="M",
        stand_type="underhand",
        scoring_type="time",
        is_handicap=True,
    )
    event.handicap_authority_mode = "shadow"
    competitors = [
        make_pro_competitor(db_session, tournament, "Display One", "M", events=[event.name]),
        make_pro_competitor(db_session, tournament, "Display Two", "M", events=[event.name]),
    ]
    results = [make_event_result(db_session, event, competitor) for competitor in competitors]
    external_ids = []
    for index, competitor in enumerate(competitors, start=1):
        external_id = f"strathmark:competitor:000{index}"
        external_ids.append(external_id)
        db_session.add(
            CompetitorExternalIdentity(
                competitor_uid=competitor.uid,
                namespace="strathmark",
                external_id=external_id,
                status="reviewed",
                reviewed_by_id=admin_user.id,
            )
        )
    db_session.add(
        WoodConfig(
            tournament_id=tournament.id,
            config_key="block_underhand_pro_M",
            species="Western White Pine",
            size_value=13.0,
            size_unit="in",
        )
    )
    db_session.flush()
    run = prepare_shadow_run(
        event,
        actor=admin_user,
        prediction_as_of=date(2027, 5, 8),
        schedule_fingerprint="1" * 64,
        observation_schema_version="strathmark.shadow-observation-fingerprint.v1",
        observation_fingerprint="2" * 64,
    )
    transition_shadow_run(
        run,
        expected_version=1,
        lifecycle="preflight-approved",
        actor_id=admin_user.id,
        reason_code="preflight_approved",
    )
    db_session.flush()
    return event, results, run, external_ids


def _client(transport):
    return StrathmarkShadowClient(
        ShadowClientConfig(
            base_url="http://127.0.0.1:8000",
            consumer_id="missoula:service:shadow",
            service_token="service-token-123456789",
            attestation_key="attestation-key-123456789",
        ),
        transport=transport,
    )


def test_request_digest_and_attestation_bind_exact_payload(prepared_shadow):
    _event, _results, run, external_ids = prepared_shadow
    transport = FakeTransport()
    transport.queue("/v1/shadow/receipts/lookup", ShadowRemoteNotFound("missing"))
    transport.queue("/v1/shadow/calculate", _receipt_response(run, external_ids))
    client = _client(transport)

    calculate_or_recover_shadow_run(run, client=client)

    calculate_call = transport.calls[1]
    payload = calculate_call["payload"]
    assert payload["competitors"] == [
        {"competitor_id": "strathmark:competitor:0001", "gender": "M"},
        {"competitor_id": "strathmark:competitor:0002", "gender": "M"},
    ]
    assert "Display One" not in json.dumps(payload)
    claims, signature, encoded = _decode_attestation(
        calculate_call["headers"]["X-STRATHMARK-Actor-Attestation"]
    )
    assert claims["schema_version"] == "strathmark.actor-attestation.v2"
    assert claims["actor_id"].startswith("missoula:operator:")
    assert claims["action"] == "shadow.calculate"
    assert claims["subject_revision"] == run.run_revision
    assert claims["request_digest"] == canonical_shadow_request_digest(payload)
    expected = hmac.new(
        b"attestation-key-123456789",
        encoded.encode("ascii"),
        hashlib.sha256,
    ).digest()
    assert hmac.compare_digest(signature, expected)
    assert claims["issued_at"] <= int(time.time()) <= claims["expires_at"]


def test_receipt_lookup_always_precedes_calculation_and_persists_exact_core(
    db_session,
    prepared_shadow,
):
    _event, results, run, external_ids = prepared_shadow
    response = _receipt_response(run, external_ids)
    transport = FakeTransport()
    transport.queue("/v1/shadow/receipts/lookup", ShadowRemoteNotFound("missing"))
    transport.queue("/v1/shadow/calculate", response)

    with patch(
        "services.mark_assignment.assign_handicap_marks",
        side_effect=AssertionError("legacy official calculator must not run"),
    ):
        recovered = calculate_or_recover_shadow_run(run, client=_client(transport))
    db_session.flush()

    assert [call["path"] for call in transport.calls] == [
        "/v1/shadow/receipts/lookup",
        "/v1/shadow/calculate",
    ]
    assert recovered.core_json == response["receipt"]["core_json"]
    assert run.lifecycle == "calculated"
    assert run.active_input_fingerprint == "a" * 64
    assert ShadowReceiptRevision.query.filter_by(run_id=run.id).count() == 1
    assert [(row.handicap_factor, row.predicted_time, row.mark_assigned_at) for row in results] == [
        (0.0, None, None),
        (0.0, None, None),
    ]


def test_calculation_timeout_recovers_by_lookup_without_second_calculation(
    db_session,
    prepared_shadow,
):
    _event, _results, run, external_ids = prepared_shadow
    lookup_response = _receipt_response(run, external_ids)
    lookup_response["schema_version"] = "strathmark.shadow-receipt-lookup-response.v1"
    lookup_response.pop("trusted")
    lookup_response.pop("status")
    lookup_response.pop("draft_predictions")
    transport = FakeTransport()
    transport.queue(
        "/v1/shadow/receipts/lookup",
        ShadowRemoteNotFound("missing"),
        lookup_response,
    )
    transport.queue("/v1/shadow/calculate", ShadowRemoteTimeout("unknown outcome"))

    receipt = calculate_or_recover_shadow_run(run, client=_client(transport))
    db_session.flush()

    assert receipt.core["request_id"] == run.request_id
    assert [call["path"] for call in transport.calls] == [
        "/v1/shadow/receipts/lookup",
        "/v1/shadow/calculate",
        "/v1/shadow/receipts/lookup",
    ]
    assert len([call for call in transport.calls if call["path"].endswith("calculate")]) == 1


def test_restart_uses_local_immutable_receipt_without_transport(prepared_shadow, db_session):
    _event, _results, run, external_ids = prepared_shadow
    transport = FakeTransport()
    transport.queue("/v1/shadow/receipts/lookup", ShadowRemoteNotFound("missing"))
    transport.queue("/v1/shadow/calculate", _receipt_response(run, external_ids))
    first = calculate_or_recover_shadow_run(run, client=_client(transport))
    db_session.flush()

    restarted_transport = FakeTransport()
    restarted = calculate_or_recover_shadow_run(run, client=_client(restarted_transport))

    assert restarted.core_json == first.core_json
    assert restarted_transport.calls == []


def test_unreviewed_identity_blocks_before_transport(db_session, admin_user):
    tournament = make_tournament(db_session, year=2027)
    event = make_event(
        db_session,
        tournament,
        name="Underhand",
        stand_type="underhand",
        scoring_type="time",
        is_handicap=True,
    )
    event.handicap_authority_mode = "shadow"
    competitor = make_pro_competitor(db_session, tournament, "Unmapped Name", "M")
    make_event_result(db_session, event, competitor)
    db_session.add(
        WoodConfig(
            tournament_id=tournament.id,
            config_key="block_underhand_pro_M",
            species="Western White Pine",
            size_value=13.0,
            size_unit="in",
        )
    )
    db_session.flush()

    with pytest.raises(ShadowIdentityError, match="reviewed identity"):
        prepare_shadow_run(
            event,
            actor=admin_user,
            prediction_as_of=date(2027, 5, 8),
            schedule_fingerprint="1" * 64,
            observation_schema_version="strathmark.shadow-observation-fingerprint.v1",
            observation_fingerprint="2" * 64,
        )


def test_dual_run_target_is_rejected_before_transport(db_session, admin_user):
    tournament = make_tournament(db_session, year=2027)
    event = make_event(
        db_session,
        tournament,
        name="Speed Climb",
        stand_type="underhand",
        scoring_type="time",
        is_handicap=True,
        requires_dual_runs=True,
    )
    event.handicap_authority_mode = "shadow"

    with pytest.raises(ShadowUnsupportedEventError, match="single elapsed"):
        prepare_shadow_run(
            event,
            actor=admin_user,
            prediction_as_of=date(2027, 5, 8),
            schedule_fingerprint="1" * 64,
            observation_schema_version="strathmark.shadow-observation-fingerprint.v1",
            observation_fingerprint="2" * 64,
        )


def test_shadow_configuration_is_local_service_not_supabase(monkeypatch):
    for key in ("STRATHMARK_SUPABASE_URL", "STRATHMARK_SUPABASE_KEY"):
        monkeypatch.delenv(key, raising=False)
    config = ShadowClientConfig.from_mapping(
        {
            "STRATHMARK_SHADOW_URL": "http://127.0.0.1:8000",
            "STRATHMARK_SHADOW_CONSUMER_ID": "missoula:service:shadow",
            "STRATHMARK_SHADOW_SERVICE_TOKEN": "service-token-123456789",
            "STRATHMARK_SHADOW_ATTESTATION_KEY": "attestation-key-123456789",
        }
    )

    assert config.base_url == "http://127.0.0.1:8000"
    assert config.consumer_id == "missoula:service:shadow"
    assert (
        shadow_configuration_status(
            {
                "STRATHMARK_SHADOW_URL": config.base_url,
                "STRATHMARK_SHADOW_CONSUMER_ID": config.consumer_id,
                "STRATHMARK_SHADOW_SERVICE_TOKEN": config.service_token,
                "STRATHMARK_SHADOW_ATTESTATION_KEY": config.attestation_key,
            }
        )
        == "configured"
    )
