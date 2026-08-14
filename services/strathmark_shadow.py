"""One strict, receipt-first adapter for STRATHMARK V2 shadow operations.

The legacy official mark-assignment service remains separate.  This module
never writes ``EventResult.handicap_factor``, ``predicted_time``, or
``mark_assigned_at`` and never loads Supabase data.  It sends only stable
pseudonymous identities and the frozen request projection over the versioned
local STRATHMARK HTTP contract.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import secrets
import socket
import time
import uuid
from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping, Protocol
from urllib import error, parse, request

from database import db
from models import (
    CollegeCompetitor,
    CompetitorExternalIdentity,
    Event,
    EventResult,
    ProCompetitor,
    ShadowHandicapRun,
    ShadowReceiptRevision,
    User,
    WoodConfig,
)
from services.shadow_handicap_state import transition_shadow_run

DEFAULT_CONSUMER_ID = "missoula:service:shadow"
CALCULATE_SCHEMA_VERSION = "strathmark.shadow-calculate.v1"
LOOKUP_SCHEMA_VERSION = "strathmark.shadow-receipt-lookup.v1"
STATUS_SCHEMA_VERSION = "strathmark.shadow-status.v1"
ATTESTATION_SCHEMA_VERSION = "strathmark.actor-attestation.v2"
REQUEST_DIGEST_SCHEMA_VERSION = "strathmark.shadow-request-digest.v1"
ATTESTATION_AUDIENCE = "strathmark.shadow.v1"
OBSERVATION_SCHEMA_VERSION = "strathmark.shadow-observation-fingerprint.v1"
TARGET_CONTRACT = "single-elapsed-seconds.v1"

_EVENT_CODE = {"standing_block": "SB", "underhand": "UH"}


class ShadowAdapterError(RuntimeError):
    """Base error for the isolated shadow integration."""


class ShadowIdentityError(ShadowAdapterError):
    """A stable reviewed cross-system identity is missing or conflicted."""


class ShadowUnsupportedEventError(ShadowAdapterError):
    """The event does not implement the frozen single-result target."""


class ShadowReceiptIntegrityError(ShadowAdapterError):
    """Remote or persisted receipt evidence does not match the immutable run."""


class ShadowRemoteError(ShadowAdapterError):
    def __init__(self, detail: str, *, status_code: int | None = None):
        super().__init__(detail)
        self.status_code = status_code


class ShadowRemoteNotFound(ShadowRemoteError):
    pass


class ShadowRemoteConflict(ShadowRemoteError):
    pass


class ShadowRemoteTimeout(ShadowRemoteError):
    pass


class ShadowRemoteUnavailable(ShadowRemoteError):
    pass


class ShadowTransport(Protocol):
    def post(
        self,
        *,
        base_url: str,
        path: str,
        payload: Mapping[str, Any],
        headers: Mapping[str, str],
        timeout_seconds: float,
    ) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class ShadowClientConfig:
    base_url: str
    consumer_id: str
    service_token: str
    attestation_key: str

    def __post_init__(self):
        parsed = parse.urlparse(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("STRATHMARK shadow URL must be an absolute HTTP(S) URL")
        _require_namespaced(self.consumer_id, "consumer_id")
        for field, value in (
            ("service_token", self.service_token),
            ("attestation_key", self.attestation_key),
        ):
            if not isinstance(value, str) or not 16 <= len(value) <= 4096 or not value.isascii():
                raise ValueError(f"{field} must contain 16 to 4096 ASCII characters")
        if hmac.compare_digest(self.service_token, self.attestation_key):
            raise ValueError("service_token and attestation_key must be distinct")

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> "ShadowClientConfig":
        required = {
            "base_url": "STRATHMARK_SHADOW_URL",
            "consumer_id": "STRATHMARK_SHADOW_CONSUMER_ID",
            "service_token": "STRATHMARK_SHADOW_SERVICE_TOKEN",
            "attestation_key": "STRATHMARK_SHADOW_ATTESTATION_KEY",
        }
        missing = [env_name for env_name in required.values() if not values.get(env_name)]
        if missing:
            raise ValueError("STRATHMARK shadow service is not fully configured")
        return cls(**{field: str(values[env_name]).strip() for field, env_name in required.items()})


def shadow_configuration_status(values: Mapping[str, Any]) -> str:
    """Return configured/not-configured/invalid without exposing secret metadata."""
    names = (
        "STRATHMARK_SHADOW_URL",
        "STRATHMARK_SHADOW_CONSUMER_ID",
        "STRATHMARK_SHADOW_SERVICE_TOKEN",
        "STRATHMARK_SHADOW_ATTESTATION_KEY",
    )
    present = [bool(values.get(name)) for name in names]
    if not any(present):
        return "not-configured"
    if not all(present):
        return "invalid"
    try:
        ShadowClientConfig.from_mapping(values)
    except ValueError:
        return "invalid"
    return "configured"


@dataclass(frozen=True)
class StoredShadowReceipt:
    core_json: str
    core: Mapping[str, Any]
    status: Mapping[str, Any]


class UrllibShadowTransport:
    """Bounded standard-library JSON transport; no cloud client dependency."""

    def post(
        self,
        *,
        base_url: str,
        path: str,
        payload: Mapping[str, Any],
        headers: Mapping[str, str],
        timeout_seconds: float,
    ) -> Mapping[str, Any]:
        body = _canonical_json(payload).encode("utf-8")
        req = request.Request(
            f"{base_url.rstrip('/')}{path}",
            data=body,
            headers={**dict(headers), "Content-Length": str(len(body))},
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=timeout_seconds) as response:
                raw = response.read(1_048_577)
                if len(raw) > 1_048_576:
                    raise ShadowRemoteError("STRATHMARK response exceeded the local bound")
                value = json.loads(raw)
        except error.HTTPError as exc:
            detail = _http_error_detail(exc)
            if exc.code == 404:
                raise ShadowRemoteNotFound(detail, status_code=exc.code) from exc
            if exc.code == 409:
                raise ShadowRemoteConflict(detail, status_code=exc.code) from exc
            if exc.code == 504:
                raise ShadowRemoteTimeout(detail, status_code=exc.code) from exc
            if exc.code in {429, 500, 502, 503}:
                raise ShadowRemoteUnavailable(detail, status_code=exc.code) from exc
            raise ShadowRemoteError(detail, status_code=exc.code) from exc
        except (TimeoutError, socket.timeout) as exc:
            raise ShadowRemoteTimeout("STRATHMARK request timed out") from exc
        except error.URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                raise ShadowRemoteTimeout("STRATHMARK request timed out") from exc
            raise ShadowRemoteUnavailable("STRATHMARK local service is unavailable") from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ShadowRemoteError("STRATHMARK returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise ShadowRemoteError("STRATHMARK returned an invalid response object")
        return value


class StrathmarkShadowClient:
    def __init__(
        self,
        config: ShadowClientConfig,
        *,
        transport: ShadowTransport | None = None,
    ):
        self.config = config
        self.transport = transport or UrllibShadowTransport()

    def lookup(self, run: ShadowHandicapRun, actor: User) -> Mapping[str, Any]:
        payload = {
            "schema_version": LOOKUP_SCHEMA_VERSION,
            "consumer_id": run.consumer_id,
            "request_id": run.request_id,
            "run_revision": run.run_revision,
            "current_active_fingerprint": None,
            "timeout_ms": 2000,
        }
        return self._post(
            path="/v1/shadow/receipts/lookup",
            action="shadow.receipt.lookup",
            subject_revision=run.run_revision,
            actor=actor,
            payload=payload,
            timeout_ms=2000,
        )

    def calculate(
        self,
        run: ShadowHandicapRun,
        actor: User,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        return self._post(
            path="/v1/shadow/calculate",
            action="shadow.calculate",
            subject_revision=run.run_revision,
            actor=actor,
            payload=payload,
            timeout_ms=int(payload.get("timeout_ms", 5000)),
        )

    def status(self, run: ShadowHandicapRun, actor: User) -> Mapping[str, Any]:
        payload = {
            "schema_version": STATUS_SCHEMA_VERSION,
            "consumer_id": run.consumer_id,
            "request_id": run.request_id,
            "run_revision": run.run_revision,
            "current_active_fingerprint": None,
            "model_version": None,
            "timeout_ms": 2000,
        }
        response = self._post(
            path="/v1/shadow/status",
            action="shadow.status.read",
            subject_revision=run.run_revision,
            actor=actor,
            payload=payload,
            timeout_ms=2000,
        )
        status = response.get("status")
        if not isinstance(status, dict):
            raise ShadowRemoteError("STRATHMARK status response is invalid")
        return status

    def _post(
        self,
        *,
        path: str,
        action: str,
        subject_revision: str,
        actor: User,
        payload: Mapping[str, Any],
        timeout_ms: int,
    ) -> Mapping[str, Any]:
        if self.config.consumer_id != payload.get("consumer_id"):
            raise ShadowIdentityError("configured consumer does not match the frozen run")
        if actor.role not in {User.ROLE_ADMIN, User.ROLE_JUDGE}:
            raise ShadowIdentityError("authenticated role is not authorized for shadow operation")
        request_digest = canonical_shadow_request_digest(payload)
        issued_at = int(time.time())
        claims = {
            "schema_version": ATTESTATION_SCHEMA_VERSION,
            "consumer_id": self.config.consumer_id,
            "actor_id": actor.shadow_actor_id,
            "roles": [actor.role],
            "action": action,
            "subject_revision": subject_revision,
            "request_digest_schema_version": REQUEST_DIGEST_SCHEMA_VERSION,
            "request_digest": request_digest,
            "audience": ATTESTATION_AUDIENCE,
            "nonce": secrets.token_urlsafe(24),
            "issued_at": issued_at,
            "expires_at": issued_at + 60,
        }
        attestation = _sign_actor_attestation(claims, self.config.attestation_key)
        return self.transport.post(
            base_url=self.config.base_url,
            path=path,
            payload=payload,
            headers={
                "Authorization": f"Bearer {self.config.service_token}",
                "X-STRATHMARK-Actor-Attestation": attestation,
                "Content-Type": "application/json",
            },
            timeout_seconds=timeout_ms / 1000.0 + 0.25,
        )


def canonical_shadow_request_digest(payload: Mapping[str, Any]) -> str:
    envelope = {
        "schema_version": REQUEST_DIGEST_SCHEMA_VERSION,
        "payload": _normalize_canonical_json(dict(payload)),
    }
    return hashlib.sha256(_canonical_json(envelope).encode("utf-8")).hexdigest()


def prepare_shadow_run(
    event: Event,
    *,
    actor: User,
    prediction_as_of: date,
    schedule_fingerprint: str,
    observation_schema_version: str,
    observation_fingerprint: str,
    consumer_id: str = DEFAULT_CONSUMER_ID,
    seed: int = 20260811,
    supersedes_run: ShadowHandicapRun | None = None,
) -> ShadowHandicapRun:
    """Freeze one scoring-inert whole-field request from reviewed local state."""

    event_code = _validate_shadow_event(event)
    if actor.role not in {User.ROLE_ADMIN, User.ROLE_JUDGE}:
        raise ShadowIdentityError("judge or admin role required for shadow preparation")
    _require_namespaced(actor.shadow_actor_id, "operator_id")
    _require_sha256(schedule_fingerprint, "schedule_fingerprint")
    _require_sha256(observation_fingerprint, "observation_fingerprint")
    if observation_schema_version != OBSERVATION_SCHEMA_VERSION:
        raise ValueError("unsupported observation fingerprint schema")
    if not isinstance(prediction_as_of, date):
        raise ValueError("prediction_as_of must be an explicit date")
    if supersedes_run is not None and (
        supersedes_run.event_id != event.id or supersedes_run.consumer_id != consumer_id
    ):
        raise ShadowIdentityError("a superseding run must belong to the same event and consumer")

    results = (
        EventResult.query.filter_by(event_id=event.id)
        .filter(EventResult.status == "pending")
        .order_by(EventResult.id)
        .all()
    )
    if not results:
        raise ShadowIdentityError("shadow field has no pending entrants")
    competitors = _reviewed_competitors(event, results)
    wood = _wood_payload(event)

    run_id = f"missoula:shadow-run:{uuid.uuid4()}"
    request_id = f"missoula:request:{uuid.uuid4()}"
    run_revision = f"missoula:run-revision:{uuid.uuid4()}"
    field_run_id = f"missoula:field-run:{uuid.uuid4()}"
    payload = {
        "schema_version": CALCULATE_SCHEMA_VERSION,
        "consumer_id": consumer_id,
        "tournament_id": event.tournament.shadow_tournament_id,
        "event_occurrence_id": event.shadow_event_occurrence_id,
        "field_run_id": field_run_id,
        "operator_id": actor.shadow_actor_id,
        "request_id": request_id,
        "run_revision": run_revision,
        "event_code": event_code,
        "target_contract": TARGET_CONTRACT,
        "prediction_as_of": prediction_as_of.isoformat(),
        "schedule_fingerprint": schedule_fingerprint,
        "observation_schema_version": observation_schema_version,
        "observation_fingerprint": observation_fingerprint,
        "competitors": competitors,
        "wood": wood,
        "seed": seed,
        "timeout_ms": 5000,
    }
    snapshot_json = _canonical_json(payload)
    active_fingerprint = _sha256(
        {
            "schema_version": "missoula.shadow-active-input.v1",
            "tournament_id": payload["tournament_id"],
            "event_occurrence_id": payload["event_occurrence_id"],
            "field_run_id": payload["field_run_id"],
            "event_code": event_code,
            "prediction_as_of": payload["prediction_as_of"],
            "schedule_fingerprint": schedule_fingerprint,
            "competitors": competitors,
            "wood": wood,
            "seed": seed,
        }
    )
    run = ShadowHandicapRun(
        run_id=run_id,
        request_id=request_id,
        consumer_id=consumer_id,
        tournament_id=event.tournament_id,
        event_id=event.id,
        event_occurrence_id=event.shadow_event_occurrence_id,
        field_run_id=field_run_id,
        run_revision=run_revision,
        supersedes_run_id=supersedes_run.id if supersedes_run is not None else None,
        authority="shadow",
        lifecycle="prepared",
        lifecycle_version=1,
        prediction_as_of=prediction_as_of,
        roster_fingerprint=_sha256(competitors),
        schedule_fingerprint=schedule_fingerprint,
        wood_fingerprint=_sha256(wood),
        active_input_fingerprint=active_fingerprint,
        observation_schema_version=observation_schema_version,
        observation_fingerprint=observation_fingerprint,
        input_snapshot_json=snapshot_json,
        input_snapshot_sha256=hashlib.sha256(snapshot_json.encode("utf-8")).hexdigest(),
        created_by_id=actor.id,
    )
    db.session.add(run)
    db.session.flush()
    return run


def calculate_or_recover_shadow_run(
    run: ShadowHandicapRun,
    *,
    client: StrathmarkShadowClient,
) -> StoredShadowReceipt:
    """Return local receipt, lookup remote receipt, or calculate once then recover."""

    local = _local_receipt(run)
    if local is not None:
        return local
    if run.lifecycle != "preflight-approved":
        raise ValueError("shadow run must be preflight-approved before calculation")
    actor = db.session.get(User, run.created_by_id)
    if actor is None:
        raise ShadowIdentityError("shadow run actor no longer exists")
    payload = _load_frozen_input(run)

    try:
        remote = client.lookup(run, actor)
    except ShadowRemoteNotFound:
        remote = None
    if remote is not None:
        return _persist_remote_receipt(run, remote, source="lookup")

    try:
        response = client.calculate(run, actor, payload)
    except ShadowRemoteTimeout:
        try:
            recovered = client.lookup(run, actor)
        except ShadowRemoteNotFound:
            raise ShadowRemoteTimeout(
                "calculation timed out with no recoverable receipt; do not recalculate blindly"
            ) from None
        return _persist_remote_receipt(run, recovered, source="timeout-lookup")
    if response.get("trusted") is not True:
        raise ShadowReceiptIntegrityError("STRATHMARK did not return a trusted field receipt")
    return _persist_remote_receipt(run, response, source="calculate")


def _local_receipt(run: ShadowHandicapRun) -> StoredShadowReceipt | None:
    if not run.receipts:
        return None
    row = run.receipts[-1]
    core = _validate_core_json(row.core_json)
    if hashlib.sha256(row.core_json.encode("utf-8")).hexdigest() != row.core_sha256:
        raise ShadowReceiptIntegrityError("persisted receipt digest mismatch")
    _validate_receipt_identity(run, core)
    return StoredShadowReceipt(
        core_json=row.core_json,
        core=core,
        status={
            "trust": "recorded",
            "freshness": (
                "current"
                if core.get("active_input", {}).get("fingerprint") == run.active_input_fingerprint
                else "stale"
            ),
        },
    )


def load_local_shadow_receipt(run: ShadowHandicapRun) -> StoredShadowReceipt | None:
    """Return a digest- and identity-verified local receipt, if one exists."""

    return _local_receipt(run)


def _persist_remote_receipt(
    run: ShadowHandicapRun,
    response: Mapping[str, Any],
    *,
    source: str,
) -> StoredShadowReceipt:
    receipt = response.get("receipt")
    if not isinstance(receipt, dict):
        raise ShadowReceiptIntegrityError(f"{source} response omitted the receipt")
    core_json = receipt.get("core_json")
    supplied_core = receipt.get("core")
    if not isinstance(core_json, str) or not isinstance(supplied_core, dict):
        raise ShadowReceiptIntegrityError("receipt core projection is invalid")
    if not 1 <= len(core_json.encode("utf-8")) <= 262_144:
        raise ShadowReceiptIntegrityError("receipt core exceeds the local bound")
    core = _validate_core_json(core_json)
    if core != supplied_core or _canonical_core_json(core) != core_json:
        raise ShadowReceiptIntegrityError("receipt core JSON is not canonical or self-consistent")
    prediction_count = _validate_receipt_identity(run, core)
    active_fingerprint = core.get("active_input", {}).get("fingerprint")
    _require_sha256(active_fingerprint, "receipt active fingerprint")
    status = receipt.get("status")
    if not isinstance(status, dict) or status.get("trust") != "recorded":
        raise ShadowReceiptIntegrityError("receipt is not locally trusted by STRATHMARK")

    row = ShadowReceiptRevision(
        revision=1,
        schema_version="strathmark.shadow-receipt-core.v1",
        core_json=core_json,
        core_sha256=hashlib.sha256(core_json.encode("utf-8")).hexdigest(),
        prediction_count=prediction_count,
        ledger_request_id=f"strathmark:ledger-request:{uuid.uuid5(uuid.NAMESPACE_URL, run.request_id)}",
    )
    run.receipts.append(row)
    run.active_input_fingerprint = active_fingerprint
    transition_shadow_run(
        run,
        expected_version=run.lifecycle_version,
        lifecycle="calculated",
        actor_id=run.created_by_id,
        reason_code="trusted_receipt_recorded",
    )
    db.session.flush()
    return StoredShadowReceipt(core_json=core_json, core=core, status=dict(status))


def _load_frozen_input(run: ShadowHandicapRun) -> Mapping[str, Any]:
    digest = hashlib.sha256(run.input_snapshot_json.encode("utf-8")).hexdigest()
    if not hmac.compare_digest(digest, run.input_snapshot_sha256):
        raise ShadowReceiptIntegrityError("frozen shadow input digest mismatch")
    value = _validate_core_json(run.input_snapshot_json)
    if value.get("schema_version") != CALCULATE_SCHEMA_VERSION:
        raise ShadowReceiptIntegrityError("frozen shadow input schema mismatch")
    for key, expected in (
        ("consumer_id", run.consumer_id),
        ("request_id", run.request_id),
        ("run_revision", run.run_revision),
    ):
        if value.get(key) != expected:
            raise ShadowReceiptIntegrityError(f"frozen shadow input {key} mismatch")
    return value


def _validate_receipt_identity(run: ShadowHandicapRun, core: Mapping[str, Any]) -> int:
    if core.get("schema_version") != "strathmark.shadow-receipt-core.v1":
        raise ShadowReceiptIntegrityError("receipt schema mismatch")
    for key, expected in (
        ("consumer_id", run.consumer_id),
        ("request_id", run.request_id),
        ("run_revision", run.run_revision),
    ):
        if core.get(key) != expected:
            raise ShadowReceiptIntegrityError(f"receipt {key} mismatch")
    input_payload = _load_frozen_input(run)
    expected_ids = [row["competitor_id"] for row in input_payload["competitors"]]
    predictions = core.get("predictions")
    if not isinstance(predictions, list) or not predictions:
        raise ShadowReceiptIntegrityError("receipt predictions are incomplete")
    actual_ids = [row.get("competitor_id") for row in predictions if isinstance(row, dict)]
    if actual_ids != expected_ids or len(actual_ids) != len(predictions):
        raise ShadowReceiptIntegrityError("receipt prediction field does not match frozen entrants")
    prediction_ids = [row.get("prediction_id") for row in predictions]
    if any(not isinstance(value, str) or not value for value in prediction_ids):
        raise ShadowReceiptIntegrityError("receipt prediction identity is missing")
    if len(set(prediction_ids)) != len(prediction_ids):
        raise ShadowReceiptIntegrityError("receipt prediction identities are not unique")
    return len(predictions)


def _validate_shadow_event(event: Event) -> str:
    if not event.is_handicap or event.handicap_authority_mode != "shadow":
        raise ShadowUnsupportedEventError("event is not configured for shadow handicap mode")
    if event.scoring_type != "time":
        raise ShadowUnsupportedEventError("shadow V2 supports elapsed-time events only")
    if event.requires_dual_runs or event.requires_triple_runs:
        raise ShadowUnsupportedEventError(
            "shadow V2 supports one authoritative single elapsed result only"
        )
    event_code = _EVENT_CODE.get(event.stand_type)
    if event_code is None:
        raise ShadowUnsupportedEventError("event is outside the frozen SB/UH target contract")
    return event_code


def _reviewed_competitors(event: Event, results: list[EventResult]) -> list[dict[str, Any]]:
    ids = [row.competitor_id for row in results]
    model = CollegeCompetitor if event.event_type == "college" else ProCompetitor
    competitors = model.query.filter(model.id.in_(ids)).all()
    by_id = {row.id: row for row in competitors}
    uids = [row.uid for row in competitors]
    mappings = CompetitorExternalIdentity.query.filter(
        CompetitorExternalIdentity.competitor_uid.in_(uids),
        CompetitorExternalIdentity.namespace == "strathmark",
        CompetitorExternalIdentity.status == "reviewed",
    ).all()
    by_uid = {row.competitor_uid: row for row in mappings}

    output = []
    for result in results:
        competitor = by_id.get(result.competitor_id)
        mapping = by_uid.get(getattr(competitor, "uid", None))
        if competitor is None or mapping is None:
            raise ShadowIdentityError("every shadow entrant requires one reviewed identity mapping")
        output.append(
            {
                "competitor_id": mapping.external_id,
                "gender": competitor.gender if competitor.gender in {"M", "F"} else None,
            }
        )
    if len({row["competitor_id"] for row in output}) != len(output):
        raise ShadowIdentityError("shadow entrant identities are ambiguous")
    return output


def _wood_payload(event: Event) -> dict[str, Any]:
    gender_suffix = f"_{event.gender}" if event.gender else "_M"
    keys = [
        f"block_{event.stand_type}_{event.event_type}{gender_suffix}",
        f"block_{event.stand_type}_{event.event_type}",
    ]
    row = (
        WoodConfig.query.filter(
            WoodConfig.tournament_id == event.tournament_id,
            WoodConfig.config_key.in_(keys),
        )
        .order_by(WoodConfig.id)
        .first()
    )
    if row is None or not row.species or row.size_value is None:
        raise ValueError("shadow calculation requires explicit wood species and size")
    diameter_mm = float(row.size_value) * 25.4 if row.size_unit == "in" else float(row.size_value)
    if not 225 <= diameter_mm <= 500:
        raise ValueError("shadow wood diameter must be between 225 and 500 mm")
    return {"species": row.species, "diameter_mm": diameter_mm, "quality": 5}


def _normalize_canonical_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("shadow request keys must be strings")
        return {key: _normalize_canonical_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize_canonical_json(item) for item in value]
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("shadow request numbers must be finite")
        return int(value) if value.is_integer() else value
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise ValueError("shadow request must contain canonical JSON values")


def _canonical_json(value: Any) -> str:
    return json.dumps(
        _normalize_canonical_json(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def _canonical_core_json(value: Mapping[str, Any]) -> str:
    """Match STRATHMARK's persisted core encoding without numeric coercion."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _sign_actor_attestation(payload: Mapping[str, Any], signing_key: str) -> str:
    canonical = _canonical_json(payload).encode("utf-8")
    encoded = base64.urlsafe_b64encode(canonical).rstrip(b"=").decode("ascii")
    signature = hmac.new(signing_key.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256)
    encoded_signature = base64.urlsafe_b64encode(signature.digest()).rstrip(b"=").decode("ascii")
    return f"{encoded}.{encoded_signature}"


def _require_namespaced(value: Any, field: str) -> str:
    text = str(value or "")
    parts = text.split(":", 2)
    if len(parts) != 3 or not all(parts) or len(text) > 224:
        raise ValueError(f"{field} must be a bounded namespaced identifier")
    return text


def _require_sha256(value: Any, field: str) -> str:
    text = str(value or "")
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return text


def _validate_core_json(value: str) -> Mapping[str, Any]:
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ShadowReceiptIntegrityError("receipt JSON is invalid") from exc
    if not isinstance(parsed, dict):
        raise ShadowReceiptIntegrityError("receipt JSON must be an object")
    return parsed


def _http_error_detail(exc: error.HTTPError) -> str:
    try:
        raw = exc.read(16_385)
        if len(raw) > 16_384:
            return "STRATHMARK request failed"
        value = json.loads(raw)
        detail = value.get("detail") if isinstance(value, dict) else None
        if isinstance(detail, str) and detail:
            return detail[:500]
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        pass
    return "STRATHMARK request failed"


__all__ = [
    "ShadowAdapterError",
    "ShadowClientConfig",
    "ShadowIdentityError",
    "ShadowReceiptIntegrityError",
    "ShadowRemoteConflict",
    "ShadowRemoteError",
    "ShadowRemoteNotFound",
    "ShadowRemoteTimeout",
    "ShadowRemoteUnavailable",
    "ShadowUnsupportedEventError",
    "StoredShadowReceipt",
    "StrathmarkShadowClient",
    "calculate_or_recover_shadow_run",
    "canonical_shadow_request_digest",
    "load_local_shadow_receipt",
    "prepare_shadow_run",
    "shadow_configuration_status",
]
