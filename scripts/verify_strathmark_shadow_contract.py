"""Verify Missoula against the installed STRATHMARK shadow contract and runtime.

This command is intentionally fail-closed. It uses a temporary local ledger and
evidence store, performs no network I/O, and proves that a Missoula-signed
calculate request can be replayed after a simulated STRATHMARK process restart
without loading the model artifact a second time.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

EXPECTED_CONTRACT_DIGEST = "8b0a11a6613c74ad7a5e01f3fe99d6bbede8b94dc7cdffe27930b5d0193d90db"
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


class InProcessTransport:
    """Adapt FastAPI TestClient to Missoula's bounded transport protocol."""

    def __init__(self, client):
        self.client = client
        self.calls: list[str] = []

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

        self.calls.append(path)
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
    import strathmark
    from fastapi.testclient import TestClient
    from strathmark.consumer_contract import (
        EXPECTED_SHADOW_CONSUMER_PATHS,
        load_shadow_consumer_contract,
        shadow_consumer_contract_digest,
    )
    from strathmark.ledger import PredictionLedger
    from strathmark.predictor import FilePredictionProvider

    from services.strathmark_shadow import (
        ShadowClientConfig,
        ShadowRemoteNotFound,
        StrathmarkShadowClient,
    )

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
    payload = contract["paths"]["/v1/shadow/calculate"]["post"]["requestBody"][
        "content"
    ]["application/json"]["example"]
    cutoff = date.fromisoformat(payload["prediction_as_of"])

    os.environ["STRATHMARK_SHADOW_SERVICE_CREDENTIALS"] = json.dumps(
        {CONSUMER_ID: SERVICE_TOKEN}
    )
    os.environ["STRATHMARK_SHADOW_ATTESTATION_KEYS"] = json.dumps(
        {CONSUMER_ID: ATTESTATION_KEY}
    )
    os.environ["STRATHMARK_TRUSTED_TOPOLOGY"] = "offline-single-writer-durable"

    with tempfile.TemporaryDirectory(prefix="missoula-strathmark-contract-") as raw:
        root = Path(raw)
        ledger_path = root / "prediction-ledger.db"
        store_path = root / "evidence.db"
        ledger = PredictionLedger(ledger_path)
        store = _prepare_empty_snapshot(store_path, cutoff)
        app = _install_overrides(
            ledger=ledger,
            store=store,
            provider=FilePredictionProvider(),
        )
        actor = SimpleNamespace(role="admin", shadow_actor_id=payload["operator_id"])
        run = SimpleNamespace(
            consumer_id=payload["consumer_id"],
            request_id=payload["request_id"],
            run_revision=payload["run_revision"],
        )
        config = ShadowClientConfig(
            base_url="http://strathmark.invalid",
            consumer_id=CONSUMER_ID,
            service_token=SERVICE_TOKEN,
            attestation_key=ATTESTATION_KEY,
        )
        try:
            first_transport = InProcessTransport(TestClient(app))
            client = StrathmarkShadowClient(config, transport=first_transport)
            try:
                client.lookup(run, actor)
            except ShadowRemoteNotFound:
                pass
            else:
                raise AssertionError("brand-new request unexpectedly had a receipt")
            first = client.calculate(run, actor, payload)
            first_core_json = first["receipt"]["core_json"]

            restarted_ledger = PredictionLedger(ledger_path)
            restarted_store = type(store)(store_path)
            restarted_app = _install_overrides(
                ledger=restarted_ledger,
                store=restarted_store,
                provider=ArtifactMustNotLoad(),
            )
            restarted_transport = InProcessTransport(TestClient(restarted_app))
            restarted_client = StrathmarkShadowClient(
                config,
                transport=restarted_transport,
            )
            replay = restarted_client.calculate(run, actor, payload)
            if replay["receipt"]["core_json"] != first_core_json:
                raise AssertionError("restart replay changed the immutable receipt core")
        finally:
            app.dependency_overrides.clear()

    return {
        "contract_digest": digest,
        "distribution_path": str(Path(strathmark.__file__).resolve()),
        "route_count": len(paths),
        "receipt_replay": "exact",
        "network_used": False,
        "production_data_used": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    result = verify()
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
