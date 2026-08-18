"""Verify Missoula against the installed STRATHMARK shadow contract and runtime.

This command is intentionally fail-closed. It uses a temporary local ledger and
evidence store, performs no network I/O, and proves that a Missoula-signed
calculate request can be replayed after a simulated STRATHMARK process restart
without loading the model artifact a second time.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import sys
import tempfile
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

EXPECTED_CONTRACT_DIGEST = "8b0a11a6613c74ad7a5e01f3fe99d6bbede8b94dc7cdffe27930b5d0193d90db"
EXPECTED_STRATHMARK_COMMIT = "da5c44d07311b226c1e9842104477efaf61253fa"
EXPECTED_PATHS = frozenset(
    {
        "/health",
        "/v1/shadow/calculate",
        "/v1/shadow/receipts/lookup",
        "/v1/shadow/status",
        "/v1/shadow/outcomes/apply",
        "/v1/shadow/mirror/replay",
        "/v1/shadow/drift",
    }
)
CONSUMER_ID = "missoula:service:shadow"
SERVICE_TOKEN = "missoula-release-rehearsal-service-token"
ATTESTATION_KEY = "missoula-release-rehearsal-attestation-key"
_ARTIFACT_ENVIRONMENT = (
    "STRATHMARK_PREDICTION_ENGINE",
    "STRATHMARK_PREDICTION_CORE_ARTIFACT",
    "STRATHMARK_PREDICTION_RESIDUAL_ARTIFACT",
)
_VERIFIER_ENVIRONMENT = (
    *_ARTIFACT_ENVIRONMENT,
    "DATABASE_URL",
    "FLASK_ENV",
    "PRODUCTION",
    "RAILWAY_ENVIRONMENT",
    "SECRET_KEY",
    "TESTING",
    "STRATHMARK_SUPABASE_URL",
    "STRATHMARK_SUPABASE_KEY",
    "STRATHMARK_SHADOW_SERVICE_CREDENTIALS",
    "STRATHMARK_SHADOW_ATTESTATION_KEYS",
    "STRATHMARK_TRUSTED_TOPOLOGY",
)


def _installed_distribution_provenance() -> dict[str, str]:
    import strathmark

    distribution = importlib.metadata.distribution("strathmark")
    direct_url_raw = distribution.read_text("direct_url.json")
    try:
        direct_url = json.loads(direct_url_raw or "")
        vcs = direct_url["vcs_info"]
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise AssertionError("installed STRATHMARK lacks PEP 610 VCS provenance") from exc
    if vcs.get("vcs") != "git" or vcs.get("commit_id") != EXPECTED_STRATHMARK_COMMIT:
        raise AssertionError(
            f"installed STRATHMARK is not the reviewed commit {EXPECTED_STRATHMARK_COMMIT}"
        )
    distribution_root = Path(distribution.locate_file("")).resolve()
    module_path = Path(strathmark.__file__).resolve()
    try:
        module_path.relative_to(distribution_root)
    except ValueError as exc:
        raise AssertionError("a source tree is shadowing the installed STRATHMARK package") from exc
    if direct_url.get("dir_info", {}).get("editable"):
        raise AssertionError("editable STRATHMARK installs are not release evidence")
    return {
        "commit_id": vcs["commit_id"],
        "direct_url": str(direct_url.get("url") or ""),
        "distribution_path": str(module_path),
    }


@contextmanager
def _isolated_runtime_environment(empty_cwd: Path, missoula_db_path: Path):
    missing = object()
    previous = {name: os.environ.get(name, missing) for name in _VERIFIER_ENVIRONMENT}
    previous_cwd = Path.cwd()
    try:
        for name in _ARTIFACT_ENVIRONMENT:
            os.environ.pop(name, None)
        for name in ("PRODUCTION", "RAILWAY_ENVIRONMENT", "STRATHMARK_SUPABASE_URL", "STRATHMARK_SUPABASE_KEY"):
            os.environ.pop(name, None)
        os.environ["DATABASE_URL"] = f"sqlite:///{missoula_db_path.as_posix()}"
        os.environ["FLASK_ENV"] = "testing"
        os.environ["TESTING"] = "1"
        os.environ["SECRET_KEY"] = "missoula-release-rehearsal-secret-key"
        os.environ["STRATHMARK_SHADOW_SERVICE_CREDENTIALS"] = json.dumps(
            {CONSUMER_ID: SERVICE_TOKEN}
        )
        os.environ["STRATHMARK_SHADOW_ATTESTATION_KEYS"] = json.dumps(
            {CONSUMER_ID: ATTESTATION_KEY}
        )
        os.environ["STRATHMARK_TRUSTED_TOPOLOGY"] = "offline-single-writer-durable"
        os.chdir(empty_cwd)
        yield
    finally:
        os.chdir(previous_cwd)
        for name, value in previous.items():
            if value is missing:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


class InProcessTransport:
    """Adapt FastAPI TestClient to Missoula's bounded transport protocol."""

    def __init__(self, client):
        self.client = client
        self.calls: list[dict[str, Any]] = []

    def post(
        self,
        *,
        base_url: str,
        path: str,
        payload: Mapping[str, Any],
        headers: Mapping[str, str],
        timeout_seconds: float,
    ) -> Mapping[str, Any]:
        del base_url, timeout_seconds
        from services.strathmark_shadow import (
            ShadowRemoteConflict,
            ShadowRemoteError,
            ShadowRemoteNotFound,
            ShadowRemoteTimeout,
            ShadowRemoteUnavailable,
        )

        self.calls.append({"path": path, "payload": dict(payload)})
        response = self.client.post(path, json=dict(payload), headers=dict(headers))
        detail = response.json().get("detail", "STRATHMARK request failed")
        errors = {
            404: ShadowRemoteNotFound,
            409: ShadowRemoteConflict,
            429: ShadowRemoteUnavailable,
            500: ShadowRemoteUnavailable,
            502: ShadowRemoteUnavailable,
            503: ShadowRemoteUnavailable,
            504: ShadowRemoteTimeout,
        }
        if response.status_code in errors:
            raise errors[response.status_code](detail, status_code=response.status_code)
        if response.status_code >= 400:
            raise ShadowRemoteError(detail, status_code=response.status_code)
        value = response.json()
        if not isinstance(value, dict):
            raise AssertionError("STRATHMARK response was not an object")
        return value


class ArtifactMustNotLoad:
    def snapshot(self, prediction_as_of):
        del prediction_as_of
        raise AssertionError("receipt replay attempted to load the prediction artifact")


def _prepare_empty_snapshot(path: Path, cutoff: date):
    from strathmark.store import (
        EVIDENCE_SNAPSHOT_SOURCE_SCHEMA_VERSION,
        EvidenceSnapshotPayload,
        ResultStore,
        canonical_evidence_source_digest,
    )

    store = ResultStore(path)
    captured_at = datetime.now(timezone.utc)
    source_id = "missoula:contract-rehearsal:empty"
    payload = EvidenceSnapshotPayload(
        schema_version=EVIDENCE_SNAPSHOT_SOURCE_SCHEMA_VERSION,
        source_id=source_id,
        cutoff=cutoff,
        captured_at=captured_at,
        rows=(),
        source_digest=canonical_evidence_source_digest(
            source_id=source_id,
            cutoff=cutoff,
            captured_at=captured_at,
            rows=(),
        ),
    )

    class Source:
        def load_snapshot(self, *, cutoff):
            if cutoff != payload.cutoff:
                raise AssertionError("evidence adapter received a different cutoff")
            return payload

    store.refresh_evidence_snapshot(Source(), cutoff=cutoff)
    return store


def _install_overrides(*, ledger, store, provider):
    from strathmark.api import app, get_ledger, get_shadow_service, get_store
    from strathmark.shadow import ShadowPredictionService

    service = ShadowPredictionService(
        ledger,
        prediction_provider=provider,
        result_store=store,
    )
    app.dependency_overrides[get_ledger] = lambda: ledger
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_shadow_service] = lambda: service
    return app


def verify() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="missoula-strathmark-contract-") as raw:
        root = Path(raw).resolve()
        empty_cwd = root / "empty-cwd"
        empty_cwd.mkdir()
        missoula_db_path = root / "missoula-rehearsal.db"
        with _isolated_runtime_environment(empty_cwd, missoula_db_path):
            return _verify_isolated(root=root, empty_cwd=empty_cwd)


def _verify_isolated(*, root: Path, empty_cwd: Path) -> dict[str, Any]:
    from alembic.script import ScriptDirectory
    from fastapi.testclient import TestClient
    from flask_migrate import upgrade
    from sqlalchemy import text
    from strathmark.consumer_contract import (
        EXPECTED_SHADOW_CONSUMER_PATHS,
        load_shadow_consumer_contract,
        shadow_consumer_contract_digest,
    )
    from strathmark.ledger import PredictionLedger
    from strathmark.predictor import FilePredictionProvider

    from app import create_app
    from database import db
    from models import (
        CompetitorExternalIdentity,
        Event,
        EventResult,
        ProCompetitor,
        Tournament,
        User,
        WoodConfig,
    )
    from services.shadow_handicap_state import transition_shadow_run
    from services.shadow_operator import build_shadow_schedule_fingerprint
    from services.shadow_settlement import capture_shadow_outcome_revisions
    from services.strathmark_shadow import (
        ShadowClientConfig,
        StrathmarkShadowClient,
        calculate_or_recover_shadow_run,
        prepare_shadow_run,
    )

    provenance = _installed_distribution_provenance()
    contract = load_shadow_consumer_contract()
    digest = shadow_consumer_contract_digest(document=contract)
    if digest != EXPECTED_CONTRACT_DIGEST:
        raise AssertionError(
            f"installed STRATHMARK contract digest {digest} does not match "
            f"Missoula's reviewed digest {EXPECTED_CONTRACT_DIGEST}"
        )
    paths = frozenset(contract["paths"])
    if paths != EXPECTED_PATHS or paths != EXPECTED_SHADOW_CONSUMER_PATHS:
        raise AssertionError("installed STRATHMARK route set does not match Missoula")
    contract_example = contract["paths"]["/v1/shadow/calculate"]["post"]["requestBody"][
        "content"
    ]["application/json"]["example"]
    cutoff = date(2027, 5, 8)

    ledger_path = root / "prediction-ledger.db"
    store_path = root / "evidence.db"
    missoula_db_path = root / "missoula-rehearsal.db"
    ledger = PredictionLedger(ledger_path)
    store = _prepare_empty_snapshot(store_path, cutoff)
    strathmark_app = _install_overrides(
        ledger=ledger,
        store=store,
        provider=FilePredictionProvider(),
    )
    config = ShadowClientConfig(
        base_url="https://strathmark.invalid",
        consumer_id=CONSUMER_ID,
        service_token=SERVICE_TOKEN,
        attestation_key=ATTESTATION_KEY,
    )
    missoula_app = create_app()
    missoula_app.config.update(
        TESTING=True,
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{missoula_db_path.as_posix()}",
        WTF_CSRF_ENABLED=False,
        WTF_CSRF_CHECK_DEFAULT=False,
    )
    calculate_payload: dict[str, Any] | None = None
    outcome_payload: dict[str, Any] | None = None
    first_core_json = ""
    status: Mapping[str, Any] = {}
    outcome_status = duplicate_status = ""
    calculate_payload_exact = numeric_payload_exact = False
    migration_at_head = False
    try:
        with missoula_app.app_context():
            db.engine.dispose()
            upgrade(directory=str(REPO_ROOT / "migrations"))
            migration_head = ScriptDirectory(str(REPO_ROOT / "migrations")).get_current_head()
            migration_at_head = (
                db.session.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
                == migration_head
            )

            actor = User(username="shadow_release_verifier", role=User.ROLE_ADMIN)
            actor.set_password("isolated-release-rehearsal")
            tournament = Tournament(name="Synthetic Missoula Rehearsal", year=2027, status="setup")
            db.session.add_all([actor, tournament])
            db.session.flush()
            event = Event(
                tournament_id=tournament.id,
                name="Underhand",
                event_type="pro",
                gender="M",
                scoring_type="time",
                scoring_order="lowest_wins",
                stand_type="underhand",
                max_stands=5,
                is_partnered=False,
                requires_dual_runs=False,
                requires_triple_runs=False,
                is_handicap=True,
                is_open=False,
                has_prelims=False,
                payouts="{}",
                status="pending",
                handicap_authority_mode="shadow",
            )
            competitor = ProCompetitor(
                tournament_id=tournament.id,
                name="Synthetic Competitor",
                gender="M",
                events_entered=json.dumps([event.name]),
                gear_sharing="{}",
                partners="{}",
                status="active",
                is_left_handed_springboard=False,
                springboard_slow_heat=False,
            )
            db.session.add_all([event, competitor])
            db.session.flush()
            result = EventResult(
                event_id=event.id,
                competitor_id=competitor.id,
                competitor_type="pro",
                competitor_name=competitor.name,
                handicap_factor=0.0,
                points_awarded=0,
                payout_amount=0.0,
                status="pending",
            )
            db.session.add_all(
                [
                    result,
                    CompetitorExternalIdentity(
                        competitor_uid=competitor.uid,
                        namespace="strathmark",
                        external_id="strathmark:competitor:release-rehearsal-001",
                        status="reviewed",
                        reviewed_by_id=actor.id,
                    ),
                    WoodConfig(
                        tournament_id=tournament.id,
                        config_key="block_underhand_pro_M",
                        species="Western White Pine",
                        size_value=13.0,
                        size_unit="in",
                    ),
                ]
            )
            db.session.flush()
            run = prepare_shadow_run(
                event,
                actor=actor,
                prediction_as_of=cutoff,
                schedule_fingerprint=build_shadow_schedule_fingerprint(event),
                observation_schema_version="strathmark.shadow-observation-fingerprint.v1",
                observation_fingerprint=hashlib.sha256(
                    b"synthetic-release-rehearsal-observation"
                ).hexdigest(),
            )
            transition_shadow_run(
                run,
                expected_version=run.lifecycle_version,
                lifecycle="preflight-approved",
                actor_id=actor.id,
                reason_code="release_rehearsal_preflight",
            )
            calculate_payload = json.loads(run.input_snapshot_json)

            first_transport = InProcessTransport(TestClient(strathmark_app))
            client = StrathmarkShadowClient(config, transport=first_transport)
            first = calculate_or_recover_shadow_run(run, client=client)
            first_core_json = first.core_json
            calculate_calls = [
                call for call in first_transport.calls if call["path"] == "/v1/shadow/calculate"
            ]
            calculate_payload_exact = (
                len(calculate_calls) == 1 and calculate_calls[0]["payload"] == calculate_payload
            )

            restarted_ledger = PredictionLedger(ledger_path)
            restarted_store = type(store)(store_path)
            restarted_app = _install_overrides(
                ledger=restarted_ledger,
                store=restarted_store,
                provider=ArtifactMustNotLoad(),
            )
            restarted_transport = InProcessTransport(TestClient(restarted_app))
            restarted_client = StrathmarkShadowClient(config, transport=restarted_transport)
            replay = restarted_client.calculate(run, actor, calculate_payload)
            if replay["receipt"]["core_json"] != first_core_json:
                raise AssertionError("restart replay changed the immutable receipt core")
            status = restarted_client.status(run, actor)
            if status.get("receipt_readiness") != "ready":
                raise AssertionError("installed STRATHMARK did not report a review-ready receipt")

            transition_shadow_run(
                run,
                expected_version=run.lifecycle_version,
                lifecycle="reviewed",
                actor_id=actor.id,
                reason_code="release_rehearsal_reviewed",
            )
            run.reviewed_by_id = actor.id
            transition_shadow_run(
                run,
                expected_version=run.lifecycle_version,
                lifecycle="shadow-issued",
                actor_id=actor.id,
                reason_code="release_rehearsal_issued",
            )
            run.issued_by_id = actor.id
            result.status = "completed"
            result.result_value = 42.5
            captured = capture_shadow_outcome_revisions(event, actor_id=actor.id)
            if captured.numeric_action_count != 1 or not run.settlement_outbox:
                raise AssertionError("Missoula did not build one numeric settlement intent")
            outcome_payload = json.loads(run.settlement_outbox[-1].payload_json)

            outcome = restarted_client.apply_outcome(run, actor, outcome_payload)
            outcome_status = outcome.get("outcome", {}).get("status")
            if outcome_status != "recorded":
                raise AssertionError("installed STRATHMARK did not record the numeric outcome")
            numeric_calls = [
                call
                for call in restarted_transport.calls
                if call["path"] == "/v1/shadow/outcomes/apply"
            ]
            numeric_payload_exact = (
                len(numeric_calls) == 1 and numeric_calls[0]["payload"] == outcome_payload
            )
            duplicate = restarted_client.apply_outcome(run, actor, outcome_payload)
            duplicate_status = duplicate.get("outcome", {}).get("status")
            if duplicate_status != "duplicate":
                raise AssertionError("numeric outcome retry was not idempotent")
            db.session.commit()
    finally:
        strathmark_app.dependency_overrides.clear()
        with missoula_app.app_context():
            db.session.remove()
            db.engine.dispose()

    if calculate_payload is None or outcome_payload is None:
        raise AssertionError("Missoula release rehearsal did not build its request payloads")

    data_evidence = {
        "contract_example_not_used_as_input": calculate_payload != contract_example,
        "temporary_ledger": _is_within(ledger_path, root),
        "temporary_evidence_store": _is_within(store_path, root),
        "temporary_missoula_database": _is_within(missoula_db_path, root),
        "missoula_database_migrated": migration_at_head,
        "calculate_payload_from_missoula_builder": calculate_payload_exact,
        "numeric_payload_from_missoula_builder": numeric_payload_exact,
        "empty_working_directory": Path.cwd().resolve() == empty_cwd.resolve(),
        "artifact_environment_scrubbed": all(
            name not in os.environ for name in _ARTIFACT_ENVIRONMENT
        ),
    }
    production_data_used = not all(data_evidence.values())
    if production_data_used:
        raise AssertionError("release rehearsal could not prove synthetic data isolation")
    return {
        "contract_digest": digest,
        "distribution_path": provenance["distribution_path"],
        "distribution_commit": provenance["commit_id"],
        "distribution_direct_url": provenance["direct_url"],
        "route_count": len(paths),
        "receipt_replay": "exact",
        "status_readiness": status["receipt_readiness"],
        "numeric_outcome": outcome_status,
        "numeric_outcome_retry": duplicate_status,
        "network_used": False,
        "production_data_used": production_data_used,
        "data_isolation": data_evidence,
    }


def _is_within(path: Path, root: Path) -> bool:
    return path.resolve().is_relative_to(root.resolve())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    result = verify()
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
