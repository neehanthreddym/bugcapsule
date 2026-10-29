"""Execute explicit replacement plans locally; never discover fields or call a model.

JSON paths contain exact {"key": "..."} segments and array {"each": "*"}
segments, with no expressions, indices, object wildcards, or executable syntax.
Query rules use exactly one key segment, matched case-sensitively, and reject
duplicate query names. URL path identifiers, headers, form bodies/params, root
scalars, and custom HAR metadata are unsupported replacement targets.

Inputs must already be scrubbed, selected HarTransaction objects. Missing paths
and null intermediate objects are left alone; a rule must match at least one
existing field across its specified entries. Matched nulls/booleans stay unchanged.
Other leaves must be strings (any kind) or integers (id only). Equality is global
across targeted fields using (scalar type, original value); assigning different
kinds to one field or one equal typed source value is a conflict.

Synthetic counters never embed/hash/encode original values. All original strings
and integers in retained entries and decoded JSON are reserved against collisions.
Integer IDs use a sequence starting at 1_000_001; strings use fixed synthetic
labels and example.test emails. Query values stay strings, never coerced to IDs.
Maps exist only within the call and are never returned. Rewritten JSON bodies use
the scrubber's plain/base64 UTF-8 and size conventions. Results remain private:
untargeted data is retained, bugs are not repaired, and anonymization is not
guaranteed. This is not an outbound model-safe payload builder.
"""

from collections import Counter
from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass
import json
from typing import Literal
from urllib.parse import urlencode, urlsplit

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

from app.engine.scrub import (
    ScrubError, _is_json, _json_text, _mime, _named_items, _query_pairs,
    _read_text, _reject_constant, _set_text, _update_sizes,
)
from app.schemas import HarTransaction


ReplacementKind = Literal["id", "email", "name", "text"]
ReplacementTarget = Literal["response_json", "request_json", "query"]


class _PlanModel(BaseModel):
    model_config = ConfigDict(
        strict=True, frozen=True, extra="forbid", hide_input_in_errors=True
    )


class ObjectKey(_PlanModel):
    """An exact JSON object key, including literal punctuation or '*'."""

    key: str


class ArrayItems(_PlanModel):
    """Visit all items of an array in their original order."""

    each: Literal["*"]


class ReplacementRule(_PlanModel):
    entry_ids: list[int] = Field(min_length=1)
    target: ReplacementTarget
    path: list[ObjectKey | ArrayItems] = Field(min_length=1, repr=False)
    kind: ReplacementKind

    @field_validator("entry_ids")
    @classmethod
    def unique_nonnegative_ids(cls, value: list[int]) -> list[int]:
        if any(entry_id < 0 for entry_id in value) or len(set(value)) != len(value):
            raise ValueError("Entry IDs must be unique and nonnegative.")
        return value

    @model_validator(mode="after")
    def query_key_only(self) -> "ReplacementRule":
        if self.target == "query" and (
            len(self.path) != 1 or not isinstance(self.path[0], ObjectKey)
        ):
            raise ValueError("Query selectors require exactly one object key.")
        return self


class ReplacementPlan(_PlanModel):
    rules: list[ReplacementRule] = Field(min_length=1)


@dataclass(frozen=True)
class ReplacementCount:
    """Value-free occurrence counts; no original values, paths, or private maps."""

    entry_id: int
    target: ReplacementTarget
    kind: ReplacementKind
    count: int


@dataclass(frozen=True)
class ReplacementResult:
    transactions: list[HarTransaction]
    replacements: list[ReplacementCount]


class ReplacementError(ValueError):
    """An invalid plan or retained representation; messages never echo inputs."""


@dataclass(repr=False)
class _WorkingEntry:
    transaction: HarTransaction
    entry: dict
    response_json: JsonValue
    request_json: JsonValue
    has_request_json: bool


@dataclass(repr=False)
class _Occurrence:
    entry_id: int
    target: ReplacementTarget
    kind: ReplacementKind
    parent: dict | list
    key: str | int
    value: str | int


def validate_plan(data: object) -> ReplacementPlan:
    """Validate untrusted plan data with a fixed, value-free error on failure."""
    try:
        if isinstance(data, ReplacementPlan):
            data = data.model_dump(warnings=False)
        return ReplacementPlan.model_validate(data)
    except (ValueError, TypeError, RecursionError):
        raise ReplacementError("Invalid replacement plan.") from None


def _same_json(left: JsonValue, right: JsonValue) -> bool:
    return json.dumps(left, sort_keys=True, allow_nan=False) == json.dumps(
        right, sort_keys=True, allow_nan=False
    )


def _prepare(transaction: HarTransaction) -> _WorkingEntry:
    entry = deepcopy(transaction.original_entry)
    request, response = entry["request"], entry["response"]
    if request["url"] != transaction.url:
        raise ReplacementError("conflicting transaction URL representations")
    content = response["content"]
    if not _is_json(_mime(content)):
        raise ReplacementError("response target requires JSON")
    data = json.loads(_read_text(content), parse_constant=_reject_constant)
    if not _same_json(data, transaction.response_json):
        raise ReplacementError("conflicting parsed response and HAR body")
    query = _named_items(request["queryString"])
    if [(item["name"], item["value"]) for item in query] != _query_pairs(
        urlsplit(request["url"]).query
    ):
        raise ReplacementError("conflicting URL and queryString representations")
    post = request.get("postData")
    has_request_json = isinstance(post, dict) and _is_json(_mime(post))
    request_json = None
    if has_request_json:
        if post.get("params", []) != []:
            raise ReplacementError("ambiguous JSON request-body params")
        request_json = json.loads(_read_text(post), parse_constant=_reject_constant)
    return _WorkingEntry(transaction, entry, data, request_json, has_request_json)


def _resolve(node: JsonValue, path: list[ObjectKey | ArrayItems]) -> list[tuple]:
    if node is None:
        return []
    segment, remaining = path[0], path[1:]
    if isinstance(segment, ObjectKey):
        if not isinstance(node, dict):
            raise ReplacementError("selector requires an object")
        keys = [segment.key] if segment.key in node else []
    else:
        if not isinstance(node, list):
            raise ReplacementError("selector requires an array")
        keys = range(len(node))
    if not remaining:
        return [(node, key) for key in keys]
    matches = []
    for key in keys:
        matches.extend(_resolve(node[key], remaining))
    return matches


def _locations(work: _WorkingEntry, rule: ReplacementRule) -> list[tuple]:
    if rule.target == "query":
        items = [item for item in work.entry["request"]["queryString"]
                 if item["name"] == rule.path[0].key]
        if len(items) > 1:
            raise ReplacementError("ambiguous query target")
        return [(item, "value") for item in items]
    if rule.target == "request_json":
        if not work.has_request_json:
            raise ReplacementError("target requires a JSON request body")
        data = work.request_json
    else:
        data = work.response_json
    return _resolve(data, rule.path)


def _reserve(value: JsonValue, reserved: dict[type, set]) -> None:
    if type(value) in (str, int):
        reserved[type(value)].add(value)
    elif isinstance(value, dict):
        for child in value.values():
            _reserve(child, reserved)
    elif isinstance(value, list):
        for child in value:
            _reserve(child, reserved)


def _candidate(kind: ReplacementKind, scalar_type: type, number: int) -> str | int:
    if scalar_type is int:
        return 1_000_000 + number
    if kind == "id":
        return f"synthetic-id-{number:06d}"
    if kind == "email":
        return f"synthetic-{number:06d}@example.test"
    if kind == "name":
        return f"Synthetic Person {number:06d}"
    return f"Synthetic text {number:06d}"


def _finish(work: _WorkingEntry) -> HarTransaction:
    request, response = work.entry["request"], work.entry["response"]
    content = response["content"]
    content["size"] = _set_text(content, _json_text(work.response_json))
    content.pop("compression", None)
    _update_sizes(response, content["size"])
    if work.has_request_json:
        size = _set_text(request["postData"], _json_text(work.request_json))
        _update_sizes(request, size)
    parts = urlsplit(request["url"])
    request["url"] = parts._replace(query=urlencode([
        (item["name"], item["value"]) for item in request["queryString"]
    ])).geturl()
    request["headersSize"] = -1
    tx = work.transaction
    return HarTransaction(
        entry_id=tx.entry_id, method=tx.method, url=request["url"], origin=tx.origin,
        status=tx.status, response_json=work.response_json, original_entry=work.entry,
    )


def apply_replacements(
    transactions: Sequence[HarTransaction], plan: ReplacementPlan
) -> ReplacementResult:
    """Validate and execute an explicit plan atomically, retaining no public maps.

    Duplicate identical instructions apply once. Allocation follows rule order,
    entry_ids order, then array order; fixed input and plan yield fixed output.
    Null/bool matches are intentionally unchanged and not counted as replacements.
    """
    plan = validate_plan(plan)
    working: dict[int, _WorkingEntry] = {}
    for transaction in transactions:
        if transaction.entry_id in working:
            raise ReplacementError("Duplicate selected entry IDs.")
        try:
            working[transaction.entry_id] = _prepare(transaction)
        except (ReplacementError, ScrubError) as error:
            raise ReplacementError(f"Entry {transaction.entry_id}: {error}.") from None
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
            raise ReplacementError(f"Entry {transaction.entry_id}: invalid retained data.") from None

    occurrences = []
    locations = {}
    source_kinds = {}
    for rule_index, rule in enumerate(plan.rules):
        match_count = 0
        for entry_id in rule.entry_ids:
            if entry_id not in working:
                raise ReplacementError(f"Entry {entry_id}: unknown selected entry ID.")
            try:
                matches = _locations(working[entry_id], rule)
            except ReplacementError as error:
                raise ReplacementError(f"Entry {entry_id}: {error}.") from None
            except RecursionError:
                raise ReplacementError(f"Entry {entry_id}: selector exceeds supported nesting.") from None
            match_count += len(matches)
            for parent, key in matches:
                location = (entry_id, rule.target, id(parent), key)
                if location in locations:
                    if locations[location] != rule.kind:
                        raise ReplacementError(f"Entry {entry_id}: conflicting replacement instructions.")
                    continue
                locations[location] = rule.kind
                value = parent[key]
                if value is None or type(value) is bool:
                    continue
                if type(value) not in (str, int) or (type(value) is int and rule.kind != "id"):
                    raise ReplacementError(f"Entry {entry_id}: incompatible replacement target type.")
                source = (type(value), value)
                if source in source_kinds and source_kinds[source] != rule.kind:
                    raise ReplacementError(f"Entry {entry_id}: conflicting kinds for equal typed values.")
                source_kinds[source] = rule.kind
                occurrences.append(_Occurrence(entry_id, rule.target, rule.kind, parent, key, value))
        if match_count == 0:
            raise ReplacementError(f"Rule {rule_index}: selector matched no fields.")

    reserved = {str: set(), int: set()}
    try:
        for work in working.values():
            _reserve(work.entry, reserved)
            _reserve(work.response_json, reserved)
            _reserve(work.request_json, reserved)
        mapping = {}
        counters = Counter()
        counts = Counter()
        for occurrence in occurrences:
            scalar_type = type(occurrence.value)
            source = (scalar_type, occurrence.value)
            if source not in mapping:
                counter_key = (occurrence.kind, scalar_type)
                while True:
                    counters[counter_key] += 1
                    candidate = _candidate(occurrence.kind, scalar_type, counters[counter_key])
                    if candidate not in reserved[scalar_type]:
                        break
                mapping[source] = candidate
                reserved[scalar_type].add(candidate)
            occurrence.parent[occurrence.key] = mapping[source]
            counts[(occurrence.entry_id, occurrence.target, occurrence.kind)] += 1
        result = [_finish(work) for work in working.values()]
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise ReplacementError("Could not safely synchronize replacement output.") from None
    return ReplacementResult(
        transactions=result,
        replacements=[ReplacementCount(*key, count) for key, count in sorted(counts.items())],
    )
