"""Frozen STRATHMARK V2 shadow consumer and recovery behavior."""

import base64
import copy
import hashlib
import hmac
import json
import time
from datetime import date
from unittest.mock import patch
from urllib import request

import pytest

from models import (
    CompetitorExternalIdentity,
    ShadowHandicapRun,
    ShadowReceiptRevision,
    User,
    WoodConfig,
)
from services.shadow_handicap_state import transition_shadow_run
from services.strathmark_shadow import (
    ShadowClientConfig,
    ShadowIdentityError,
    ShadowReceiptIntegrityError,
    ShadowRemoteError,
    ShadowRemoteNotFound,
    ShadowRemoteTimeout,
    ShadowUnsupportedEventError,
    StrathmarkShadowClient,
    UrllibShadowTransport,
    _ledger_request_id,
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


def _receipt_response(run, competitor_ids, *, active_fingerprint=None):
    from strathmark.consumer_contract import load_shadow_consumer_contract

    document = load_shadow_consumer_contract()
    response = copy.deepcopy(
        document["paths"]["/v1/shadow/calculate"]["post"]["responses"]["200"]["content"][
            "application/json"
        ]["example"]
    )
    core = response["receipt"]["core"]
    frozen = json.loads(run.input_snapshot_json)
    identity_keys = (
        "consumer_id",
        "tournament_id",
        "event_occurrence_id",
        "field_run_id",
        "operator_id",
        "request_id",
        "run_revision",
        "event_code",
        "target_contract",
        "prediction_as_of",
    )
    for key in identity_keys:
        core[key] = frozen[key]

    projection = core["request_projection"]
    for key in identity_keys:
        projection[key] = frozen[key]
    projection["schedule_fingerprint"] = frozen["schedule_fingerprint"]
    projection["observation_schema_version"] = frozen["observation_schema_version"]
    projection["observation_fingerprint"] = frozen["observation_fingerprint"]
    projection["seed"] = frozen["seed"]
    projection["competitors"] = [
        {
            "competitor_id": row["competitor_id"],
            "gender": row.get("gender") or "UNKNOWN",
        }
        for row in frozen["competitors"]
    ]
    projection["wood"] = {
        "species": frozen["wood"]["species"].upper(),
        "diameter_mm": frozen["wood"]["diameter_mm"],
        "quality": frozen["wood"]["quality"],
    }
    projection_core = {key: value for key, value in projection.items() if key != "fingerprint"}
    projection["fingerprint"] = hashlib.sha256(
        json.dumps(projection_core, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    for calculation in (core["active_input"]["caller_input"], core["calculation_input"]):
        calculation["event_code"] = frozen["event_code"]
        calculation["prediction_as_of"] = frozen["prediction_as_of"]
        calculation["diameter_mm"] = frozen["wood"]["diameter_mm"]
        calculation["species"] = frozen["wood"]["species"].upper()
        calculation["seed"] = frozen["seed"]
        calculation["competitors"] = [
            {
                "competitor_id": row["competitor_id"],
                "gender": row.get("gender") or "__MISSING__",
                "manual_time_override": None,
                "history": [],
            }
            for row in frozen["competitors"]
        ]
    active_input = core["active_input"]
    for key in (
        "tournament_id",
        "event_occurrence_id",
        "field_run_id",
        "target_contract",
        "schedule_fingerprint",
    ):
        active_input[key] = frozen[key]
    active_input["evidence_snapshot"]["cutoff"] = frozen["prediction_as_of"]
    active_core = {key: value for key, value in active_input.items() if key != "fingerprint"}
    active_input["fingerprint"] = (
        active_fingerprint
        or hashlib.sha256(
            json.dumps(active_core, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
    )

    core["observation"] = {
        "schema_version": frozen["observation_schema_version"],
        "fingerprint": frozen["observation_fingerprint"],
    }
    core["evidence_snapshot"]["cutoff"] = frozen["prediction_as_of"]
    prediction_template = core["predictions"][0]
    core["predictions"] = []
    core["evidence_diagnostics"] = []
    for ordinal, competitor_id in enumerate(competitor_ids):
        prediction = copy.deepcopy(prediction_template)
        prediction.update(
            {
                "ordinal": ordinal,
                "competitor_id": competitor_id,
                "prediction_id": f"strathmark:prediction:{ordinal:04d}",
                "event_code": frozen["event_code"],
                "assigned_mark": 3 + ordinal,
                "median_seconds": 40.0 + ordinal,
                "evidence_cutoff": frozen["prediction_as_of"],
            }
        )
        core["predictions"].append(prediction)
        core["evidence_diagnostics"].append(
            {
                "ordinal": ordinal,
                "competitor_id": competitor_id,
                "total_rows": 0,
                "included_rows": 0,
                "excluded_rows": 0,
                "excluded_by_reason": {},
                "canonicalization_version": "prediction-v2-evidence-v1",
            }
        )
    core_json = json.dumps(core, sort_keys=True, separators=(",", ":"))
    response["receipt"]["core_json"] = core_json
    response["receipt"]["core"] = core
    return response


def _contract_response(path):
    from strathmark.consumer_contract import load_shadow_consumer_contract

    document = load_shadow_consumer_contract()
    return copy.deepcopy(
        document["paths"][path]["post"]["responses"]["200"]["content"]["application/json"][
            "example"
        ]
    )


def _numeric_outcome_response(run, actor, payload, *, status="recorded"):
    core = json.loads(run.receipts[-1].core_json)
    predictions = {row["prediction_id"]: row for row in core["predictions"]}
    revisions = []
    for index, requested in enumerate(
        sorted(payload["revisions"], key=lambda row: row["prediction_id"])
    ):
        prediction = predictions[requested["prediction_id"]]
        actual_time = requested["actual_time"]
        revisions.append(
            {
                "revision_id": f"strathmark:numeric-revision:{index + 1}",
                "prediction_id": requested["prediction_id"],
                "revision": requested["expected_revision"] + 1,
                "competitor_id": requested["competitor_id"],
                "event_code": requested["event_code"],
                "action": requested["action"],
                "actual_time": actual_time,
                "residual": (
                    None
                    if requested["action"] == "void"
                    else actual_time - prediction["median_seconds"]
                ),
                "supersedes_revision_id": (
                    None
                    if requested["expected_revision"] == 0
                    else f"strathmark:numeric-revision:prior-{index + 1}"
                ),
                "created_at": "2027-05-09T12:00:00+00:00",
            }
        )
    return {
        "schema_version": "strathmark.shadow-numeric-outcome-response.v1",
        "outcome": {
            "outcome_revision_id": payload["outcome_revision_id"],
            "ledger_request_id": _ledger_request_id(payload["consumer_id"], payload["request_id"]),
            "caller_id": payload["consumer_id"],
            "revisions": revisions,
            "actor": actor.shadow_actor_id,
            "reason_code": payload.get("reason_code"),
            "created_at": "2027-05-09T12:00:00+00:00",
            "status": status,
            "cloud_status": "not_configured",
        },
    }


def _rewrite_receipt_core(response, mutation):
    mutation(response["receipt"]["core"])
    response["receipt"]["core_json"] = json.dumps(
        response["receipt"]["core"], sort_keys=True, separators=(",", ":")
    )
    return response


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
    assert (
        run.active_input_fingerprint == response["receipt"]["core"]["active_input"]["fingerprint"]
    )
    assert ShadowReceiptRevision.query.filter_by(run_id=run.id).count() == 1
    assert [(row.handicap_factor, row.predicted_time, row.mark_assigned_at) for row in results] == [
        (0.0, None, None),
        (0.0, None, None),
    ]


@pytest.mark.parametrize(
    ("path", "mutation"),
    (
        (
            "/v1/shadow/calculate",
            lambda response: response.update({"unexpected": True}),
        ),
        (
            "/v1/shadow/calculate",
            lambda response: response.pop("draft_predictions"),
        ),
        (
            "/v1/shadow/calculate",
            lambda response: response.update({"schema_version": "wrong"}),
        ),
        (
            "/v1/shadow/receipts/lookup",
            lambda response: response.update({"unexpected": True}),
        ),
        (
            "/v1/shadow/receipts/lookup",
            lambda response: response.pop("receipt"),
        ),
        (
            "/v1/shadow/receipts/lookup",
            lambda response: response.update({"schema_version": "wrong"}),
        ),
    ),
    ids=(
        "calculate-extra-field",
        "calculate-missing-field",
        "calculate-wrong-field",
        "lookup-extra-field",
        "lookup-missing-field",
        "lookup-wrong-field",
    ),
)
def test_calculate_and_lookup_responses_match_the_complete_pinned_contract(
    prepared_shadow,
    path,
    mutation,
):
    _event, _results, run, external_ids = prepared_shadow
    actor = User.query.filter_by(id=run.created_by_id).one()
    if path.endswith("calculate"):
        response = _receipt_response(run, external_ids)
    else:
        response = _receipt_response(run, external_ids)
        response["schema_version"] = "strathmark.shadow-receipt-lookup-response.v1"
        response.pop("trusted")
        response.pop("status")
        response.pop("draft_predictions")
    mutation(response)
    transport = FakeTransport()
    transport.queue(path, response)
    client = _client(transport)

    with pytest.raises(ShadowReceiptIntegrityError, match="response"):
        if path.endswith("calculate"):
            client.calculate(run, actor, json.loads(run.input_snapshot_json))
        else:
            client.lookup(run, actor)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda core: core.update({"event_code": "SB"}),
        lambda core: core.update({"target_contract": "best-of-three.v1"}),
        lambda core: core["predictions"][0].update({"ordinal": True}),
        lambda core: core["predictions"][0].pop("source"),
        lambda core: core["request_projection"]["wood"].update({"diameter_mm": 350.0}),
    ],
    ids=(
        "wrong-event",
        "wrong-target",
        "boolean-ordinal",
        "incomplete-prediction",
        "wrong-frozen-wood",
    ),
)
def test_receipt_contract_rejects_wrong_or_incomplete_frozen_evidence(
    prepared_shadow,
    mutation,
):
    _event, _results, run, external_ids = prepared_shadow
    response = _rewrite_receipt_core(_receipt_response(run, external_ids), mutation)
    transport = FakeTransport()
    transport.queue("/v1/shadow/receipts/lookup", ShadowRemoteNotFound("missing"))
    transport.queue("/v1/shadow/calculate", response)

    with pytest.raises(ShadowReceiptIntegrityError):
        calculate_or_recover_shadow_run(run, client=_client(transport))


@pytest.mark.parametrize("projection", ("request_projection", "active_input"))
def test_receipt_recomputes_embedded_fingerprints(prepared_shadow, projection):
    _event, _results, run, external_ids = prepared_shadow
    response = _receipt_response(run, external_ids)
    response["receipt"]["core"][projection]["fingerprint"] = "f" * 64
    response["receipt"]["core_json"] = json.dumps(
        response["receipt"]["core"], sort_keys=True, separators=(",", ":")
    )
    transport = FakeTransport()
    transport.queue("/v1/shadow/receipts/lookup", ShadowRemoteNotFound("missing"))
    transport.queue("/v1/shadow/calculate", response)

    with pytest.raises(ShadowReceiptIntegrityError, match="fingerprint"):
        calculate_or_recover_shadow_run(run, client=_client(transport))


def test_standing_block_uses_the_existing_woodboss_standing_key(db_session, admin_user):
    tournament = make_tournament(db_session, year=2027)
    event = make_event(
        db_session,
        tournament,
        name="Standing Block",
        event_type="pro",
        gender="M",
        stand_type="standing_block",
        scoring_type="time",
        is_handicap=True,
    )
    event.handicap_authority_mode = "shadow"
    competitor = make_pro_competitor(
        db_session, tournament, "Standing Competitor", "M", events=[event.name]
    )
    make_event_result(db_session, event, competitor)
    db_session.add(
        CompetitorExternalIdentity(
            competitor_uid=competitor.uid,
            namespace="strathmark",
            external_id="strathmark:competitor:standing-1",
            status="reviewed",
            reviewed_by_id=admin_user.id,
        )
    )
    db_session.add(
        WoodConfig(
            tournament_id=tournament.id,
            config_key="block_standing_pro_M",
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

    assert json.loads(run.input_snapshot_json)["wood"]["diameter_mm"] == pytest.approx(330.2)


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


@pytest.mark.parametrize(
    "base_url",
    ("http://strathmark.invalid", "http://localhost.evil:8000", "ftp://127.0.0.1:8000"),
)
def test_shadow_configuration_rejects_cleartext_non_loopback_origins(base_url):
    with pytest.raises(ValueError, match="HTTPS|loopback|HTTP"):
        ShadowClientConfig(
            base_url=base_url,
            consumer_id="missoula:service:shadow",
            service_token="service-token-123456789",
            attestation_key="attestation-key-123456789",
        )


def test_urllib_transport_installs_a_fail_closed_redirect_handler(monkeypatch):
    captured = {}

    class FakeOpener:
        def open(self, req, timeout):
            captured["request"] = req
            captured["timeout"] = timeout
            return type(
                "Response",
                (),
                {
                    "__enter__": lambda self: self,
                    "__exit__": lambda self, *args: None,
                    "read": lambda self, limit: b"{}",
                },
            )()

    def build_opener(*handlers):
        captured["handlers"] = handlers
        return FakeOpener()

    monkeypatch.setenv("HTTP_PROXY", "http://attacker.invalid:8080")
    monkeypatch.setenv("HTTPS_PROXY", "http://attacker.invalid:8080")
    monkeypatch.setattr("services.strathmark_shadow.request.build_opener", build_opener)
    monkeypatch.setattr(
        "services.strathmark_shadow.request.urlopen",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("credentialed transport must use the no-redirect opener")
        ),
    )

    UrllibShadowTransport().post(
        base_url="https://strathmark.example",
        path="/v1/shadow/status",
        payload={"schema_version": "test"},
        headers={"Authorization": "Bearer secret", "X-STRATHMARK-Actor-Attestation": "proof"},
        timeout_seconds=1.0,
    )

    proxy_handler, redirect_handler = captured["handlers"]
    assert isinstance(proxy_handler, request.ProxyHandler)
    assert proxy_handler.proxies == {}
    assert (
        redirect_handler.redirect_request(None, None, 307, "redirect", {}, "https://other.example")
        is None
    )
    assert captured["request"].full_url == "https://strathmark.example/v1/shadow/status"
    assert captured["request"].get_header("Authorization") == "Bearer secret"


def test_status_request_is_actor_and_run_bound(prepared_shadow):
    _event, _results, run, _external_ids = prepared_shadow
    transport = FakeTransport()
    response = _contract_response("/v1/shadow/status")
    transport.queue("/v1/shadow/status", response)

    status = _client(transport).status(run, User.query.filter_by(id=run.created_by_id).one())

    assert status["receipt_readiness"] == "ready"
    call = transport.calls[0]
    assert call["path"] == "/v1/shadow/status"
    assert call["payload"] == {
        "schema_version": "strathmark.shadow-status.v1",
        "consumer_id": run.consumer_id,
        "request_id": run.request_id,
        "run_revision": run.run_revision,
        "current_active_fingerprint": None,
        "model_version": None,
        "timeout_ms": 2000,
    }
    claims, _signature, _encoded = _decode_attestation(
        call["headers"]["X-STRATHMARK-Actor-Attestation"]
    )
    assert claims["action"] == "shadow.status.read"
    assert claims["subject_revision"] == run.run_revision
    assert claims["request_digest"] == canonical_shadow_request_digest(call["payload"])


@pytest.mark.parametrize(
    "mutation",
    (
        lambda response: response.update({"unexpected": True}),
        lambda response: response["status"].pop("numeric_revision_count"),
        lambda response: response["status"].update({"mirror_pending_count": -1}),
    ),
    ids=("extra-field", "missing-field", "invalid-count"),
)
def test_status_response_must_match_the_complete_pinned_contract(
    prepared_shadow,
    mutation,
):
    _event, _results, run, _external_ids = prepared_shadow
    actor = User.query.filter_by(id=run.created_by_id).one()
    response = _contract_response("/v1/shadow/status")
    mutation(response)
    transport = FakeTransport()
    transport.queue("/v1/shadow/status", response)

    with pytest.raises(ShadowReceiptIntegrityError, match="status response"):
        _client(transport).status(run, actor)


def test_disabled_actor_cannot_create_a_new_attestation(prepared_shadow):
    _event, _results, run, _external_ids = prepared_shadow
    actor = User.query.filter_by(id=run.created_by_id).one()
    actor.is_active_user = False
    transport = FakeTransport()

    with pytest.raises(ShadowIdentityError, match="active"):
        _client(transport).status(run, actor)

    assert transport.calls == []


def test_disabled_actor_can_still_recover_a_verified_local_receipt(
    prepared_shadow,
    db_session,
):
    _event, _results, run, external_ids = prepared_shadow
    transport = FakeTransport()
    transport.queue("/v1/shadow/receipts/lookup", ShadowRemoteNotFound("missing"))
    transport.queue("/v1/shadow/calculate", _receipt_response(run, external_ids))
    first = calculate_or_recover_shadow_run(run, client=_client(transport))
    db_session.flush()
    User.query.filter_by(id=run.created_by_id).one().is_active_user = False
    restarted_transport = FakeTransport()

    recovered = calculate_or_recover_shadow_run(run, client=_client(restarted_transport))

    assert recovered.core_json == first.core_json
    assert restarted_transport.calls == []


@pytest.mark.parametrize(
    "mutation",
    (
        lambda response: response.update({"unexpected": True}),
        lambda response: response["outcome"].update(
            {"outcome_revision_id": "missoula:outcome-revision:other"}
        ),
        lambda response: response["outcome"].update(
            {"ledger_request_id": "00000000-0000-0000-0000-000000000000"}
        ),
        lambda response: response["outcome"].update({"caller_id": "missoula:service:other"}),
        lambda response: response["outcome"].update({"actor": "missoula:operator:other"}),
        lambda response: response["outcome"].update({"reason_code": "corrected_time"}),
        lambda response: response["outcome"]["revisions"][0].update(
            {"prediction_id": "strathmark:prediction:other"}
        ),
        lambda response: response["outcome"]["revisions"][0].update(
            {"competitor_id": "strathmark:competitor:other"}
        ),
        lambda response: response["outcome"]["revisions"][0].update({"event_code": "SB"}),
        lambda response: response["outcome"]["revisions"][0].update({"actual_time": 43.5}),
        lambda response: response["outcome"]["revisions"][0].update({"revision": 2}),
        lambda response: response["outcome"]["revisions"][0].update({"residual": 999.0}),
        lambda response: response["outcome"]["revisions"][0].update(
            {"supersedes_revision_id": "strathmark:numeric-revision:unexpected"}
        ),
    ),
    ids=(
        "extra-field",
        "outcome-id",
        "ledger-request",
        "caller",
        "actor",
        "reason",
        "prediction",
        "competitor",
        "event",
        "actual-time",
        "revision",
        "residual",
        "supersedes",
    ),
)
def test_numeric_outcome_response_is_schema_valid_and_exactly_request_bound(
    prepared_shadow,
    db_session,
    mutation,
):
    _event, _results, run, external_ids = prepared_shadow
    actor = User.query.filter_by(id=run.created_by_id).one()
    transport = FakeTransport()
    transport.queue("/v1/shadow/receipts/lookup", ShadowRemoteNotFound("missing"))
    transport.queue("/v1/shadow/calculate", _receipt_response(run, external_ids))
    calculate_or_recover_shadow_run(run, client=_client(transport))
    db_session.flush()
    prediction = json.loads(run.receipts[-1].core_json)["predictions"][0]
    payload = {
        "schema_version": "strathmark.shadow-numeric-outcome.v1",
        "consumer_id": run.consumer_id,
        "request_id": run.request_id,
        "run_revision": run.run_revision,
        "outcome_revision_id": "missoula:outcome-revision:test-response-binding",
        "reason_code": None,
        "revisions": [
            {
                "prediction_id": prediction["prediction_id"],
                "competitor_id": prediction["competitor_id"],
                "event_code": prediction["event_code"],
                "expected_revision": 0,
                "action": "settle",
                "actual_time": 42.5,
            }
        ],
        "timeout_ms": 5000,
    }
    response = _numeric_outcome_response(run, actor, payload)
    mutation(response)
    transport.queue("/v1/shadow/outcomes/apply", response)

    with pytest.raises(ShadowReceiptIntegrityError, match="numeric outcome response"):
        _client(transport).apply_outcome(run, actor, payload)


def test_complete_numeric_outcome_response_is_accepted(prepared_shadow, db_session):
    _event, _results, run, external_ids = prepared_shadow
    actor = User.query.filter_by(id=run.created_by_id).one()
    transport = FakeTransport()
    transport.queue("/v1/shadow/receipts/lookup", ShadowRemoteNotFound("missing"))
    transport.queue("/v1/shadow/calculate", _receipt_response(run, external_ids))
    calculate_or_recover_shadow_run(run, client=_client(transport))
    db_session.flush()
    prediction = json.loads(run.receipts[-1].core_json)["predictions"][0]
    payload = {
        "schema_version": "strathmark.shadow-numeric-outcome.v1",
        "consumer_id": run.consumer_id,
        "request_id": run.request_id,
        "run_revision": run.run_revision,
        "outcome_revision_id": "missoula:outcome-revision:test-response-success",
        "reason_code": None,
        "revisions": [
            {
                "prediction_id": prediction["prediction_id"],
                "competitor_id": prediction["competitor_id"],
                "event_code": prediction["event_code"],
                "expected_revision": 0,
                "action": "settle",
                "actual_time": 42.5,
            }
        ],
        "timeout_ms": 5000,
    }
    expected = _numeric_outcome_response(run, actor, payload)
    transport.queue("/v1/shadow/outcomes/apply", expected)

    assert _client(transport).apply_outcome(run, actor, payload) == expected
