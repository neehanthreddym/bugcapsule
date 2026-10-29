"""Approval-bound planning workflow over the restricted payload boundary.

The workflow binds human approval to the exact reviewed preview snapshot,
sends that snapshot once through the bounded client, validates the single
tool call with Pydantic, resolves references locally via PrivateBindings,
and converts accepted proposals into the existing ReplacementPlan format for
local validation only. Resolved selectors and bindings stay local. A
structurally valid proposal is returned for human review; it is not proof of
semantic correctness, and this module never executes replacements, mutates
inputs, or fabricates a successful plan.
"""

from dataclasses import dataclass
import json
from typing import Any

from pydantic import ValidationError

from app.agent.client import AgentError, ToolCallResult, Transport, request_tool_call
from app.agent.schemas import ProposeCapsulePlanArgs
from app.config import get_meta_api_key
from app.engine.replace import ReplacementError, ReplacementPlan, validate_plan
from app.privacy import PreparedPayload, PrivacyError


_PATTERNS = frozenset({
    "duplicate_pagination", "missing_null_fields", "inconsistent_identifier_references",
})
_SCALAR_TYPES = frozenset({"string", "integer", "null", "boolean"})


class ApprovalError(ValueError):
    """Approval is missing or no longer matches the exact reviewed snapshot."""


class PlanValidationError(ValueError):
    """Tool arguments, references, types, or checks failed local validation."""


@dataclass(frozen=True)
class Approval:
    """An exact reviewed snapshot; changes require a renewed approval."""

    preview_json: str


@dataclass(frozen=True)
class ValidatedProposal:
    """Safe refs-based proposal plus a locally validated plan for human review."""

    args: ProposeCapsulePlanArgs
    replacement_plan: ReplacementPlan | None
    failure_pattern: str
    usage: dict[str, int]


def approve_preview(prepared: PreparedPayload) -> Approval:
    """Capture the exact preview snapshot behind a later approval check."""
    if not isinstance(prepared, PreparedPayload):
        raise ApprovalError("Invalid prepared payload.")
    preview = prepared.preview_json
    if type(preview) is not str or not preview:
        raise ApprovalError("Invalid prepared payload.")
    return Approval(preview_json=preview)


def _collect_nodes(payload: dict[str, Any]) -> tuple[str, dict[str, dict[str, Any]]]:
    failure_pattern = payload.get("failure_pattern")
    transactions = payload.get("transactions")
    if failure_pattern not in _PATTERNS or not isinstance(transactions, list):
        raise PlanValidationError("Invalid reviewed snapshot.")
    nodes: dict[str, dict[str, Any]] = {}

    def visit(node: Any) -> None:
        if not isinstance(node, dict):
            raise PlanValidationError("Invalid reviewed snapshot.")
        ref = node.get("ref")
        types = node.get("types")
        if type(ref) is not str or not ref or ref in nodes:
            raise PlanValidationError("Invalid reviewed snapshot.")
        if not isinstance(types, list) or not types:
            raise PlanValidationError("Invalid reviewed snapshot.")
        for entry in types:
            if not isinstance(entry, dict):
                raise PlanValidationError("Invalid reviewed snapshot.")
            if type(entry.get("type")) is not str or type(entry.get("count")) is not int:
                raise PlanValidationError("Invalid reviewed snapshot.")
        nodes[ref] = node
        fields = node.get("fields", [])
        if not isinstance(fields, list):
            raise PlanValidationError("Invalid reviewed snapshot.")
        for field in fields:
            if not isinstance(field, dict) or "node" not in field:
                raise PlanValidationError("Invalid reviewed snapshot.")
            visit(field["node"])
        if "items" in node:
            visit(node["items"])

    for entry in transactions:
        if not isinstance(entry, dict):
            raise PlanValidationError("Invalid reviewed snapshot.")
        for target in ("query", "request_json", "response_json"):
            if target in entry:
                visit(entry[target])
    return failure_pattern, nodes


def _check_replacement(
    prepared: PreparedPayload, nodes: dict[str, dict[str, Any]], ref: str, kind: str
) -> None:
    node = nodes.get(ref)
    if node is None:
        raise PlanValidationError("Unknown reference.")
    try:
        binding = prepared.bindings.resolve(ref)
    except PrivacyError:
        raise PlanValidationError("Unknown reference.") from None
    if not binding.path:
        raise PlanValidationError("Unsupported replacement target.")
    if binding.target == "query" and len(binding.path) != 1:
        raise PlanValidationError("Unsupported replacement target.")
    observed = {entry.get("type") for entry in node["types"]}
    if not observed or not observed <= _SCALAR_TYPES:
        raise PlanValidationError("Unsupported replacement type.")
    if "integer" in observed and kind != "id":
        raise PlanValidationError("Unsupported replacement type.")


def request_capsule_plan(
    prepared: PreparedPayload,
    approval: Approval | None,
    *,
    api_key: str | None = None,
    transport: Transport | None = None,
) -> ValidatedProposal:
    """Request one plan for the approved snapshot; never fabricate success."""
    if not isinstance(prepared, PreparedPayload):
        raise ApprovalError("Invalid prepared payload.")
    if not isinstance(approval, Approval):
        raise ApprovalError("Missing approval.")
    if approval.preview_json != prepared.preview_json:
        raise ApprovalError("Snapshot changed; renewed approval required.")
    try:
        payload = json.loads(prepared.preview_json)
    except (ValueError, RecursionError):
        raise PlanValidationError("Invalid reviewed snapshot.") from None
    if not isinstance(payload, dict):
        raise PlanValidationError("Invalid reviewed snapshot.")
    failure_pattern, nodes = _collect_nodes(payload)
    key = api_key if api_key is not None else get_meta_api_key()
    try:
        result: ToolCallResult = request_tool_call(
            prepared.preview_json, api_key=key, transport=transport
        )
    except AgentError:
        raise
    except (ValueError, TypeError, RecursionError):
        raise PlanValidationError("Invalid tool arguments.") from None
    try:
        raw_args = json.loads(result.arguments)
    except (ValueError, RecursionError):
        raise PlanValidationError("Invalid tool arguments.") from None
    if not isinstance(raw_args, dict):
        raise PlanValidationError("Invalid tool arguments.")
    try:
        args = ProposeCapsulePlanArgs.model_validate(raw_args)
    except (ValidationError, ValueError, TypeError, RecursionError):
        raise PlanValidationError("Invalid tool arguments.") from None
    for ref in [item.ref for item in args.replacements]:
        if ref not in nodes:
            raise PlanValidationError("Unknown reference.")
    for relationship in args.relationships:
        for ref in relationship.refs:
            if ref not in nodes:
                raise PlanValidationError("Unknown reference.")
    for ref in args.check.refs:
        if ref not in nodes:
            raise PlanValidationError("Unknown reference.")
    if args.check.type != failure_pattern:
        raise PlanValidationError("Check inconsistent with failure pattern.")
    for item in args.replacements:
        _check_replacement(prepared, nodes, item.ref, item.kind)
    plan: ReplacementPlan | None = None
    if args.replacements:
        grouped: dict[tuple, dict[str, Any]] = {}
        order: list[tuple] = []
        for item in args.replacements:
            binding = prepared.bindings.resolve(item.ref)
            path_key = tuple(
                ("key", segment.key) if hasattr(segment, "key") else ("each", "*")
                for segment in binding.path
            )
            group_key = (binding.target, path_key, item.kind)
            if group_key not in grouped:
                grouped[group_key] = {
                    "entry_ids": [],
                    "target": binding.target,
                    "path": [segment.model_dump() for segment in binding.path],
                    "kind": item.kind,
                }
                order.append(group_key)
            if binding.entry_id not in grouped[group_key]["entry_ids"]:
                grouped[group_key]["entry_ids"].append(binding.entry_id)
        try:
            plan = validate_plan({"rules": [grouped[key] for key in order]})
        except (ReplacementError, ValueError, TypeError, RecursionError):
            raise PlanValidationError("Invalid replacement plan.") from None
    return ValidatedProposal(
        args=args,
        replacement_plan=plan,
        failure_pattern=failure_pattern,
        usage=dict(result.usage),
    )
