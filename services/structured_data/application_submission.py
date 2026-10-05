from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from services.realtime_qa.closed_world import ClosedWorldInputError

SHA256 = re.compile(r"^sha256:[a-f0-9]{64}$")
DIRECT_IDENTIFIER = re.compile(
    r"(?:^|_)(?:name|full_name|display_name|姓名|id_card|identity_card|身份证|"
    r"phone|mobile|手机号|email|邮箱|address|住址)(?:$|_)",
    re.IGNORECASE,
)
SUBMISSION_FIELDS = {
    "case_id_hash",
    "service_item_id",
    "submitted_material_code",
    "submit_status",
    "snapshot_version",
    "snapshot_complete",
    "submitted_at_version",
    "source_locator",
    "dataset_id",
    "source_sha256",
    "pii_scope",
}


class ApplicationSubmissionRecord(BaseModel):
    """One material submission fact without direct identity attributes."""

    model_config = ConfigDict(extra="forbid")

    submitted_material_code: str = Field(min_length=1, max_length=256)
    submit_status: Literal["SUBMITTED", "ACCEPTED", "REJECTED", "WITHDRAWN", "INVALID"]
    source_locator: str = Field(min_length=1, max_length=2048)


class ApplicationSubmissionSnapshot(BaseModel):
    """Auditable per-case snapshot required before closed-world subtraction."""

    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(min_length=3, max_length=160)
    case_id_hash: str = Field(pattern=SHA256.pattern)
    service_item_id: str = Field(min_length=1, max_length=256)
    snapshot_version: str = Field(min_length=1, max_length=256)
    snapshot_complete: bool
    submitted_at_version: str = Field(min_length=1, max_length=256)
    dataset_id: str = Field(pattern=r"^DS-[A-Z0-9-]{8,80}$")
    source_sha256: str = Field(pattern=SHA256.pattern)
    snapshot_sha256: str = Field(pattern=SHA256.pattern)
    source_refs: list[str] = Field(min_length=1, max_length=32)
    pii_scope: dict[str, Any]
    row_count: int = Field(ge=0)
    records: list[ApplicationSubmissionRecord]
    dataset_type: Literal["PRODUCTION_EVIDENCE", "TEST_ONLY"]
    production_evidence: bool

    @model_validator(mode="after")
    def validate_snapshot(self) -> ApplicationSubmissionSnapshot:
        if self.production_evidence != (self.dataset_type == "PRODUCTION_EVIDENCE"):
            raise ClosedWorldInputError(
                "SOURCE_NOT_BOUND", "dataset_type and production_evidence disagree"
            )
        if self.row_count != len(self.records):
            raise ClosedWorldInputError(
                "INPUT_INCOMPLETE", "submission row count differs from the snapshot receipt"
            )
        if len(self.source_refs) != len(set(self.source_refs)) or any(
            not str(item).strip() for item in self.source_refs
        ):
            raise ClosedWorldInputError(
                "SOURCE_NOT_BOUND", "submission source_refs are missing or duplicated"
            )
        _validate_pii_scope(self.pii_scope)
        return self

    def to_closed_world_contract(self) -> dict[str, Any]:
        if not self.snapshot_complete:
            raise ClosedWorldInputError(
                "INPUT_INCOMPLETE",
                "submitted-material snapshot is not complete for this case and version",
            )
        return {
            "predicate": "SubmittedMaterial",
            "completeness": "COMPLETE_FOR_CASE_AND_SNAPSHOT",
            "dataset_type": self.dataset_type,
            "production_evidence": self.production_evidence,
            "dataset_id": self.dataset_id,
            "source_sha256": self.source_sha256,
            "snapshot_sha256": self.snapshot_sha256,
            "snapshot_version": self.snapshot_version,
            "submitted_at_version": self.submitted_at_version,
            "row_count": self.row_count,
            "source_refs": list(self.source_refs),
            "key_fields": ["case_id_hash", "submitted_material_code"],
            "field_bindings": {
                "case_id": "case_id_hash",
                "service_item_id": "service_item_id",
                "material_code": "submitted_material_code",
                "submit_status": "submit_status",
                "snapshot_version": "snapshot_version",
                "source_locator": "source_locator",
            },
            "status_filter": ["SUBMITTED", "ACCEPTED"],
            "pii_scope": dict(self.pii_scope),
        }

    def to_closed_world_rows(self) -> list[dict[str, Any]]:
        return [
            {
                "case_id_hash": self.case_id_hash,
                "service_item_id": self.service_item_id,
                "submitted_material_code": record.submitted_material_code,
                "submit_status": record.submit_status,
                "snapshot_version": self.snapshot_version,
                "source_locator": record.source_locator,
            }
            for record in self.records
        ]


class ApplicationSubmissionValidation(BaseModel):
    status: Literal["COMPLETE", "INPUT_INCOMPLETE"]
    reason_code: Literal["READY", "INPUT_INCOMPLETE"]
    case_id_hash: str
    service_item_id: str
    snapshot_version: str
    row_count: int = Field(ge=0)
    dataset_type: Literal["PRODUCTION_EVIDENCE", "TEST_ONLY"]
    production_evidence: bool
    missing_facts_allowed: bool
    reason: str | None = None


def validate_application_submission_snapshot(
    payload: dict[str, Any],
    *,
    expected_project_id: str | None = None,
) -> tuple[ApplicationSubmissionSnapshot, ApplicationSubmissionValidation]:
    """Validate one per-case snapshot without interpreting absence prematurely."""

    try:
        snapshot = ApplicationSubmissionSnapshot.model_validate(payload)
    except ValidationError as exc:
        message = str(exc)
        if "submission row count differs" in message:
            raise ClosedWorldInputError("INPUT_INCOMPLETE", message) from exc
        if any(
            marker in message
            for marker in (
                "dataset_type and production_evidence disagree",
                "submission source_refs are missing or duplicated",
                "PII scope",
                "direct identity fields are forbidden",
            )
        ):
            raise ClosedWorldInputError("SOURCE_NOT_BOUND", message) from exc
        raise
    if expected_project_id and snapshot.project_id != expected_project_id:
        raise ClosedWorldInputError(
            "SOURCE_NOT_BOUND", "submission snapshot belongs to a different project"
        )
    if not snapshot.snapshot_complete:
        return snapshot, ApplicationSubmissionValidation(
            status="INPUT_INCOMPLETE",
            reason_code="INPUT_INCOMPLETE",
            case_id_hash=snapshot.case_id_hash,
            service_item_id=snapshot.service_item_id,
            snapshot_version=snapshot.snapshot_version,
            row_count=snapshot.row_count,
            dataset_type=snapshot.dataset_type,
            production_evidence=snapshot.production_evidence,
            missing_facts_allowed=False,
            reason="submitted-material snapshot is incomplete; absence remains UNKNOWN",
        )
    return snapshot, ApplicationSubmissionValidation(
        status="COMPLETE",
        reason_code="READY",
        case_id_hash=snapshot.case_id_hash,
        service_item_id=snapshot.service_item_id,
        snapshot_version=snapshot.snapshot_version,
        row_count=snapshot.row_count,
        dataset_type=snapshot.dataset_type,
        production_evidence=snapshot.production_evidence,
        missing_facts_allowed=True,
    )


def _validate_pii_scope(scope: dict[str, Any]) -> None:
    allowed_fields = scope.get("allowed_fields")
    if (
        scope.get("mode") != "MINIMUM_NECESSARY"
        or scope.get("direct_identifiers_included") is not False
        or not isinstance(allowed_fields, list)
        or not allowed_fields
    ):
        raise ClosedWorldInputError(
            "SOURCE_NOT_BOUND", "PII scope lacks minimum-necessary/no-direct-identifier proof"
        )
    fields = {str(item).strip() for item in allowed_fields}
    if "case_id_hash" not in fields or not fields.issubset(SUBMISSION_FIELDS):
        raise ClosedWorldInputError(
            "SOURCE_NOT_BOUND", "PII scope contains unapproved fields"
        )
    if any(DIRECT_IDENTIFIER.search(field) for field in fields):
        raise ClosedWorldInputError(
            "SOURCE_NOT_BOUND", "direct identity fields are forbidden"
        )
