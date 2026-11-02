"""Explicit local summary DTOs and fixed errors, never raw capture models.

Origins and display paths are local routing information, not model-safe data.
Display paths omit query strings, userinfo and fragments; path identifiers are
not anonymized. Bodies, headers and retained HAR entries have no DTO fields.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import JSONResponse

from app.privacy import FailurePattern, MAX_APPROVED_LABELS
from app.engine.replace import ObjectKey, ArrayItems


class _DTO(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", hide_input_in_errors=True)


class TransactionSummary(_DTO):
    entry_id: int
    method: str
    status: int
    origin: str
    display_path: str


class OriginGroup(_DTO):
    origin: str
    entry_ids: list[int]


class SkippedEntrySummary(_DTO):
    entry_id: int
    reason: Literal["unsupported MIME type", "unsupported URL scheme", "unsupported content encoding"]


class SelectionSummary(_DTO):
    entry_ids: list[int]
    count: int
    origin: str | None


class CaptureSummary(_DTO):
    capture_id: str
    entry_count: int
    supported_count: int
    skipped_count: int
    transactions: list[TransactionSummary]
    origin_groups: list[OriginGroup]
    skipped_entries: list[SkippedEntrySummary]
    selection: SelectionSummary


class SelectionRequest(_DTO):
    entry_ids: list[int] = Field(min_length=1, max_length=10)


class SchemaLabelChoices(_DTO):
    labels: list[str]


class PreviewRequest(_DTO):
    failure_pattern: FailurePattern
    approved_labels: list[str] = Field(default_factory=list, max_length=MAX_APPROVED_LABELS)


class PreviewMetadata(_DTO):
    examples_truncated: bool
    examples_omitted: int
    max_examples_per_node: int
    max_examples_total: int


class ModelPreview(_DTO):
    preview_id: str
    preview_json: str
    byte_count: int
    sha256: str
    metadata: PreviewMetadata


class PlanRequest(_DTO):
    preview_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    approved: bool = False


class IncompleteDiagnostics(_DTO):
    status: Literal["incomplete"]
    reason: Literal["max_output_tokens", "content_filter", "unknown"]
    max_output_tokens: int
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    reasoning_tokens: int | None = None


class PlanningDiagnostics(_DTO):
    attempts: int = 0
    request_attempted: bool = False
    latency_ms: int | None = None
    http_status: int | None = None
    incomplete: IncompleteDiagnostics | None = None


class ReviewReplacement(_DTO):
    ref: str
    kind: Literal["id", "email", "name", "text"]


class ReviewRelationship(_DTO):
    refs: list[str]
    reason: str


class ReviewLocation(_DTO):
    ref: str
    entry_id: int
    target: Literal["response_json", "request_json", "query"]
    path: list[ObjectKey | ArrayItems]


class ResolvedReplacement(_DTO):
    entry_ids: list[int]
    target: Literal["response_json", "request_json", "query"]
    path: list[ObjectKey | ArrayItems]
    kind: Literal["id", "email", "name", "text"]


class ReviewCheck(_DTO):
    type: FailurePattern
    refs: list[str]
    note: str
    resolution: Literal["resolved", "definition_reference_roles_required", "invalid_configuration"]
    locations: list[ReviewLocation]
    identity_groups: list[list[str]]


class ProposalReview(_DTO):
    proposal_id: str
    preview_id: str
    sha256: str
    replacements: list[ReviewReplacement]
    relationships: list[ReviewRelationship]
    check: ReviewCheck
    needs_review: bool
    review_reason: str
    resolved_replacements: list[ResolvedReplacement]
    usage: dict[Literal["input_tokens", "output_tokens", "total_tokens"], int]
    diagnostics: PlanningDiagnostics
    validation: Literal["schema_validated"] = "schema_validated"
    transformations: Literal["not_run"] = "not_run"
    verification: Literal["not_run"] = "not_run"


ERRORS = {
    "invalid_har": (400, "Invalid HAR JSON or capture structure."),
    "unsupported_media_type": (415, "Use application/json with an unencoded UTF-8 HAR body."),
    "payload_too_large": (413, "HAR input exceeds the 16 MiB limit."),
    "invalid_selection": (422, "Select 1–10 unique supported integer IDs from one origin."),
    "capture_not_found": (404, "Capture not found or expired."),
    "capture_capacity": (409, "Capture capacity reached; delete a capture or wait for expiry."),
    "selection_required": (409, "Confirm a transaction selection before preparing a model preview."),
    "invalid_preview_settings": (422, "Choose a supported failure pattern and unique labels from the offered choices."),
    "label_choices_unavailable": (422, "Selected data is unsupported or exceeds schema-label or privacy limits."),
    "preview_unavailable": (422, "Selected data is unsupported or exceeds model-payload privacy limits."),
    "approval_required": (403, "Explicit approval of this exact preview is required before contacting Meta."),
    "invalid_plan_request": (422, "Provide only the active preview ID, SHA-256 digest and explicit approval."),
    "stale_preview": (409, "The preview or selection changed. The result was discarded; no retry was made."),
    "planning_consumed": (409, "Planning was already submitted for this capture. No additional request was made."),
    "missing_meta_configuration": (503, "Backend Meta API configuration is missing or invalid. No request was attempted."),
    "planning_timeout": (504, "Meta planning timed out. It was not retried."),
    "planning_transport_error": (502, "Meta planning failed due to a network or TLS error. It was not retried."),
    "planning_http_error": (502, "Meta returned an HTTP error. The request was not retried."),
    "planning_incomplete": (502, "Meta returned an incomplete response. Partial output was rejected; no retry was made."),
    "planning_response_error": (502, "Meta did not return an acceptable tool response. It was not retried."),
    "planning_invalid_proposal": (502, "The proposed plan failed local validation. It was not retried."),
    "planning_failed": (500, "Planning stopped safely. No automatic retry was made."),
    "invalid_host": (400, "Only loopback Host values are accepted."),
    "invalid_origin": (403, "Browser Origin is not allowed."),
    "invalid_request": (400, "Invalid local API request."),
    "not_found": (404, "Local route not found."),
    "method_not_allowed": (405, "Method not allowed."),
    "internal_error": (500, "Local request could not be completed."),
}


class CaptureApiError(Exception):
    """Only a fixed code; never attach captured input or parser diagnostics."""

    def __init__(self, code: str, *, diagnostics: PlanningDiagnostics | None = None):
        self.code = code
        self.diagnostics = diagnostics
        super().__init__(code)


def error_response(code: str, diagnostics: PlanningDiagnostics | None = None) -> JSONResponse:
    status, message = ERRORS[code]
    error = {"code": code, "message": message}
    if diagnostics is not None:
        error["diagnostics"] = diagnostics.model_dump(mode="json", exclude_none=True)
    return JSONResponse({"error": error}, status_code=status,
                        headers={"Cache-Control": "no-store"})
