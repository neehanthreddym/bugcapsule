"""The sole outbound payload boundary: scrub first, then construct new allowlisted data.

No partial replacement plan is trusted as a privacy guarantee. All scalar examples
are per-call typed tokens, never hashes/encodings/prefixes of originals. All schema
labels are aliased unless the caller explicitly approves the exact label text;
approval applies to that text across object/query locations in this payload only.
There are no default approved labels or inferred approvals.

JSON summaries retain all observed types, array-length distributions, object-field
presence/missing counts, and explicit null markers, including when examples are
limited. Arrays are summarized through wildcard selectors, not copied records.
Examples include occurrence counts, with at most MAX_EXAMPLES_PER_NODE distinct
typed examples per node and MAX_EXAMPLES_TOTAL overall. Safe metadata discloses
omitted examples. Structural limits reject rather than silently omit structure.

MAX_DEPTH counts JSON edges from each decoded document root; MAX_LOCAL_DEPTH
bounds retained HAR metadata. MAX_INPUT_NODES/MAX_INPUT_BYTES count the combined
scrubbed entries and decoded documents (keys and string values count toward bytes).
MAX_STRUCTURE_NODES, MAX_FIELDS_PER_OBJECT, MAX_PAYLOAD_BYTES, and approval limits
are hard caps. UTF-8 JSON request bodies and unique query names are supported;
form/opaque request bodies and ambiguous repeated query names are rejected.

Only entry IDs, recognized REST methods/status codes, fixed pattern identifiers,
structure/counts, explicitly approved labels, and generated references/tokens leave
this boundary. Private bindings contain local paths, never outbound fields. Token
maps are discarded after each call. The preview is the exact retained JSON snapshot;
the future client must send it unchanged after later workflow approval. Preparation
does not send anything or imply approval. Pseudonymized structures and approved
labels can still reveal context: this never guarantees anonymization.
"""

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from http import HTTPStatus
import json
import math
from types import MappingProxyType
from typing import Literal

from pydantic import JsonValue

from app.engine.har import _origin
from app.engine.replace import ArrayItems, ObjectKey
from app.engine.scrub import (
    _is_json, _mime, _read_text, _reject_constant, scrub_transactions,
)
from app.schemas import HarTransaction
from app.verify.schemas import VerificationResult


FailurePattern = Literal[
    "duplicate_pagination", "missing_null_fields", "inconsistent_identifier_references"
]
_PATTERNS = frozenset({
    "duplicate_pagination", "missing_null_fields", "inconsistent_identifier_references"
})
_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})

MAX_TRANSACTIONS = 10
MAX_DEPTH = 8
MAX_LOCAL_DEPTH = 32
MAX_INPUT_NODES = 20_000
MAX_INPUT_BYTES = 2_000_000
MAX_STRUCTURE_NODES = 256
MAX_FIELDS_PER_OBJECT = 64
MAX_EXAMPLES_PER_NODE = 3
MAX_EXAMPLES_TOTAL = 128
MAX_PAYLOAD_BYTES = 65_536
MAX_APPROVED_LABELS = 64
MAX_APPROVED_LABEL_BYTES = 64
MAX_FEEDBACK_COUNTS = 1_000_000_000
MAX_FEEDBACK_LOCATIONS = 100


class PrivacyError(ValueError):
    """Unsupported input or a boundary limit; never contains captured values."""


@dataclass(frozen=True, slots=True, repr=False)
class LocalBinding:
    """Private selectors compatible with Step 6; deliberately excluded from repr."""

    entry_id: int
    target: Literal["response_json", "request_json", "query"]
    path: tuple[ObjectKey | ArrayItems, ...]


class PrivateBindings:
    """Local lookup only; this object is not JSON serializable."""

    __slots__ = ("_locations",)

    def __init__(self, locations: dict[str, LocalBinding]):
        self._locations = MappingProxyType(dict(locations))

    def resolve(self, reference: str) -> LocalBinding:
        if not isinstance(reference, str) or reference not in self._locations:
            raise PrivacyError("Unknown local binding reference.")
        return self._locations[reference]

    def __repr__(self) -> str:
        return "PrivateBindings()"


class PreparedPayload:
    """A reviewed-byte snapshot plus private bindings, with no automatic serializer.

    payload returns a fresh copy of the restricted JSON object, so modifying it
    cannot make the retained preview stale. Send preview_json unchanged in the
    future approved workflow; never serialize this wrapper or its bindings.
    """

    __slots__ = ("_preview_json", "_bindings")

    def __init__(self, preview_json: str, bindings: PrivateBindings):
        self._preview_json = preview_json
        self._bindings = bindings

    @property
    def payload(self) -> dict[str, JsonValue]:
        return json.loads(self._preview_json)

    @property
    def preview_json(self) -> str:
        return self._preview_json

    @property
    def bindings(self) -> PrivateBindings:
        return self._bindings

    def __repr__(self) -> str:
        return "PreparedPayload()"


def _type(value: JsonValue) -> str:
    if value is None:
        return "null"
    if type(value) is bool:
        return "boolean"
    if type(value) is int:
        return "integer"
    if type(value) is float and math.isfinite(value):
        return "number"
    if type(value) is str:
        return "string"
    if type(value) is dict:
        return "object"
    if type(value) is list:
        return "array"
    raise PrivacyError("Unsupported local JSON value.")


class _Builder:
    """Ephemeral source/token maps; never returned, serialized, or logged."""

    def __init__(self, approved: frozenset[str]):
        self.approved = approved
        self.blocked = set(approved)
        self.counters = Counter()
        self.aliases = {}
        self.tokens = {}
        self.locations = {}
        self.nodes = 0
        self.input_nodes = 0
        self.input_bytes = 0
        self.examples = 0
        self.omitted = 0

    def _string(self, text: str) -> None:
        self.input_bytes += len(text.encode("utf-8"))
        if self.input_bytes > MAX_INPUT_BYTES:
            raise PrivacyError("Local input byte limit exceeded.")
        self.blocked.add(text)

    def scan(self, value: JsonValue, depth: int, limit: int) -> None:
        self.input_nodes += 1
        if self.input_nodes > MAX_INPUT_NODES:
            raise PrivacyError("Local input node limit exceeded.")
        if depth > limit:
            raise PrivacyError("Local nesting limit exceeded.")
        tag = _type(value)
        if tag == "string":
            self._string(value)
        elif tag == "object":
            for key, child in value.items():
                if type(key) is not str:
                    raise PrivacyError("Unsupported local JSON key.")
                self._string(key)
                self.scan(child, depth + 1, limit)
        elif tag == "array":
            for child in value:
                self.scan(child, depth + 1, limit)

    def fresh(self, prefix: str) -> str:
        while True:
            self.counters[prefix] += 1
            candidate = f"{prefix}_{self.counters[prefix]:04d}"
            if candidate not in self.blocked:
                self.blocked.add(candidate)
                return candidate

    def label(self, key: str) -> str:
        if key in self.approved:
            return key
        if key not in self.aliases:
            self.aliases[key] = self.fresh("field")
        return self.aliases[key]

    def summarize(self, values: list[JsonValue], entry_id: int, target: str,
                  path: tuple = ()) -> dict:
        self.nodes += 1
        if self.nodes > MAX_STRUCTURE_NODES:
            raise PrivacyError("Structural node limit exceeded.")
        ref = self.fresh("node")
        self.locations[ref] = LocalBinding(entry_id, target, path)
        types = Counter(_type(value) for value in values)
        node = {"ref": ref, "types": [
            {"type": tag, "count": count} for tag, count in sorted(types.items())
        ]}
        scalar_counts = {}
        for value in values:
            if _type(value) in {"object", "array"}:
                continue
            source = (type(value), value)
            scalar_counts[source] = scalar_counts.get(source, 0) + 1
            if value is not None and source not in self.tokens:
                self.tokens[source] = self.fresh("value")
        if scalar_counts:
            examples = []
            for source, count in scalar_counts.items():
                if len(examples) >= MAX_EXAMPLES_PER_NODE or self.examples >= MAX_EXAMPLES_TOTAL:
                    continue
                value = source[1]
                examples.append({
                    "type": _type(value),
                    "token": None if value is None else self.tokens[source],
                    "count": count,
                })
                self.examples += 1
            omitted = len(scalar_counts) - len(examples)
            self.omitted += omitted
            node["examples"] = examples
            node["examples_omitted"] = omitted

        objects = [value for value in values if type(value) is dict]
        if objects:
            keys = dict.fromkeys(key for obj in objects for key in obj)
            if len(keys) > MAX_FIELDS_PER_OBJECT:
                raise PrivacyError("Object field limit exceeded.")
            fields = []
            for key in keys:
                children = [obj[key] for obj in objects if key in obj]
                fields.append({
                    "label": self.label(key), "present": len(children),
                    "missing": len(objects) - len(children),
                    "node": self.summarize(children, entry_id, target, path + (ObjectKey(key=key),)),
                })
            node["fields"] = fields
        arrays = [value for value in values if type(value) is list]
        if arrays:
            lengths = Counter(len(value) for value in arrays)
            node["array_lengths"] = [
                {"items": length, "arrays": count} for length, count in sorted(lengths.items())
            ]
            items = [item for array in arrays for item in array]
            if items:
                node["items"] = self.summarize(items, entry_id, target, path + (ArrayItems(each="*"),))
        return node


def _validate_selection(transactions: Sequence[HarTransaction]) -> None:
    if isinstance(transactions, (str, bytes)) or not isinstance(transactions, Sequence):
        raise PrivacyError("Selection must contain local transactions.")
    if not 1 <= len(transactions) <= MAX_TRANSACTIONS:
        raise PrivacyError("Selection must contain 1–10 transactions.")
    ids, origins = set(), set()
    for tx in transactions:
        if not isinstance(tx, HarTransaction) or type(tx.entry_id) is not int or tx.entry_id < 0:
            raise PrivacyError("Invalid local transaction.")
        if tx.entry_id in ids:
            raise PrivacyError("Selection entry IDs must be unique.")
        ids.add(tx.entry_id)
        origin = _origin(tx.url, tx.entry_id)
        if origin is None or origin != tx.origin:
            raise PrivacyError("Invalid local transaction origin.")
        origins.add(origin)
        if tx.method not in _METHODS or type(tx.status) is not int:
            raise PrivacyError("Unsupported HTTP method or status.")
        HTTPStatus(tx.status)
        if (tx.original_entry["request"]["method"] != tx.method
                or tx.original_entry["response"]["status"] != tx.status):
            raise PrivacyError("Conflicting local transaction metadata.")
    if len(origins) != 1:
        raise PrivacyError("Selection must use exactly one API origin.")


def prepare_model_payload(
    transactions: Sequence[HarTransaction], failure_pattern: FailurePattern, *,
    approved_labels: frozenset[str] = frozenset(),
) -> PreparedPayload:
    """Scrub internally and return one restricted, inspectable JSON snapshot.

    PrivateBindings.resolve(node_ref) returns local Step 6 path segments. Parent
    nodes/root refs can resolve too, but later plan validation must still reject
    unsupported replacement locations/types. This function grants no approval.
    """
    try:
        if type(failure_pattern) is not str or failure_pattern not in _PATTERNS:
            raise PrivacyError("Unsupported failure pattern.")
        if not isinstance(approved_labels, (set, frozenset)) or len(approved_labels) > MAX_APPROVED_LABELS:
            raise PrivacyError("Invalid schema-label approval.")
        if any(type(label) is not str or len(label.encode("utf-8")) > MAX_APPROVED_LABEL_BYTES
               for label in approved_labels):
            raise PrivacyError("Invalid schema-label approval.")
        _validate_selection(transactions)
        # No outbound extraction occurs before this mandatory scrubber call.
        scrubbed = scrub_transactions(transactions).transactions
        builder = _Builder(frozenset(approved_labels))
        documents = []
        for tx in scrubbed:
            builder.scan(tx.original_entry, 0, MAX_LOCAL_DEPTH)
            request = tx.original_entry["request"]
            query = {}
            for item in request["queryString"]:
                if item["name"] in query:
                    raise PrivacyError("Ambiguous repeated query names are unsupported.")
                query[item["name"]] = item["value"]
            docs = {"query": query, "response_json": tx.response_json}
            if "postData" in request:
                post = request["postData"]
                if not _is_json(_mime(post)):
                    raise PrivacyError("Only JSON request bodies are supported.")
                docs["request_json"] = json.loads(_read_text(post), parse_constant=_reject_constant)
            for data in docs.values():
                builder.scan(data, 0, MAX_DEPTH)
            documents.append((tx, docs))

        entries = []
        for tx, docs in documents:
            entry = {"entry_id": tx.entry_id, "method": tx.method, "status": tx.status}
            for target, data in docs.items():
                entry[target] = builder.summarize([data], tx.entry_id, target)
            entries.append(entry)
        payload = {
            "failure_pattern": failure_pattern, "transactions": entries,
            "metadata": {
                "examples_truncated": builder.omitted > 0,
                "examples_omitted": builder.omitted,
                "max_examples_per_node": MAX_EXAMPLES_PER_NODE,
                "max_examples_total": MAX_EXAMPLES_TOTAL,
            },
        }
        preview = json.dumps(payload, ensure_ascii=True, allow_nan=False, indent=2)
        if len(preview.encode("utf-8")) > MAX_PAYLOAD_BYTES:
            raise PrivacyError("Restricted payload byte limit exceeded.")
        return PreparedPayload(preview, PrivateBindings(builder.locations))
    except PrivacyError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise PrivacyError("Unsupported or malformed local input.") from None


def prepare_revision_payload(
    transactions: Sequence[HarTransaction], original: PreparedPayload,
    verification: VerificationResult, *,
    prior_replacements: Sequence[tuple[str, str]], check_refs: Sequence[str],
    definition_refs: Sequence[str] = (), approved_labels: frozenset[str] = frozenset(),
) -> PreparedPayload:
    """Rebuild the original restricted snapshot and add allowlisted changed feedback.

    Nothing from a candidate or model prose is copied. The original snapshot must
    match a fresh preparation of the baseline, preserving node/token identities.
    Only fixed reason codes, bounded counts, existing refs/kinds, and explicit
    frozen check roles are added. Each evidence list is capped at 100 locations;
    counts are nonnegative integers at most MAX_FEEDBACK_COUNTS. The existing
    MAX_PAYLOAD_BYTES cap includes feedback. This creates no send approval.
    """
    try:
        if not isinstance(original, PreparedPayload) or not isinstance(verification, VerificationResult):
            raise ValueError
        verification = VerificationResult.model_validate(verification.model_dump(warnings=False))
        if verification.outcome != "changed":
            raise ValueError
        pattern = original.payload["failure_pattern"]
        fresh = prepare_model_payload(transactions, pattern, approved_labels=approved_labels)
        if fresh.preview_json != original.preview_json:
            raise ValueError
        payload = fresh.payload
        known = {}

        def visit(node):
            known[node["ref"]] = original.bindings.resolve(node["ref"])
            for field in node.get("fields", []):
                visit(field["node"])
            if "items" in node:
                visit(node["items"])

        for entry in payload["transactions"]:
            for target in ("query", "request_json", "response_json"):
                if target in entry:
                    visit(entry[target])

        def refs(values):
            if (isinstance(values, (str, bytes)) or not isinstance(values, Sequence)
                    or len(values) > 10 or any(type(ref) is not str or ref not in known for ref in values)
                    or len(set(values)) != len(values)):
                raise ValueError
            return list(values)

        def count(value):
            if type(value) is not int or not 0 <= value <= MAX_FEEDBACK_COUNTS:
                raise ValueError
            return value

        frozen_refs, definitions = refs(check_refs), refs(definition_refs)
        if not frozen_refs:
            raise ValueError
        if pattern == "inconsistent_identifier_references":
            if not definitions or not set(definitions) < set(frozen_refs):
                raise ValueError
        elif definitions:
            raise ValueError
        canonical = "inconsistent_references" if pattern == "inconsistent_identifier_references" else pattern
        if verification.check != canonical:
            raise ValueError
        if (isinstance(prior_replacements, (str, bytes)) or not isinstance(prior_replacements, Sequence)
                or not 1 <= len(prior_replacements) <= 64):
            raise ValueError
        prior = []
        for pair in prior_replacements:
            if not isinstance(pair, (tuple, list)) or len(pair) != 2:
                raise ValueError
            ref, kind = pair
            if type(ref) is not str or ref not in known or type(kind) is not str or kind not in {"id", "email", "name", "text"}:
                raise ValueError
            prior.append({"ref": ref, "kind": kind})

        def evidence(summary):
            if summary is None or len(summary.record_counts) > MAX_FEEDBACK_LOCATIONS:
                raise ValueError
            result = {"observations": count(summary.observations), "failures": count(summary.failures)}
            if summary.distinct_identities is not None:
                result["distinct_identities"] = count(summary.distinct_identities)
            locations = []
            for record in summary.record_counts:
                safe_refs = refs(record.location.refs)
                if not safe_refs or any(known[ref].entry_id != record.location.entry_id for ref in safe_refs):
                    raise ValueError
                locations.append({"entry_id": record.location.entry_id, "refs": safe_refs, "count": count(record.count)})
            result["record_counts"] = locations
            return result

        payload["revision"] = {
            "verification": {"outcome": "changed", "reason": verification.reason,
                             "baseline": evidence(verification.baseline),
                             "candidate": evidence(verification.transformed)},
            "prior_replacements": prior,
            "frozen_check": {"type": pattern, "refs": frozen_refs, "definition_refs": definitions},
        }
        preview = json.dumps(payload, ensure_ascii=True, allow_nan=False, indent=2)
        if len(preview.encode("utf-8")) > MAX_PAYLOAD_BYTES:
            raise ValueError
        return PreparedPayload(preview, original.bindings)
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise PrivacyError("Cannot prepare restricted revision feedback.") from None
