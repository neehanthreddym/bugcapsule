"""Explicit local review DTOs and fixed errors around the existing agent workflow.

Selectors are shown only for local human review, never sent back as model input.
No transactions, original scalar values, maps or binding objects are serialized.
Check resolution validates configuration only; it never evaluates a failure.
"""

import re

from app.agent.client import (
    AgentHttpError, AgentIncompleteResponseError, AgentResponseError, AgentTransportError,
)
from app.agent.workflow import PlanValidationError, ValidatedProposal
from app.api.schemas import (
    IncompleteDiagnostics, PlanningDiagnostics, ProposalReview, ResolvedReplacement,
    ReviewCheck, ReviewLocation, ReviewRelationship, ReviewReplacement,
)
from app.engine.replace import ArrayItems, ObjectKey
from app.privacy import PreparedPayload
from app.verify.checks import CheckConfigurationError, check_from_proposal
from app.verify.schemas import DuplicatePaginationCheck


def _path(segments):
    return [ObjectKey(key=segment.key) if isinstance(segment, ObjectKey) else ArrayItems(each="*")
            for segment in segments]


def proposal_review(proposal: ValidatedProposal, prepared: PreparedPayload, *, proposal_id: str,
                    preview_id: str, sha256: str, diagnostics: PlanningDiagnostics) -> ProposalReview:
    """Allowlist validated proposal fields and resolved selectors, not source values."""
    check = proposal.args.check
    locations = []
    for ref in check.refs:
        binding = prepared.bindings.resolve(ref)
        locations.append(ReviewLocation(ref=ref, entry_id=binding.entry_id, target=binding.target,
                                        path=_path(binding.path)))
    groups = []
    if check.type == "inconsistent_identifier_references":
        # The tool's ref list does not establish identifier roles. Never infer them.
        resolution = "definition_reference_roles_required"
    else:
        try:
            configuration = check_from_proposal(check, prepared.bindings)
            resolution = "resolved"
            if isinstance(configuration, DuplicatePaginationCheck):
                groups = [[field.ref for field in page.fields] for page in configuration.pages]
        except CheckConfigurationError:
            resolution = "invalid_configuration"
    rules = proposal.replacement_plan.rules if proposal.replacement_plan is not None else []
    usage = {key: value for key in ("input_tokens", "output_tokens", "total_tokens")
             if type(value := proposal.usage.get(key)) is int and 0 <= value <= 10_000_000}
    return ProposalReview(
        proposal_id=proposal_id, preview_id=preview_id, sha256=sha256,
        replacements=[ReviewReplacement(ref=item.ref, kind=item.kind) for item in proposal.args.replacements],
        relationships=[ReviewRelationship(refs=list(item.refs), reason=item.reason) for item in proposal.args.relationships],
        check=ReviewCheck(type=check.type, refs=list(check.refs), note=check.note, resolution=resolution,
                          locations=locations, identity_groups=groups),
        needs_review=proposal.args.needs_review, review_reason=proposal.args.review_reason,
        resolved_replacements=[ResolvedReplacement(entry_ids=list(rule.entry_ids), target=rule.target,
                                                    path=_path(rule.path), kind=rule.kind) for rule in rules],
        usage=usage, diagnostics=diagnostics,
    )


def planning_failure(error: Exception, diagnostics: PlanningDiagnostics):
    """Return fixed codes plus allowlisted diagnostics, never arbitrary error text."""
    if isinstance(error, AgentIncompleteResponseError):
        diagnostics.incomplete = IncompleteDiagnostics(**error.diagnostics.as_dict())
        return "planning_incomplete"
    if isinstance(error, AgentHttpError):
        match = re.fullmatch(r"Meta request failed \(HTTP ([1-5][0-9]{2})\)\.", str(error))
        if match:
            diagnostics.http_status = int(match[1])
        return "planning_http_error"
    if isinstance(error, AgentTransportError):
        return "planning_timeout" if str(error) == "Request timed out." else "planning_transport_error"
    if isinstance(error, PlanValidationError):
        return "planning_invalid_proposal"
    if isinstance(error, AgentResponseError):
        return "planning_response_error"
    return "planning_failed"
