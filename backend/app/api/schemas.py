"""Explicit local summary DTOs and fixed errors, never raw capture models.

Origins and display paths are local routing information, not model-safe data.
Display paths omit query strings, userinfo and fragments; path identifiers are
not anonymized. Bodies, headers and retained HAR entries have no DTO fields.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import JSONResponse


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


ERRORS = {
    "invalid_har": (400, "Invalid HAR JSON or capture structure."),
    "unsupported_media_type": (415, "Use application/json with an unencoded UTF-8 HAR body."),
    "payload_too_large": (413, "HAR input exceeds the 16 MiB limit."),
    "invalid_selection": (422, "Select 1–10 unique supported integer IDs from one origin."),
    "capture_not_found": (404, "Capture not found or expired."),
    "capture_capacity": (409, "Capture capacity reached; delete a capture or wait for expiry."),
    "invalid_host": (400, "Only loopback Host values are accepted."),
    "invalid_origin": (403, "Browser Origin is not allowed."),
    "invalid_request": (400, "Invalid local API request."),
    "not_found": (404, "Local route not found."),
    "method_not_allowed": (405, "Method not allowed."),
    "internal_error": (500, "Local request could not be completed."),
}


class CaptureApiError(Exception):
    """Only a fixed code; never attach captured input or parser diagnostics."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def error_response(code: str) -> JSONResponse:
    status, message = ERRORS[code]
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status,
                        headers={"Cache-Control": "no-store"})
