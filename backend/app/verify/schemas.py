"""Explicit local check configurations and scalar-free verification reports.

Configurations contain private selectors; never publish them as reports. Use
validate_check/check_from_proposal in checks.py for value-free validation errors.
Reports contain only fixed codes, safe refs, counts, and array index locations.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.engine.replace import ArrayItems, ObjectKey


CheckType = Literal["duplicate_pagination", "missing_null_fields", "inconsistent_references"]
Outcome = Literal["preserved", "changed", "not_present", "inconclusive"]
ReasonCode = Literal[
    "failure_preserved", "baseline_failure_absent", "failure_removed",
    "equality_relationships_changed", "field_states_changed", "reference_resolution_changed",
    "value_types_changed", "record_counts_changed", "record_locations_changed",
    "entry_set_changed", "insufficient_evidence", "incompatible_data", "invalid_input",
]
ValueType = Literal["string", "integer", "number", "boolean", "null", "object", "array"]


class _Model(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid", hide_input_in_errors=True)


class FieldSelector(_Model):
    entry_id: int = Field(ge=0)
    target: Literal["response_json", "request_json", "query"] = "response_json"
    path: list[ObjectKey | ArrayItems] = Field(min_length=1, repr=False)
    ref: str | None = Field(default=None, pattern=r"^node_[0-9]{4,}$", max_length=64)

    @model_validator(mode="after")
    def _query_key(self) -> "FieldSelector":
        if self.target == "query" and (len(self.path) != 1 or not isinstance(self.path[0], ObjectKey)):
            raise ValueError("Query selectors require one exact key.")
        return self


class IdentityPage(_Model):
    """Ordered fields forming one composite identity for each record on a page."""

    fields: list[FieldSelector] = Field(min_length=1, max_length=10)


class DuplicatePaginationCheck(_Model):
    type: Literal["duplicate_pagination"] = "duplicate_pagination"
    pages: list[IdentityPage] = Field(min_length=2, max_length=10)


class MissingNullFieldsCheck(_Model):
    """Explicitly selected terminal keys are expected to be present and non-null."""

    type: Literal["missing_null_fields"] = "missing_null_fields"
    fields: list[FieldSelector] = Field(min_length=1, max_length=10)


class InconsistentReferencesCheck(_Model):
    type: Literal["inconsistent_references"] = "inconsistent_references"
    definitions: list[FieldSelector] = Field(min_length=1, max_length=10)
    references: list[FieldSelector] = Field(min_length=1, max_length=10)


CheckConfig = DuplicatePaginationCheck | MissingNullFieldsCheck | InconsistentReferencesCheck


class Location(_Model):
    entry_id: int
    target: Literal["response_json", "request_json", "query"]
    refs: tuple[str, ...]
    record: tuple[int, ...]


class RecordCount(_Model):
    location: Location
    count: int


class FieldState(_Model):
    location: Location
    state: Literal["missing", "null", "present"]
    value_type: ValueType | None


class ReferenceResolution(_Model):
    location: Location
    state: Literal["resolved", "dangling"]
    definitions: tuple[Location, ...]


class Evidence(_Model):
    observations: int
    failures: int
    distinct_identities: int | None = None
    record_counts: tuple[RecordCount, ...] = ()
    equality_groups: tuple[tuple[Location, ...], ...] = ()
    fields: tuple[FieldState, ...] = ()
    definition_locations: tuple[Location, ...] = ()
    resolutions: tuple[ReferenceResolution, ...] = ()


class VerificationResult(_Model):
    check: CheckType
    outcome: Outcome
    reason: ReasonCode
    baseline: Evidence | None = None
    transformed: Evidence | None = None
