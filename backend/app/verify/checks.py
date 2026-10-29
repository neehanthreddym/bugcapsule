"""Full-data, local verification of exactly three explicitly configured failures.

Selectors reuse ObjectKey/ArrayItems and PrivateBindings, never field-name
heuristics or model examples. Duplicate checks use ordered composite identity
fields per distinct transaction/page. They compare the complete typed equality
partition, including within-page relationships, and require a cross-page duplicate
in the baseline. Reference checks compare full definition/reference partitions and
the exact definition locations reached by every reference. Identifier leaves must
be integers or strings; nulls, booleans, floats, missing IDs, or containers are
inconclusive. Missing/null checks support all JSON leaf types, but require stable
present types and each field's exact state, not just missing/null totals.

All intermediate containers must exist with the declared shape. Only terminal
object keys may be missing; null/missing intermediate containers are inconclusive.
Empty, known arrays are represented by their full shapes. No required fields or
identifier roles are inferred. Arrays correspond by their index tuples; this
checks location relationships, not business-level record identity or reordering.

Preserved means a baseline failure exists and its full selected signature survives.
Not_present means no selected failure exists in a valid baseline. Invalid evidence
is inconclusive; invalid configuration raises a fixed CheckConfigurationError.
Inputs are never modified. Reports contain no scalar values or private paths.
"""

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
import math

from app.agent.schemas import CheckProposal
from app.engine.har import _origin
from app.engine.replace import ArrayItems, ObjectKey, _prepare
from app.privacy import PrivateBindings
from app.schemas import HarTransaction
from app.verify.schemas import (
    CheckConfig, DuplicatePaginationCheck, Evidence, FieldSelector, FieldState,
    IdentityPage, InconsistentReferencesCheck, Location, MissingNullFieldsCheck,
    RecordCount, ReferenceResolution, VerificationResult,
)


class CheckConfigurationError(ValueError):
    """Invalid explicit configuration; never echoes selectors or provider notes."""


class _Incompatible(ValueError):
    pass


class _Insufficient(_Incompatible):
    pass


_MISSING = object()


def _selectors(check: CheckConfig) -> list[FieldSelector]:
    if isinstance(check, DuplicatePaginationCheck):
        return [field for page in check.pages for field in page.fields]
    if isinstance(check, MissingNullFieldsCheck):
        return check.fields
    return check.definitions + check.references


def _path_key(path) -> tuple:
    return tuple(("key", segment.key) if isinstance(segment, ObjectKey) else ("each",)
                 for segment in path)


def _record_root(path) -> tuple:
    wildcards = [index for index, segment in enumerate(path) if isinstance(segment, ArrayItems)]
    return _path_key(path[:wildcards[-1] + 1] if wildcards else path[:-1])


def validate_check(data: object) -> CheckConfig:
    """Validate local configuration without returning Pydantic input diagnostics."""
    try:
        if isinstance(data, (DuplicatePaginationCheck, MissingNullFieldsCheck, InconsistentReferencesCheck)):
            data = data.model_dump(warnings=False)
        models = {"duplicate_pagination": DuplicatePaginationCheck,
                  "missing_null_fields": MissingNullFieldsCheck,
                  "inconsistent_references": InconsistentReferencesCheck}
        if not isinstance(data, dict) or type(data.get("type")) is not str:
            raise ValueError
        check = models[data["type"]].model_validate(data)
        seen, refs = set(), set()
        for selector in _selectors(check):
            key = (selector.entry_id, selector.target, _path_key(selector.path))
            if key in seen or (selector.ref is not None and selector.ref in refs):
                raise ValueError
            seen.add(key)
            if selector.ref is not None:
                refs.add(selector.ref)
        if isinstance(check, DuplicatePaginationCheck):
            pages = set()
            arity = len(check.pages[0].fields)
            for page in check.pages:
                first = page.fields[0]
                if len(page.fields) != arity or first.entry_id in pages:
                    raise ValueError
                pages.add(first.entry_id)
                if any((field.entry_id, field.target, _record_root(field.path)) !=
                       (first.entry_id, first.target, _record_root(first.path)) for field in page.fields):
                    raise ValueError
        return check
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise CheckConfigurationError("Invalid check configuration.") from None


def check_from_proposal(
    proposal: CheckProposal, bindings: PrivateBindings, *,
    definition_refs: Sequence[str] | None = None,
) -> CheckConfig:
    """Resolve an existing agent proposal locally, discarding free-text notes.

    The agent's existing inconsistent_identifier_references name maps only here
    to inconsistent_references. Its untyped ref list cannot establish roles:
    definition_refs must explicitly partition it into definitions/references.
    """
    try:
        if not isinstance(proposal, CheckProposal) or not isinstance(bindings, PrivateBindings):
            raise ValueError
        proposal = CheckProposal.model_validate(proposal.model_dump(warnings=False))
        fields = []
        for ref in proposal.refs:
            binding = bindings.resolve(ref)
            fields.append(FieldSelector(entry_id=binding.entry_id, target=binding.target,
                                        path=list(binding.path), ref=ref))
        if proposal.type == "duplicate_pagination":
            if definition_refs is not None:
                raise ValueError
            grouped = {}
            for field in fields:
                grouped.setdefault(field.entry_id, []).append(field)
            check = DuplicatePaginationCheck(pages=[IdentityPage(fields=group) for group in grouped.values()])
        elif proposal.type == "missing_null_fields":
            if definition_refs is not None:
                raise ValueError
            check = MissingNullFieldsCheck(fields=fields)
        else:
            if (isinstance(definition_refs, (str, bytes)) or not isinstance(definition_refs, Sequence)
                    or not definition_refs or len(set(definition_refs)) != len(definition_refs)
                    or not set(definition_refs) < set(proposal.refs)):
                raise ValueError
            check = InconsistentReferencesCheck(
                definitions=[field for field in fields if field.ref in definition_refs],
                references=[field for field in fields if field.ref not in definition_refs],
            )
        return validate_check(check)
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise CheckConfigurationError("Invalid check proposal or role configuration.") from None


@dataclass(repr=False)
class _Observation:
    location: Location
    value: object


@dataclass(repr=False)
class _Extracted:
    observations: list[_Observation]
    shape: tuple
    count: RecordCount


def _extract(work, selector: FieldSelector, reference: str) -> _Extracted:
    observations, shape = [], []

    def location(indices):
        return Location(entry_id=selector.entry_id, target=selector.target,
                        refs=(reference,), record=indices)

    def visit(node, segments, indices):
        segment, remaining = segments[0], segments[1:]
        if isinstance(segment, ObjectKey):
            if type(node) is not dict:
                raise _Incompatible
            child = node.get(segment.key, _MISSING)
            if remaining:
                if child is _MISSING:
                    raise _Incompatible
                visit(child, remaining, indices)
            else:
                observations.append(_Observation(location(indices), child))
        else:
            if type(node) is not list:
                raise _Incompatible
            shape.append((indices, len(node)))
            for index, child in enumerate(node):
                if remaining:
                    visit(child, remaining, indices + (index,))
                else:
                    observations.append(_Observation(location(indices + (index,)), child))

    if selector.target == "query":
        items = [item for item in work.entry["request"]["queryString"]
                 if item["name"] == selector.path[0].key]
        if len(items) > 1:
            raise _Incompatible
        observations.append(_Observation(location(()), items[0]["value"] if items else _MISSING))
    else:
        if selector.target == "request_json":
            if not work.has_request_json:
                raise _Incompatible
            data = work.request_json
        else:
            data = work.response_json
        visit(data, selector.path, ())
    return _Extracted(observations, tuple(shape), RecordCount(location=location(()), count=len(observations)))


def _identifier(value) -> tuple:
    if type(value) not in (int, str):
        raise _Incompatible
    return type(value), value


def _partition(keys, locations):
    classes, groups, signature = {}, [], []
    for key, location in zip(keys, locations, strict=True):
        if key not in classes:
            classes[key] = len(groups)
            groups.append([])
        group = classes[key]
        groups[group].append(location)
        signature.append(group)
    return tuple(signature), tuple(tuple(group) for group in groups)


def _duplicate(check: DuplicatePaginationCheck, extracted):
    keys, locations, types = [], [], []
    for page in check.pages:
        columns = [extracted[id(field)].observations for field in page.fields]
        indices = [observation.location.record for observation in columns[0]]
        if any([observation.location.record for observation in column] != indices for column in columns):
            raise _Incompatible
        for row in zip(*columns, strict=True):
            identity = tuple(_identifier(observation.value) for observation in row)
            keys.append(identity)
            types.append(tuple(value[0] for value in identity))
            first = row[0].location
            locations.append(Location(entry_id=first.entry_id, target=first.target,
                                      refs=tuple(observation.location.refs[0] for observation in row),
                                      record=first.record))
    partition, groups = _partition(keys, locations)
    failures = 0
    for group in groups:
        counts = list(Counter(location.entry_id for location in group).values())
        failures += sum(count * other for index, count in enumerate(counts) for other in counts[index + 1:])
    return Evidence(observations=len(keys), failures=failures, distinct_identities=len(groups),
                    equality_groups=tuple(group for group in groups if len(group) > 1)), (partition, tuple(types))


def _value_type(value):
    if value is _MISSING:
        return None
    if value is None:
        return "null"
    tags = {str: "string", int: "integer", bool: "boolean", float: "number", dict: "object", list: "array"}
    if type(value) not in tags or (type(value) is float and not math.isfinite(value)):
        raise _Incompatible
    return tags[type(value)]


def _missing(check: MissingNullFieldsCheck, extracted):
    fields = []
    for selector in check.fields:
        for observation in extracted[id(selector)].observations:
            state = "missing" if observation.value is _MISSING else "null" if observation.value is None else "present"
            fields.append(FieldState(location=observation.location, state=state, value_type=_value_type(observation.value)))
    if not fields:
        raise _Insufficient
    evidence = Evidence(observations=len(fields), failures=sum(field.state != "present" for field in fields), fields=tuple(fields))
    return evidence, (tuple(field.state for field in fields), tuple(field.value_type for field in fields))


def _references(check: InconsistentReferencesCheck, extracted):
    definitions = [obs for selector in check.definitions for obs in extracted[id(selector)].observations]
    references = [obs for selector in check.references for obs in extracted[id(selector)].observations]
    if not references:
        raise _Insufficient
    targets = {}
    for observation in definitions:
        targets.setdefault(_identifier(observation.value), []).append(observation.location)
    resolutions = []
    for observation in references:
        reached = tuple(targets.get(_identifier(observation.value), []))
        resolutions.append(ReferenceResolution(location=observation.location,
                                              state="resolved" if reached else "dangling", definitions=reached))
    all_observations = definitions + references
    keys = [_identifier(observation.value) for observation in all_observations]
    partition, groups = _partition(keys, [observation.location for observation in all_observations])
    evidence = Evidence(
        observations=len(all_observations), failures=sum(item.state == "dangling" for item in resolutions),
        distinct_identities=len(groups), equality_groups=tuple(group for group in groups if len(group) > 1),
        definition_locations=tuple(observation.location for observation in definitions), resolutions=tuple(resolutions),
    )
    return evidence, ((partition, tuple(resolutions)), tuple(key[0] for key in keys))


def _workspaces(transactions):
    if (isinstance(transactions, (str, bytes)) or not isinstance(transactions, Sequence)
            or not 1 <= len(transactions) <= 10):
        raise _Incompatible
    workspaces = {}
    origins = set()
    for tx in transactions:
        if (not isinstance(tx, HarTransaction) or type(tx.entry_id) is not int
                or tx.entry_id < 0 or tx.entry_id in workspaces):
            raise _Incompatible
        origin = _origin(tx.url, tx.entry_id)
        if origin is None or origin != tx.origin:
            raise _Incompatible
        workspaces[tx.entry_id] = _prepare(tx)
        origins.add(origin)
    if len(origins) != 1:
        raise _Incompatible
    return workspaces


def verify_failure(
    baseline: Sequence[HarTransaction], transformed: Sequence[HarTransaction], check: CheckConfig,
) -> VerificationResult:
    """Compare full local captures using an explicit, locally validated check."""
    check = validate_check(check)
    try:
        before, after = _workspaces(baseline), _workspaces(transformed)
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        return VerificationResult(check=check.type, outcome="inconclusive", reason="invalid_input")
    selectors = _selectors(check)
    if any(selector.entry_id not in before for selector in selectors):
        raise CheckConfigurationError("Unknown selected entry ID.")
    if set(before) != set(after):
        return VerificationResult(check=check.type, outcome="changed", reason="entry_set_changed")
    evidence, signatures, shapes, types = [], [], [], []
    calculator = _duplicate if isinstance(check, DuplicatePaginationCheck) else _missing if isinstance(check, MissingNullFieldsCheck) else _references
    try:
        for workspace in (before, after):
            extracted = {}
            for index, selector in enumerate(selectors):
                extracted[id(selector)] = _extract(workspace[selector.entry_id], selector, selector.ref or f"check_{index + 1:04d}")
            counts = tuple(extracted[id(selector)].count for selector in selectors)
            shape = tuple(extracted[id(selector)].shape for selector in selectors)
            result, signature = calculator(check, extracted)
            evidence.append(result.model_copy(update={"record_counts": counts}))
            signatures.append(signature[0])
            types.append(signature[1])
            shapes.append(shape)
    except _Insufficient:
        return VerificationResult(check=check.type, outcome="inconclusive", reason="insufficient_evidence",
                                  baseline=evidence[0] if evidence else None)
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        return VerificationResult(check=check.type, outcome="inconclusive", reason="incompatible_data",
                                  baseline=evidence[0] if evidence else None)
    old, new = evidence
    if old.failures == 0:
        outcome, reason = "not_present", "baseline_failure_absent"
    elif old.record_counts != new.record_counts:
        outcome, reason = "changed", "record_counts_changed"
    elif shapes[0] != shapes[1]:
        outcome, reason = "changed", "record_locations_changed"
    elif new.failures == 0:
        outcome, reason = "changed", "failure_removed"
    elif isinstance(check, MissingNullFieldsCheck) and signatures[0] != signatures[1]:
        outcome, reason = "changed", "field_states_changed"
    elif types[0] != types[1]:
        outcome, reason = "changed", "value_types_changed"
    elif signatures[0] != signatures[1]:
        outcome = "changed"
        reason = ("reference_resolution_changed" if isinstance(check, InconsistentReferencesCheck)
                  else "equality_relationships_changed")
    else:
        outcome, reason = "preserved", "failure_preserved"
    return VerificationResult(check=check.type, outcome=outcome, reason=reason, baseline=old, transformed=new)


def verify_duplicate_pagination(
    baseline: Sequence[HarTransaction], transformed: Sequence[HarTransaction],
    check: DuplicatePaginationCheck,
) -> VerificationResult:
    if not isinstance(validate_check(check), DuplicatePaginationCheck):
        raise CheckConfigurationError("Invalid duplicate-pagination configuration.")
    return verify_failure(baseline, transformed, check)


def verify_missing_null_fields(
    baseline: Sequence[HarTransaction], transformed: Sequence[HarTransaction],
    check: MissingNullFieldsCheck,
) -> VerificationResult:
    if not isinstance(validate_check(check), MissingNullFieldsCheck):
        raise CheckConfigurationError("Invalid missing/null configuration.")
    return verify_failure(baseline, transformed, check)


def verify_inconsistent_references(
    baseline: Sequence[HarTransaction], transformed: Sequence[HarTransaction],
    check: InconsistentReferencesCheck,
) -> VerificationResult:
    if not isinstance(validate_check(check), InconsistentReferencesCheck):
        raise CheckConfigurationError("Invalid reference configuration.")
    return verify_failure(baseline, transformed, check)
