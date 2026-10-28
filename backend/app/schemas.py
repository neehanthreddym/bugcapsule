"""Private, local HAR models; never use these as model payloads or API responses."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class _LocalHarModel(BaseModel):
    """Hide validation inputs and keep private capture fields out of repr output."""

    model_config = ConfigDict(
        strict=True, frozen=True, hide_input_in_errors=True, allow_inf_nan=False
    )


class HarTransaction(_LocalHarModel):
    """One supported transaction containing unsanitized, local-only private data.

    Frozen fields do not make nested JSON immutable. Callers must not modify the
    retained entry. Excluding it from serialization is only an accidental-use
    safeguard: the other fields also contain private data and are not outbound-safe.
    """

    entry_id: int = Field(ge=0)
    method: str = Field(min_length=1, repr=False)
    url: str = Field(min_length=1, repr=False)
    origin: str = Field(min_length=1, repr=False)
    status: int = Field(ge=100, le=599, repr=False)
    response_json: JsonValue = Field(repr=False)
    original_entry: dict[str, JsonValue] = Field(repr=False, exclude=True)


class SkippedHarEntry(_LocalHarModel):
    """An original entry index and a fixed reason without captured content."""

    entry_id: int = Field(ge=0)
    reason: Literal[
        "unsupported MIME type",
        "unsupported URL scheme",
        "unsupported content encoding",
    ]


class ParsedHar(_LocalHarModel):
    """Local capture data, origin-to-entry-ID groups, and unsupported-entry reasons."""

    transactions: list[HarTransaction] = Field(repr=False)
    by_origin: dict[str, list[int]] = Field(repr=False)
    skipped_entries: list[SkippedHarEntry]
