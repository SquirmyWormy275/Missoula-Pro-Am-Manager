"""Prospective, numerically inactive context observations for future seasons."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import uuid
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from sqlalchemy import update
from sqlalchemy.orm.attributes import set_committed_value

from database import db
from models import (
    CompetitorExternalIdentity,
    Event,
    Heat,
    ShadowContextObservation,
    ShadowHandicapRun,
    ShadowOutcomeRevision,
    User,
)

CONTEXT_SCHEMA_VERSION = "missoula.shadow-context-observation.v1"
CONTEXT_EXPORT_SCHEMA_VERSION = "missoula.shadow-context-audit-export.v1"
MAX_CONTEXT_JSON_BYTES = 4096
MAX_CONTEXT_SOURCE_RECORDS = 32
MAX_CONTEXT_OBSERVATIONS_PER_RUN = 10000
MAX_CONTEXT_EXPORT_PAGE = 500
_CODE_RE = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,79}$")
_NAMESPACED_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,31}:[a-z0-9][a-z0-9_.:-]{1,190}$")
_CAPTURE_ROLES = {
    User.ROLE_ADMIN,
    User.ROLE_JUDGE,
    User.ROLE_SCORER,
    User.ROLE_REGISTRAR,
}


class ShadowContextConflict(ValueError):
    """A context correction was based on an obsolete observation revision."""


@dataclass(frozen=True)
class ContextFactorSpec:
    factor: str
    responsible_actor: str
    stage: str
    subject_types: tuple[str, ...]
    allowed_sources: tuple[str, ...]
    explicit_unknown_allowed: bool = True


@dataclass(frozen=True)
class ContextAuditExport:
    payload: Mapping[str, Any]
    payload_json: str
    sha256: str


FACTOR_MATRIX = MappingProxyType(
    {
        "division": ContextFactorSpec(
            "division",
            "tournament_configuration",
            "event_setup",
            ("event", "entrant"),
            ("imported", "operator_entered", "system_recorded"),
        ),
        "round_heat": ContextFactorSpec(
            "round_heat",
            "scheduler",
            "schedule_issue",
            ("heat_run",),
            ("system_recorded",),
        ),
        "venue": ContextFactorSpec(
            "venue",
            "show_director",
            "tournament_setup",
            ("tournament_day",),
            ("operator_entered",),
        ),
        "lane_stand": ContextFactorSpec(
            "lane_stand",
            "scheduler_or_judge",
            "schedule_issue",
            ("run",),
            ("system_recorded", "operator_entered"),
        ),
        "issued_run_order": ContextFactorSpec(
            "issued_run_order",
            "scheduler",
            "schedule_issue",
            ("field", "heat_run"),
            ("system_recorded",),
        ),
        "material_identity": ContextFactorSpec(
            "material_identity",
            "field_prep",
            "material_preparation",
            ("material", "run"),
            ("scanned", "operator_entered"),
        ),
        "material_quality_moisture": ContextFactorSpec(
            "material_quality_moisture",
            "field_prep",
            "material_preparation",
            ("material",),
            ("measured", "operator_entered"),
        ),
        "weather": ContextFactorSpec(
            "weather",
            "designated_official",
            "event_start",
            ("time_window", "field"),
            ("measured", "operator_entered"),
        ),
        "equipment_used": ContextFactorSpec(
            "equipment_used",
            "athlete_or_judge",
            "result_entry",
            ("run",),
            ("operator_entered",),
        ),
        "rest_fatigue": ContextFactorSpec(
            "rest_fatigue",
            "system_or_judge",
            "result_entry",
            ("entrant_run",),
            ("derived", "operator_entered"),
        ),
        "penalty_nonfinish": ContextFactorSpec(
            "penalty_nonfinish",
            "judge_or_scorer",
            "result_entry",
            ("outcome", "run"),
            ("system_recorded", "operator_entered"),
        ),
    }
)


def record_context_observation(
    run: ShadowHandicapRun,
    *,
    factor: str,
    subject_type: str,
    subject_id: str,
    value_state: str,
    value: Mapping[str, Any] | None,
    source: str,
    actor: User,
    expected_latest_observation_id: str | None = None,
    expected_context_token: str | None = None,
    formula: str | None = None,
    source_record_ids: tuple[str, ...] = (),
) -> ShadowContextObservation:
    """Append one bounded observation or correction after strict validation."""

    if actor.role not in _CAPTURE_ROLES:
        raise ValueError("authorized race-day role is required for context capture")
    if run.lifecycle in {"superseded", "cancelled"}:
        raise ValueError("context cannot be added to a closed shadow run")
    spec = FACTOR_MATRIX.get(factor)
    if spec is None:
        raise ValueError("unknown prospective context factor")
    if subject_type not in spec.subject_types:
        raise ValueError("subject type does not match the context factor")
    _require_namespaced(subject_id, "subject_id")
    if source not in spec.allowed_sources:
        raise ValueError("source is not allowed for the context factor")
    normalized_value = _validate_value(factor, value_state, value)
    normalized_formula, normalized_source_ids = _validate_provenance(
        source,
        formula,
        source_record_ids,
    )
    if expected_context_token is not None and not hmac.compare_digest(
        context_state_token(run),
        expected_context_token,
    ):
        raise ShadowContextConflict(
            "context evidence changed while this task was open; reload and review again"
        )
    latest = _latest_for_subject(run, factor, subject_type, subject_id)
    if latest is not None and expected_context_token is not None:
        expected_latest_observation_id = latest.observation_id
    if latest is not None and _same_observation(
        latest,
        value_state=value_state,
        value=normalized_value,
        source=source,
        formula=normalized_formula,
        source_record_ids=normalized_source_ids,
    ):
        return latest
    if latest is not None and actor.role not in {User.ROLE_ADMIN, User.ROLE_JUDGE}:
        raise ValueError("judge or admin role is required for context correction")
    if latest is not None and (
        expected_latest_observation_id is None
        or not hmac.compare_digest(latest.observation_id, expected_latest_observation_id)
    ):
        raise ShadowContextConflict(
            "context evidence changed while this task was open; reload and review again"
        )
    if latest is None and expected_latest_observation_id is not None:
        raise ShadowContextConflict("context evidence changed before this observation was saved")
    if len(run.context_observations) >= MAX_CONTEXT_OBSERVATIONS_PER_RUN:
        raise ValueError("context observation capacity is exhausted for this run")

    _claim_context_version(run)

    value_json = _canonical_json(normalized_value) if normalized_value is not None else None
    row = ShadowContextObservation(
        observation_id=f"missoula:context-observation:{uuid.uuid4()}",
        schema_version=CONTEXT_SCHEMA_VERSION,
        subject_type=subject_type,
        subject_id=subject_id,
        factor=factor,
        value_state=value_state,
        value_json=value_json,
        source=source,
        actor_id=actor.id,
        corrects_observation_id=latest.id if latest is not None else None,
        formula=normalized_formula,
        source_record_ids_json=(
            _canonical_json(list(normalized_source_ids)) if normalized_source_ids else None
        ),
    )
    run.context_observations.append(row)
    db.session.flush()
    return row


def capture_event_context(
    run: ShadowHandicapRun,
    *,
    event: Event,
    actor: User,
) -> ShadowContextObservation:
    """Record the known configured division without inferring absent context."""

    if event.id != run.event_id:
        raise ValueError("event does not match the shadow run")
    gender = (event.gender or "open").lower()
    division_code = f"{event.event_type}-{gender}"
    return record_context_observation(
        run,
        factor="division",
        subject_type="event",
        subject_id=run.event_occurrence_id,
        value_state="known",
        value={"division_code": division_code},
        source="system_recorded",
        actor=actor,
    )


def capture_preflight_context(
    run: ShadowHandicapRun,
    *,
    event: Event,
    actor: User,
) -> tuple[ShadowContextObservation, ...]:
    """Freeze known system schedule facts at preflight approval."""

    if event.id != run.event_id:
        raise ValueError("event does not match the shadow run")
    snapshot = json.loads(run.input_snapshot_json)
    competitors = snapshot.get("competitors")
    if not isinstance(competitors, list):
        raise ValueError("shadow input snapshot has no ordered field")
    competitor_ids = [row.get("competitor_id") for row in competitors]
    for competitor_id in competitor_ids:
        _require_namespaced(competitor_id, "competitor_id")
    rows = [
        record_context_observation(
            run,
            factor="issued_run_order",
            subject_type="field",
            subject_id=run.field_run_id,
            value_state="known",
            value={"competitor_ids": competitor_ids},
            source="system_recorded",
            actor=actor,
        )
    ]
    identities = {
        row.competitor_uid: row.external_id
        for row in CompetitorExternalIdentity.query.filter(
            CompetitorExternalIdentity.namespace == "strathmark",
            CompetitorExternalIdentity.status == "reviewed",
        ).all()
    }
    heats = Heat.query.filter_by(event_id=event.id).order_by(
        Heat.run_number,
        Heat.heat_number,
        Heat.id,
    )
    for heat in heats:
        heat_run_id = (
            f"missoula:heat-run:{run.event_occurrence_id.rsplit(':', 1)[-1]}:"
            f"{heat.heat_number}:{heat.run_number}"
        )
        rows.append(
            record_context_observation(
                run,
                factor="round_heat",
                subject_type="heat_run",
                subject_id=heat_run_id,
                value_state="known",
                value={
                    "round_number": heat.run_number,
                    "heat_number": heat.heat_number,
                    "run_number": heat.run_number,
                },
                source="system_recorded",
                actor=actor,
            )
        )
        for assignment in heat.assignments:
            external_id = identities.get(assignment.uid)
            if external_id is None:
                continue
            assignment_id = f"{heat_run_id}:{external_id.rsplit(':', 1)[-1]}"
            rows.append(
                record_context_observation(
                    run,
                    factor="lane_stand",
                    subject_type="run",
                    subject_id=assignment_id,
                    value_state="known" if assignment.stand_number is not None else "unknown",
                    value=(
                        {"stand_number": assignment.stand_number}
                        if assignment.stand_number is not None
                        else None
                    ),
                    source="system_recorded",
                    actor=actor,
                )
            )
    return tuple(rows)


def capture_outcome_context(
    run: ShadowHandicapRun,
    *,
    outcome: ShadowOutcomeRevision,
    actor: User,
) -> ShadowContextObservation:
    """Record the structured official finish/nonfinish decision beside the outcome."""

    classification = outcome.classification
    if classification == "valid_finish":
        classification = "none"
    return record_context_observation(
        run,
        factor="penalty_nonfinish",
        subject_type="outcome",
        subject_id=outcome.outcome_revision_id,
        value_state="known",
        value={"classification": classification},
        source="system_recorded",
        actor=actor,
    )


def complete_context_stage(
    run: ShadowHandicapRun,
    *,
    stage: str,
    actor: User,
    subjects: Mapping[str, tuple[str, str]],
) -> tuple[ShadowContextObservation, ...]:
    """Close a task stage by recording explicit unknowns for every supplied scope."""

    expected_factors = {key for key, spec in FACTOR_MATRIX.items() if spec.stage == stage}
    if not expected_factors:
        raise ValueError("unknown context capture stage")
    if set(subjects) != expected_factors:
        raise ValueError("stage completion requires a subject for every stage factor")
    rows = []
    for factor in sorted(expected_factors):
        subject_type, subject_id = subjects[factor]
        source = (
            "system_recorded"
            if "system_recorded" in FACTOR_MATRIX[factor].allowed_sources
            else "operator_entered"
        )
        rows.append(
            record_context_observation(
                run,
                factor=factor,
                subject_type=subject_type,
                subject_id=subject_id,
                value_state="unknown",
                value=None,
                source=source,
                actor=actor,
            )
        )
    return tuple(rows)


def build_context_audit_export(
    run: ShadowHandicapRun,
    *,
    actor: User,
    cursor: int = 0,
    limit: int = MAX_CONTEXT_EXPORT_PAGE,
) -> ContextAuditExport:
    """Build a pseudonymous, versioned export containing the full factor matrix."""

    if actor.role not in {User.ROLE_ADMIN, User.ROLE_JUDGE}:
        raise ValueError("judge or admin role is required for context export")
    _require_namespaced(actor.shadow_actor_id, "actor_id")
    if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0:
        raise ValueError("context export cursor must be a nonnegative integer")
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= MAX_CONTEXT_EXPORT_PAGE
    ):
        raise ValueError("context export limit is outside the allowed range")
    all_observations = sorted(run.context_observations, key=lambda row: row.id)
    page_observations = set(row.id for row in all_observations[cursor : cursor + limit])
    grouped: dict[str, list[ShadowContextObservation]] = {key: [] for key in FACTOR_MATRIX}
    for row in run.context_observations:
        grouped[row.factor].append(row)
    factors = []
    fingerprint_rows = []
    for factor, spec in FACTOR_MATRIX.items():
        observations = sorted(grouped[factor], key=lambda row: row.id)
        complete_history = [_export_observation(row) for row in observations]
        history = [
            item
            for row, item in zip(observations, complete_history, strict=True)
            if row.id in page_observations
        ]
        latest_by_subject: dict[tuple[str, str], Mapping[str, Any]] = {}
        for item in complete_history:
            latest_by_subject[(item["subject_type"], item["subject_id"])] = item
        latest = list(latest_by_subject.values())
        fingerprint_rows.extend(latest)
        factors.append(
            {
                "factor": factor,
                "responsible_actor": spec.responsible_actor,
                "stage": spec.stage,
                "state": "recorded" if latest else "not-recorded",
                "latest": latest,
                "history": history,
            }
        )
    context_fingerprint = _digest(_canonical_json(fingerprint_rows))
    payload = {
        "schema_version": CONTEXT_EXPORT_SCHEMA_VERSION,
        "run_id": run.run_id,
        "run_revision": run.run_revision,
        "event_occurrence_id": run.event_occurrence_id,
        "field_run_id": run.field_run_id,
        "generated_by": actor.shadow_actor_id,
        "numeric_inactive": True,
        "promotion_state": "prospective-only",
        "context_fingerprint": context_fingerprint,
        "calculation_observation_fingerprint": run.observation_fingerprint,
        "factors": factors,
        "page": {
            "cursor": cursor,
            "limit": limit,
            "next_cursor": (cursor + limit if cursor + limit < len(all_observations) else None),
            "total_observations": len(all_observations),
        },
    }
    payload_json = _canonical_json(payload)
    return ContextAuditExport(payload, payload_json, _digest(payload_json))


def context_state_token(run: ShadowHandicapRun) -> str:
    """Return a whole-run compare-and-swap token for operator capture forms."""

    latest: dict[tuple[str, str, str], ShadowContextObservation] = {}
    for row in run.context_observations:
        key = (row.factor, row.subject_type, row.subject_id)
        latest[key] = row
    projection = [
        {
            "factor": key[0],
            "subject_type": key[1],
            "subject_id": key[2],
            "observation_id": row.observation_id,
        }
        for key, row in sorted(latest.items())
    ]
    return _digest(
        _canonical_json(
            {
                "context_version": run.context_version,
                "observations": projection,
                "run_id": run.run_id,
            }
        )
    )


def _claim_context_version(run: ShadowHandicapRun) -> None:
    """Atomically reserve the next append revision in the current transaction."""

    if run.id is None:
        db.session.flush()
    expected_version = run.context_version
    if isinstance(expected_version, bool) or not isinstance(expected_version, int):
        raise ShadowContextConflict(
            "context evidence changed while this task was open; reload and review again"
        )
    result = db.session.execute(
        update(ShadowHandicapRun)
        .where(
            ShadowHandicapRun.id == run.id,
            ShadowHandicapRun.context_version == expected_version,
        )
        .values(context_version=expected_version + 1)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        raise ShadowContextConflict(
            "context evidence changed while this task was open; reload and review again"
        )
    set_committed_value(run, "context_version", expected_version + 1)


def _latest_for_subject(run, factor, subject_type, subject_id):
    candidates = [
        row
        for row in run.context_observations
        if row.factor == factor
        and row.subject_type == subject_type
        and row.subject_id == subject_id
    ]
    return max(candidates, key=lambda row: row.id) if candidates else None


def _same_observation(
    row,
    *,
    value_state,
    value,
    source,
    formula,
    source_record_ids,
) -> bool:
    stored_value = json.loads(row.value_json) if row.value_json is not None else None
    stored_sources = (
        tuple(json.loads(row.source_record_ids_json)) if row.source_record_ids_json else ()
    )
    return (
        row.value_state == value_state
        and stored_value == value
        and row.source == source
        and row.formula == formula
        and stored_sources == source_record_ids
    )


def _validate_value(factor, value_state, value):
    if value_state not in {"known", "unknown"}:
        raise ValueError("value_state must be known or unknown")
    if value_state == "unknown":
        if value is not None:
            raise ValueError("explicit unknown context cannot carry a value")
        return None
    if not isinstance(value, Mapping):
        raise ValueError("known context value must be an object")
    validators = {
        "division": _validate_division,
        "round_heat": _validate_round_heat,
        "venue": _validate_venue,
        "lane_stand": _validate_lane_stand,
        "issued_run_order": _validate_run_order,
        "material_identity": _validate_material_identity,
        "material_quality_moisture": _validate_material_quality,
        "weather": _validate_weather,
        "equipment_used": _validate_equipment,
        "rest_fatigue": _validate_rest_fatigue,
        "penalty_nonfinish": _validate_penalty_nonfinish,
    }
    normalized = validators[factor](dict(value))
    if len(_canonical_json(normalized).encode("utf-8")) > MAX_CONTEXT_JSON_BYTES:
        raise ValueError("context value exceeds the size limit")
    return normalized


def _exact(value, required, optional=()):
    keys = set(value)
    allowed = set(required) | set(optional)
    unknown = keys - allowed
    if unknown:
        raise ValueError(f"unknown field in context value: {sorted(unknown)[0]}")
    missing = set(required) - keys
    if missing:
        raise ValueError(f"missing field in context value: {sorted(missing)[0]}")


def _code(value, field):
    if not isinstance(value, str) or not _CODE_RE.fullmatch(value):
        raise ValueError(f"{field} must be a bounded machine code")
    return value


def _number(value, field, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be numeric")
    normalized = float(value)
    if not math.isfinite(normalized) or not minimum <= normalized <= maximum:
        raise ValueError(f"{field} is outside the allowed range")
    return normalized


def _integer(value, field, minimum=1, maximum=10000):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{field} must be a bounded integer")
    return value


def _validate_division(value):
    _exact(value, {"division_code"})
    return {"division_code": _code(value["division_code"], "division_code")}


def _validate_round_heat(value):
    _exact(value, {"round_number", "heat_number", "run_number"})
    return {key: _integer(value[key], key) for key in sorted(value)}


def _validate_venue(value):
    _exact(value, {"venue_code"})
    _require_namespaced(value["venue_code"], "venue_code")
    return {"venue_code": value["venue_code"]}


def _validate_lane_stand(value):
    _exact(value, {"stand_number"})
    return {"stand_number": _integer(value["stand_number"], "stand_number", 1, 512)}


def _validate_run_order(value):
    _exact(value, {"competitor_ids"})
    competitor_ids = value["competitor_ids"]
    if not isinstance(competitor_ids, list) or not 1 <= len(competitor_ids) <= 512:
        raise ValueError("competitor_ids must contain 1 to 512 stable IDs")
    for item in competitor_ids:
        _require_namespaced(item, "competitor_id")
    if len(set(competitor_ids)) != len(competitor_ids):
        raise ValueError("competitor_ids must be unique")
    return {"competitor_ids": list(competitor_ids)}


def _validate_material_identity(value):
    _exact(value, {"material_id", "batch_id"})
    for key in ("material_id", "batch_id"):
        _require_namespaced(value[key], key)
    return {"batch_id": value["batch_id"], "material_id": value["material_id"]}


def _validate_material_quality(value):
    _exact(value, {"quality_code", "moisture_percent"})
    quality = _code(value["quality_code"], "quality_code")
    if quality not in {"ungraded", "select", "standard", "reject"}:
        raise ValueError("quality_code is not recognized")
    return {
        "moisture_percent": _number(value["moisture_percent"], "moisture_percent", 0, 100),
        "quality_code": quality,
    }


def _validate_weather(value):
    _exact(
        value,
        {"temperature_f", "humidity_percent", "wind_mph", "precipitation_code"},
    )
    precipitation = _code(value["precipitation_code"], "precipitation_code")
    if precipitation not in {"none", "rain", "snow", "mixed"}:
        raise ValueError("precipitation_code is not recognized")
    return {
        "humidity_percent": _number(value["humidity_percent"], "humidity_percent", 0, 100),
        "precipitation_code": precipitation,
        "temperature_f": _number(value["temperature_f"], "temperature_f", -80, 150),
        "wind_mph": _number(value["wind_mph"], "wind_mph", 0, 200),
    }


def _validate_equipment(value):
    _exact(value, {"equipment_code"})
    return {"equipment_code": _code(value["equipment_code"], "equipment_code")}


def _validate_rest_fatigue(value):
    _exact(value, {"rest_minutes", "observation_code"})
    observation = _code(value["observation_code"], "observation_code")
    if observation not in {"rested", "typical", "fatigued"}:
        raise ValueError("observation_code must be a structured non-medical state")
    return {
        "observation_code": observation,
        "rest_minutes": _integer(value["rest_minutes"], "rest_minutes", 0, 10080),
    }


def _validate_penalty_nonfinish(value):
    _exact(value, {"classification"}, {"penalty_code"})
    classification = _code(value["classification"], "classification")
    if classification not in {
        "none",
        "penalty",
        "dns",
        "scratch",
        "dnf",
        "dq",
        "rerun",
        "no_contest",
        "timing_failure",
    }:
        raise ValueError("classification is not recognized")
    result = {"classification": classification}
    if "penalty_code" in value:
        result["penalty_code"] = _code(value["penalty_code"], "penalty_code")
    if classification == "penalty" and "penalty_code" not in result:
        raise ValueError("penalty_code is required for a penalty")
    return result


def _validate_provenance(source, formula, source_record_ids):
    if (
        not isinstance(source_record_ids, tuple)
        or len(source_record_ids) > MAX_CONTEXT_SOURCE_RECORDS
    ):
        raise ValueError("source_record_ids must be a bounded tuple")
    for item in source_record_ids:
        _require_namespaced(item, "source_record_id")
    if source == "derived":
        if formula != "elapsed_minutes_v1" or not source_record_ids:
            raise ValueError("derived context requires an approved formula and source records")
        return formula, source_record_ids
    if formula is not None or source_record_ids:
        raise ValueError("formula and source records are only allowed for derived context")
    return None, ()


def _export_observation(row):
    actor = db.session.get(User, row.actor_id)
    return {
        "observation_id": row.observation_id,
        "schema_version": row.schema_version,
        "subject_type": row.subject_type,
        "subject_id": row.subject_id,
        "value_state": row.value_state,
        "value": json.loads(row.value_json) if row.value_json is not None else None,
        "source": row.source,
        "actor_id": actor.shadow_actor_id if actor is not None else "missoula:operator:unknown",
        "captured_at_utc": row.captured_at.isoformat() + "Z",
        "corrects_observation_id": row.corrects.observation_id if row.corrects else None,
        "formula": row.formula,
        "source_record_ids": (
            json.loads(row.source_record_ids_json) if row.source_record_ids_json else []
        ),
    }


def _require_namespaced(value, field):
    if not isinstance(value, str) or not _NAMESPACED_RE.fullmatch(value):
        raise ValueError(f"{field} must be a bounded namespaced ID")


def _canonical_json(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


__all__ = [
    "CONTEXT_EXPORT_SCHEMA_VERSION",
    "CONTEXT_SCHEMA_VERSION",
    "FACTOR_MATRIX",
    "MAX_CONTEXT_EXPORT_PAGE",
    "MAX_CONTEXT_OBSERVATIONS_PER_RUN",
    "ContextAuditExport",
    "ContextFactorSpec",
    "ShadowContextConflict",
    "build_context_audit_export",
    "capture_event_context",
    "capture_outcome_context",
    "capture_preflight_context",
    "complete_context_stage",
    "context_state_token",
    "record_context_observation",
]
