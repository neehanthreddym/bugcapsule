"""Validated shapes for the single capsule-planning tool call.

The model proposes replacements by payload node reference only, never by local
paths, values, or selectors. Free-text reasons are display-only notes for human
review and are never executed. There are no code, shell, URL, or expression
fields.
"""

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


TOOL_NAME = "propose_capsule_plan"
TOOL_DESCRIPTION = (
    "Propose relationship-preserving replacements and one verification check "
    "from the supplied restricted payload. Use payload node references only."
)

TOOL_PARAMETERS: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["replacements", "relationships", "check", "needs_review", "review_reason"],
    "properties": {
        "replacements": {
            "type": "array",
            "minItems": 0,
            "maxItems": 64,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["ref", "kind"],
                "properties": {
                    "ref": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 64,
                        "pattern": "^[A-Za-z0-9_.-]+$",
                    },
                    "kind": {"type": "string", "enum": ["id", "email", "name", "text"]},
                },
            },
        },
        "relationships": {
            "type": "array",
            "minItems": 0,
            "maxItems": 32,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["refs", "reason"],
                "properties": {
                    "refs": {
                        "type": "array",
                        "minItems": 2,
                        "maxItems": 10,
                        "uniqueItems": True,
                        "items": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 64,
                            "pattern": "^[A-Za-z0-9_.-]+$",
                        },
                    },
                    "reason": {"type": "string", "minLength": 1, "maxLength": 280},
                },
            },
        },
        "check": {
            "type": "object",
            "additionalProperties": False,
            "required": ["type", "refs"],
            "properties": {
                "type": {
                    "type": "string",
                    "enum": [
                        "duplicate_pagination",
                        "missing_null_fields",
                        "inconsistent_identifier_references",
                    ],
                },
                "refs": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 10,
                    "uniqueItems": True,
                    "items": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 64,
                        "pattern": "^[A-Za-z0-9_.-]+$",
                    },
                },
                "note": {"type": "string", "maxLength": 500},
            },
        },
        "needs_review": {"type": "boolean"},
        "review_reason": {"type": "string", "maxLength": 1000},
    },
}

_REF_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")


class _AgentModel(BaseModel):
    model_config = ConfigDict(
        strict=True, frozen=True, extra="forbid", hide_input_in_errors=True,
        allow_inf_nan=False,
    )


def _check_refs(value: list[str], *, minimum: int) -> list[str]:
    if len(value) < minimum or len(value) > 10:
        raise ValueError("Reference list has an unsupported length.")
    if len(set(value)) != len(value):
        raise ValueError("Reference lists must not repeat references.")
    for ref in value:
        if type(ref) is not str or not 1 <= len(ref) <= 64 or _REF_PATTERN.fullmatch(ref) is None:
            raise ValueError("Invalid payload reference.")
    return value


class ReplacementProposal(_AgentModel):
    ref: str = Field(min_length=1, max_length=64)
    kind: Literal["id", "email", "name", "text"]

    @field_validator("ref")
    @classmethod
    def _valid_ref(cls, value: str) -> str:
        if _REF_PATTERN.fullmatch(value) is None:
            raise ValueError("Invalid payload reference.")
        return value


class RelationshipProposal(_AgentModel):
    refs: list[str] = Field(min_length=2, max_length=10)
    reason: str = Field(min_length=1, max_length=280)

    @field_validator("refs")
    @classmethod
    def _valid_refs(cls, value: list[str]) -> list[str]:
        return _check_refs(value, minimum=2)


class CheckProposal(_AgentModel):
    type: Literal[
        "duplicate_pagination",
        "missing_null_fields",
        "inconsistent_identifier_references",
    ]
    refs: list[str] = Field(min_length=1, max_length=10)
    note: str = Field(default="", max_length=500)

    @field_validator("refs")
    @classmethod
    def _valid_refs(cls, value: list[str]) -> list[str]:
        return _check_refs(value, minimum=1)


class ProposeCapsulePlanArgs(_AgentModel):
    replacements: list[ReplacementProposal] = Field(min_length=0, max_length=64)
    relationships: list[RelationshipProposal] = Field(min_length=0, max_length=32)
    check: CheckProposal
    needs_review: bool
    review_reason: str = Field(min_length=0, max_length=1000)

    @model_validator(mode="after")
    def _consistent_outcome(self) -> "ProposeCapsulePlanArgs":
        if self.needs_review:
            if not self.review_reason.strip():
                raise ValueError("A needs-review outcome requires a reason.")
        elif not self.replacements:
            raise ValueError("A confident plan requires at least one replacement.")
        kinds: dict[str, str] = {}
        for item in self.replacements:
            if item.ref in kinds and kinds[item.ref] != item.kind:
                raise ValueError("Conflicting kinds for one reference.")
            kinds[item.ref] = item.kind
        return self
