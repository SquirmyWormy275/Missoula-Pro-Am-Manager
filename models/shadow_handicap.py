"""Persistent, scoring-inert state for STRATHMARK shadow operations.

Missoula owns workflow decisions and operational outcomes.  STRATHMARK owns
numeric predictions and settlement revisions.  These tables retain only the
immutable boundary evidence needed to prepare, recover, review, issue, and
settle a whole-field shadow sheet; none of them participates in championship
scoring.
"""

import re
import uuid

import sqlalchemy as sa
from sqlalchemy.orm import validates

from database import db
from services.time_utils import utc_now_naive

from ._types import BIG_ID

_NAMESPACED_ID = re.compile(r"^[a-z][a-z0-9-]{1,31}:[a-z][a-z0-9-]{1,31}:[A-Za-z0-9._:-]{1,160}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _require_namespaced(value: str, field: str) -> str:
    if not isinstance(value, str) or not _NAMESPACED_ID.fullmatch(value):
        raise ValueError(f"{field} must be a bounded namespaced identifier")
    return value


def _require_sha256(value: str, field: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return value


class CompetitorExternalIdentity(db.Model):
    """Reviewed stable mapping from the Missoula identity spine to a consumer ID."""

    __tablename__ = "competitor_external_identities"
    __table_args__ = (
        db.UniqueConstraint(
            "competitor_uid",
            "namespace",
            name="uq_competitor_external_identity_owner_namespace",
        ),
        db.UniqueConstraint(
            "namespace",
            "external_id",
            name="uq_competitor_external_identity_namespace_id",
        ),
        db.CheckConstraint(
            "status IN ('reviewed', 'conflict', 'retired')",
            name="ck_competitor_external_identity_status",
        ),
        db.Index("ix_competitor_external_identity_external", "namespace", "external_id"),
    )

    id = db.Column(BIG_ID, primary_key=True, autoincrement=True)
    competitor_uid = db.Column(
        BIG_ID,
        db.ForeignKey("competitors.uid", ondelete="CASCADE"),
        nullable=False,
    )
    namespace = db.Column(db.String(32), nullable=False)
    external_id = db.Column(db.String(224), nullable=False)
    status = db.Column(db.String(16), nullable=False)
    reviewed_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    reviewed_at = db.Column(
        db.DateTime,
        nullable=False,
        default=utc_now_naive,
        server_default=sa.func.now(),
    )
    created_at = db.Column(
        db.DateTime,
        nullable=False,
        default=utc_now_naive,
        server_default=sa.func.now(),
    )

    competitor = db.relationship("Competitor")
    reviewed_by = db.relationship("User")

    @validates("external_id")
    def _validate_external_id(self, _key, value):
        return _require_namespaced(value, "external_id")


class ShadowHandicapRun(db.Model):
    """One immutable-input, whole-field shadow workflow attempt."""

    __tablename__ = "shadow_handicap_runs"
    __table_args__ = (
        db.UniqueConstraint("consumer_id", "request_id", name="uq_shadow_run_consumer_request"),
        db.UniqueConstraint("run_id", name="uq_shadow_run_id"),
        db.CheckConstraint("authority = 'shadow'", name="ck_shadow_run_authority"),
        db.CheckConstraint(
            "lifecycle IN ('draft', 'prepared', 'preflight-approved', 'calculated', "
            "'reviewed', 'shadow-issued', 'outcomes-complete', 'superseded', 'cancelled')",
            name="ck_shadow_run_lifecycle",
        ),
        db.CheckConstraint("lifecycle_version >= 1", name="ck_shadow_run_lifecycle_version"),
        db.CheckConstraint("context_version >= 0", name="ck_shadow_run_context_version"),
        db.Index("ix_shadow_run_event_created", "event_id", "created_at"),
        db.Index("ix_shadow_run_field_revision", "field_run_id", "run_revision"),
    )

    id = db.Column(BIG_ID, primary_key=True, autoincrement=True)
    run_id = db.Column(db.String(224), nullable=False)
    request_id = db.Column(db.String(224), nullable=False)
    consumer_id = db.Column(db.String(224), nullable=False)
    tournament_id = db.Column(
        db.Integer,
        db.ForeignKey("tournaments.id", ondelete="CASCADE"),
        nullable=False,
    )
    event_id = db.Column(
        db.Integer,
        db.ForeignKey("events.id", ondelete="CASCADE"),
        nullable=False,
    )
    event_occurrence_id = db.Column(db.String(224), nullable=False)
    field_run_id = db.Column(db.String(224), nullable=False)
    run_revision = db.Column(db.String(224), nullable=False)
    supersedes_run_id = db.Column(
        BIG_ID,
        db.ForeignKey("shadow_handicap_runs.id"),
        nullable=True,
    )
    authority = db.Column(
        db.String(16),
        nullable=False,
        default="shadow",
        server_default=sa.text("'shadow'"),
    )
    lifecycle = db.Column(
        db.String(32),
        nullable=False,
        default="draft",
        server_default=sa.text("'draft'"),
    )
    lifecycle_version = db.Column(
        db.Integer,
        nullable=False,
        default=1,
        server_default=sa.text("1"),
    )
    context_version = db.Column(
        db.Integer,
        nullable=False,
        default=0,
        server_default=sa.text("0"),
    )
    prediction_as_of = db.Column(db.Date, nullable=False)
    roster_fingerprint = db.Column(db.CHAR(64), nullable=False)
    schedule_fingerprint = db.Column(db.CHAR(64), nullable=False)
    wood_fingerprint = db.Column(db.CHAR(64), nullable=False)
    active_input_fingerprint = db.Column(db.CHAR(64), nullable=False)
    observation_schema_version = db.Column(db.String(80), nullable=False)
    observation_fingerprint = db.Column(db.CHAR(64), nullable=False)
    input_snapshot_json = db.Column(db.Text, nullable=False)
    input_snapshot_sha256 = db.Column(db.CHAR(64), nullable=False)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    reviewed_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    reviewed_at = db.Column(db.DateTime, nullable=True)
    issued_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    issued_at = db.Column(db.DateTime, nullable=True)
    supersession_reason_code = db.Column(db.String(64), nullable=True)
    created_at = db.Column(
        db.DateTime,
        nullable=False,
        default=utc_now_naive,
        server_default=sa.func.now(),
    )
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=utc_now_naive,
        onupdate=utc_now_naive,
        server_default=sa.func.now(),
    )

    supersedes = db.relationship("ShadowHandicapRun", remote_side=[id])
    receipts = db.relationship(
        "ShadowReceiptRevision",
        back_populates="run",
        order_by="ShadowReceiptRevision.revision",
        cascade="all, delete-orphan",
    )
    transitions = db.relationship(
        "ShadowLifecycleTransition",
        back_populates="run",
        order_by="ShadowLifecycleTransition.run_version",
        cascade="all, delete-orphan",
    )
    context_observations = db.relationship(
        "ShadowContextObservation",
        back_populates="run",
        order_by="ShadowContextObservation.id",
        cascade="all, delete-orphan",
    )
    outcome_revisions = db.relationship(
        "ShadowOutcomeRevision",
        back_populates="run",
        order_by="ShadowOutcomeRevision.id",
        cascade="all, delete-orphan",
    )
    settlement_outbox = db.relationship(
        "ShadowSettlementOutbox",
        back_populates="run",
        order_by="ShadowSettlementOutbox.id",
        cascade="all, delete-orphan",
    )
    field_reviews = db.relationship(
        "ShadowFieldReview",
        back_populates="run",
        order_by="ShadowFieldReview.id",
        cascade="all, delete-orphan",
    )
    issue_artifacts = db.relationship(
        "ShadowIssueArtifact",
        back_populates="run",
        order_by="ShadowIssueArtifact.id",
        cascade="all, delete-orphan",
    )

    __mapper_args__ = {
        "version_id_col": lifecycle_version,
        "version_id_generator": False,
    }

    @validates(
        "run_id",
        "request_id",
        "consumer_id",
        "event_occurrence_id",
        "field_run_id",
        "run_revision",
    )
    def _validate_identity(self, key, value):
        return _require_namespaced(value, key)

    @validates(
        "roster_fingerprint",
        "schedule_fingerprint",
        "wood_fingerprint",
        "active_input_fingerprint",
        "observation_fingerprint",
        "input_snapshot_sha256",
    )
    def _validate_fingerprint(self, key, value):
        return _require_sha256(value, key)


class ShadowLifecycleTransition(db.Model):
    """Append-only lifecycle decision bound to an optimistic run version."""

    __tablename__ = "shadow_lifecycle_transitions"
    __table_args__ = (
        db.UniqueConstraint("run_id", "run_version", name="uq_shadow_transition_run_version"),
        db.CheckConstraint("run_version >= 2", name="ck_shadow_transition_run_version"),
    )

    id = db.Column(BIG_ID, primary_key=True, autoincrement=True)
    run_id = db.Column(
        BIG_ID,
        db.ForeignKey("shadow_handicap_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    from_lifecycle = db.Column(db.String(32), nullable=False)
    to_lifecycle = db.Column(db.String(32), nullable=False)
    run_version = db.Column(db.Integer, nullable=False)
    actor_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    reason_code = db.Column(db.String(64), nullable=False)
    created_at = db.Column(
        db.DateTime,
        nullable=False,
        default=utc_now_naive,
        server_default=sa.func.now(),
    )

    run = db.relationship("ShadowHandicapRun", back_populates="transitions")


class ShadowReceiptRevision(db.Model):
    """Immutable STRATHMARK receipt core retained byte-for-byte for replay."""

    __tablename__ = "shadow_receipt_revisions"
    __table_args__ = (
        db.UniqueConstraint("run_id", "revision", name="uq_shadow_receipt_run_revision"),
        db.UniqueConstraint("ledger_request_id", name="uq_shadow_receipt_ledger_request"),
        db.CheckConstraint("revision >= 1", name="ck_shadow_receipt_revision"),
        db.CheckConstraint("prediction_count >= 0", name="ck_shadow_receipt_prediction_count"),
    )

    id = db.Column(BIG_ID, primary_key=True, autoincrement=True)
    run_id = db.Column(
        BIG_ID,
        db.ForeignKey("shadow_handicap_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    revision = db.Column(db.Integer, nullable=False)
    schema_version = db.Column(db.String(80), nullable=False)
    core_json = db.Column(db.Text, nullable=False)
    core_sha256 = db.Column(db.CHAR(64), nullable=False)
    prediction_count = db.Column(db.Integer, nullable=False)
    ledger_request_id = db.Column(db.String(224), nullable=False)
    received_at = db.Column(
        db.DateTime,
        nullable=False,
        default=utc_now_naive,
        server_default=sa.func.now(),
    )

    run = db.relationship("ShadowHandicapRun", back_populates="receipts")

    @validates("core_sha256")
    def _validate_core_sha256(self, _key, value):
        return _require_sha256(value, "core_sha256")

    @validates("ledger_request_id")
    def _validate_ledger_request_id(self, _key, value):
        try:
            parsed = uuid.UUID(str(value))
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("ledger_request_id must be a canonical UUID") from exc
        if str(parsed) != value:
            raise ValueError("ledger_request_id must be a canonical UUID")
        return value


class ShadowContextObservation(db.Model):
    """Append-only prospective factor observation; numerically inactive in V2."""

    __tablename__ = "shadow_context_observations"
    __table_args__ = (
        db.UniqueConstraint("observation_id", name="uq_shadow_context_observation_id"),
        db.CheckConstraint(
            "value_state IN ('known', 'unknown')",
            name="ck_shadow_context_value_state",
        ),
        db.CheckConstraint(
            "(value_state = 'unknown' AND value_json IS NULL) OR "
            "(value_state = 'known' AND value_json IS NOT NULL)",
            name="ck_shadow_context_value_presence",
        ),
        db.CheckConstraint(
            "source IN ('imported', 'operator_entered', 'system_recorded', "
            "'measured', 'scanned', 'derived')",
            name="ck_shadow_context_source",
        ),
        db.Index("ix_shadow_context_run_factor", "run_id", "factor"),
    )

    id = db.Column(BIG_ID, primary_key=True, autoincrement=True)
    observation_id = db.Column(db.String(224), nullable=False)
    run_id = db.Column(
        BIG_ID,
        db.ForeignKey("shadow_handicap_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    schema_version = db.Column(db.String(80), nullable=False)
    subject_type = db.Column(db.String(40), nullable=False)
    subject_id = db.Column(db.String(224), nullable=False)
    factor = db.Column(db.String(64), nullable=False)
    value_state = db.Column(db.String(16), nullable=False)
    value_json = db.Column(db.Text, nullable=True)
    source = db.Column(db.String(32), nullable=False)
    actor_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    captured_at = db.Column(
        db.DateTime,
        nullable=False,
        default=utc_now_naive,
        server_default=sa.func.now(),
    )
    corrects_observation_id = db.Column(
        BIG_ID,
        db.ForeignKey("shadow_context_observations.id"),
        nullable=True,
    )
    formula = db.Column(db.String(200), nullable=True)
    source_record_ids_json = db.Column(db.Text, nullable=True)

    run = db.relationship("ShadowHandicapRun", back_populates="context_observations")
    corrects = db.relationship("ShadowContextObservation", remote_side=[id])

    @validates("observation_id", "subject_id")
    def _validate_namespaced_ids(self, key, value):
        return _require_namespaced(value, key)


class ShadowOutcomeRevision(db.Model):
    """Missoula-owned append-only operational outcome revision."""

    __tablename__ = "shadow_outcome_revisions"
    __table_args__ = (
        db.UniqueConstraint("outcome_revision_id", name="uq_shadow_outcome_revision_id"),
        db.UniqueConstraint(
            "run_id",
            "event_result_id",
            "revision",
            name="uq_shadow_outcome_result_revision",
        ),
        db.CheckConstraint("revision >= 1", name="ck_shadow_outcome_revision"),
        db.CheckConstraint(
            "classification IN ('valid_finish', 'dns', 'scratch', 'dnf', 'dq', "
            "'penalty', 'rerun', 'no_contest', 'timing_failure')",
            name="ck_shadow_outcome_classification",
        ),
        db.CheckConstraint(
            "raw_elapsed_seconds IS NULL OR raw_elapsed_seconds > 0",
            name="ck_shadow_outcome_raw_positive",
        ),
        db.Index("ix_shadow_outcome_run_result", "run_id", "event_result_id"),
    )

    id = db.Column(BIG_ID, primary_key=True, autoincrement=True)
    outcome_revision_id = db.Column(db.String(224), nullable=False)
    run_id = db.Column(
        BIG_ID,
        db.ForeignKey("shadow_handicap_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    event_result_id = db.Column(
        db.Integer,
        db.ForeignKey("event_results.id", ondelete="CASCADE"),
        nullable=False,
    )
    revision = db.Column(db.Integer, nullable=False)
    supersedes_outcome_revision_id = db.Column(
        BIG_ID,
        db.ForeignKey("shadow_outcome_revisions.id"),
        nullable=True,
    )
    classification = db.Column(db.String(32), nullable=False)
    raw_elapsed_seconds = db.Column(db.Float, nullable=True)
    official_value = db.Column(db.Float, nullable=True)
    penalty_applied = db.Column(
        db.Boolean,
        nullable=False,
        default=False,
        server_default=sa.text("false"),
    )
    source = db.Column(db.String(32), nullable=False)
    actor_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    reason_code = db.Column(db.String(64), nullable=False)
    created_at = db.Column(
        db.DateTime,
        nullable=False,
        default=utc_now_naive,
        server_default=sa.func.now(),
    )

    run = db.relationship("ShadowHandicapRun", back_populates="outcome_revisions")
    event_result = db.relationship("EventResult")
    supersedes = db.relationship(
        "ShadowOutcomeRevision",
        remote_side=[id],
        foreign_keys=[supersedes_outcome_revision_id],
    )

    @validates("outcome_revision_id")
    def _validate_outcome_revision_id(self, _key, value):
        return _require_namespaced(value, "outcome_revision_id")


class ShadowSettlementOutbox(db.Model):
    """Durable local delivery intent written with an outcome revision."""

    __tablename__ = "shadow_settlement_outbox"
    __table_args__ = (
        db.UniqueConstraint("outbox_id", name="uq_shadow_settlement_outbox_id"),
        db.UniqueConstraint(
            "outcome_revision_id",
            name="uq_shadow_settlement_outbox_outcome_revision",
        ),
        db.CheckConstraint("action IN ('settle', 'void')", name="ck_shadow_outbox_action"),
        db.CheckConstraint(
            "delivery_status IN ('pending', 'recorded', 'retryable-failed')",
            name="ck_shadow_outbox_delivery_status",
        ),
        db.CheckConstraint("attempt_count >= 0", name="ck_shadow_outbox_attempt_count"),
        db.Index("ix_shadow_outbox_status_next", "delivery_status", "next_attempt_at"),
    )

    id = db.Column(BIG_ID, primary_key=True, autoincrement=True)
    outbox_id = db.Column(db.String(224), nullable=False)
    run_id = db.Column(
        BIG_ID,
        db.ForeignKey("shadow_handicap_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    outcome_revision_id = db.Column(
        db.String(224),
        db.ForeignKey("shadow_outcome_revisions.outcome_revision_id"),
        nullable=False,
    )
    schema_version = db.Column(db.String(80), nullable=False)
    action = db.Column(db.String(16), nullable=False)
    payload_json = db.Column(db.Text, nullable=False)
    payload_sha256 = db.Column(db.CHAR(64), nullable=False)
    actor_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    delivery_actor_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id"),
        nullable=False,
    )
    delivery_status = db.Column(
        db.String(24),
        nullable=False,
        default="pending",
        server_default=sa.text("'pending'"),
    )
    attempt_count = db.Column(db.Integer, nullable=False, default=0, server_default=sa.text("0"))
    next_attempt_at = db.Column(db.DateTime, nullable=True)
    last_attempt_at = db.Column(db.DateTime, nullable=True)
    delivered_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(
        db.DateTime,
        nullable=False,
        default=utc_now_naive,
        server_default=sa.func.now(),
    )

    run = db.relationship("ShadowHandicapRun", back_populates="settlement_outbox")
    actor = db.relationship("User", foreign_keys=[actor_id])
    delivery_actor = db.relationship("User", foreign_keys=[delivery_actor_id])

    @validates("outbox_id", "outcome_revision_id")
    def _validate_namespaced_ids(self, key, value):
        return _require_namespaced(value, key)

    @validates("payload_sha256")
    def _validate_payload_sha256(self, _key, value):
        return _require_sha256(value, "payload_sha256")


class ShadowFieldReview(db.Model):
    """Immutable proof that an operator reviewed every receipt prediction."""

    __tablename__ = "shadow_field_reviews"
    __table_args__ = (
        db.UniqueConstraint("review_id", name="uq_shadow_field_review_id"),
        db.UniqueConstraint("run_id", name="uq_shadow_field_review_run"),
        db.CheckConstraint("prediction_count > 0", name="ck_shadow_field_review_count"),
    )

    id = db.Column(BIG_ID, primary_key=True, autoincrement=True)
    review_id = db.Column(db.String(224), nullable=False)
    run_id = db.Column(
        BIG_ID,
        db.ForeignKey("shadow_handicap_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    schema_version = db.Column(db.String(80), nullable=False)
    receipt_core_sha256 = db.Column(db.CHAR(64), nullable=False)
    decision_json = db.Column(db.Text, nullable=False)
    decision_sha256 = db.Column(db.CHAR(64), nullable=False)
    prediction_count = db.Column(db.Integer, nullable=False)
    actor_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(
        db.DateTime,
        nullable=False,
        default=utc_now_naive,
        server_default=sa.func.now(),
    )

    run = db.relationship("ShadowHandicapRun", back_populates="field_reviews")
    actor = db.relationship("User")

    @validates("review_id")
    def _validate_review_id(self, _key, value):
        return _require_namespaced(value, "review_id")

    @validates("receipt_core_sha256", "decision_sha256")
    def _validate_review_sha256(self, key, value):
        return _require_sha256(value, key)


class ShadowIssueArtifact(db.Model):
    """Immutable, checksummed, non-importable whole-field operator export."""

    __tablename__ = "shadow_issue_artifacts"
    __table_args__ = (
        db.UniqueConstraint("issue_id", name="uq_shadow_issue_artifact_id"),
        db.UniqueConstraint("run_id", name="uq_shadow_issue_artifact_run"),
        db.CheckConstraint("prediction_count > 0", name="ck_shadow_issue_artifact_count"),
    )

    id = db.Column(BIG_ID, primary_key=True, autoincrement=True)
    issue_id = db.Column(db.String(224), nullable=False)
    run_id = db.Column(
        BIG_ID,
        db.ForeignKey("shadow_handicap_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    schema_version = db.Column(db.String(80), nullable=False)
    receipt_core_sha256 = db.Column(db.CHAR(64), nullable=False)
    review_decision_sha256 = db.Column(db.CHAR(64), nullable=False)
    export_json = db.Column(db.Text, nullable=False)
    export_sha256 = db.Column(db.CHAR(64), nullable=False)
    prediction_count = db.Column(db.Integer, nullable=False)
    actor_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(
        db.DateTime,
        nullable=False,
        default=utc_now_naive,
        server_default=sa.func.now(),
    )

    run = db.relationship("ShadowHandicapRun", back_populates="issue_artifacts")
    actor = db.relationship("User")

    @validates("issue_id")
    def _validate_issue_id(self, _key, value):
        return _require_namespaced(value, "issue_id")

    @validates("receipt_core_sha256", "review_decision_sha256", "export_sha256")
    def _validate_issue_sha256(self, key, value):
        return _require_sha256(value, key)


_APPEND_ONLY_MODELS = (
    ShadowLifecycleTransition,
    ShadowReceiptRevision,
    ShadowContextObservation,
    ShadowOutcomeRevision,
    ShadowFieldReview,
    ShadowIssueArtifact,
)


def _reject_append_only_change(_mapper, _connection, target):
    raise ValueError(f"{type(target).__name__} is append-only")


for _append_only_model in _APPEND_ONLY_MODELS:
    sa.event.listen(_append_only_model, "before_update", _reject_append_only_change)
    sa.event.listen(_append_only_model, "before_delete", _reject_append_only_change)

    # Production databases receive equivalent triggers from Alembic.  The
    # create_all test/bootstrap path must preserve the same direct-SQL guard;
    # ORM listeners alone cannot intercept an UPDATE or DELETE issued as text.
    for _operation in ("UPDATE", "DELETE"):
        _table_name = _append_only_model.__tablename__
        _trigger_name = f"trg_{_table_name}_append_only_{_operation.lower()}"
        sa.event.listen(
            _append_only_model.__table__,
            "after_create",
            sa.DDL(
                f"""
                CREATE TRIGGER {_trigger_name}
                BEFORE {_operation} ON {_table_name}
                BEGIN
                    SELECT RAISE(ABORT, 'shadow evidence is append-only');
                END
                """
            ).execute_if(dialect="sqlite"),
        )
