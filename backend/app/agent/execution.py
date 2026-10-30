"""Human-approved, local execution with at most two attempted model requests.

This is one small session, not a retry mechanism. Only a changed deterministic
verification permits one inspectable revision. Protocol/validation errors stop.
Each plan/check/role review has its own single-use execution approval; request
approval remains bound to the complete initial or revision snapshot. Candidates
always restart from independent scrubbed copies of the original baseline.
Preserved means only the configured failure survived, never safe to publish.
No exporting, file writes, or automatic approvals occur here.
"""

from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass, field
import json
import hashlib
import hmac
import secrets
from typing import Literal

from app.agent.client import Transport
from app.agent.workflow import (
    Approval, ApprovalError, ValidatedProposal, request_capsule_plan,
)
from app.engine.replace import ReplacementPlan, apply_replacements, validate_plan
from app.engine.scrub import scrub_transactions
from app.privacy import (
    FailurePattern, PreparedPayload, prepare_model_payload, prepare_revision_payload,
)
from app.schemas import HarTransaction
from app.verify.checks import check_from_proposal, validate_check, verify_failure
from app.verify.schemas import CheckConfig, VerificationResult


MAX_MODEL_REQUESTS = 2
_RESULT_KEY = secrets.token_bytes(32)


class ExecutionError(ValueError):
    """A fixed, value-free session error; never wraps provider prose."""


def _json(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False)


class PlanReview:
    """Private local plan/check preview; copies prevent post-approval mutation."""

    __slots__ = ("_preview", "_token", "_roles")

    def __init__(self, plan: ReplacementPlan, check: CheckConfig, roles: tuple[str, ...], attempt: int):
        self._preview = _json({"attempt": attempt, "replacement_plan": plan.model_dump(),
                               "check": check.model_dump(), "definition_refs": list(roles)})
        self._token = object()
        self._roles = roles

    @property
    def preview_json(self) -> str:
        return self._preview

    @property
    def plan(self) -> ReplacementPlan:
        return validate_plan(json.loads(self._preview)["replacement_plan"])

    @property
    def check(self) -> CheckConfig:
        return validate_check(json.loads(self._preview)["check"])

    def __repr__(self) -> str:
        return "PlanReview()"


@dataclass(frozen=True, repr=False)
class ExecutionApproval:
    _token: object
    _preview: str


def approve_execution(review: PlanReview) -> ExecutionApproval:
    """Call only after the human approves this exact local plan/check preview."""
    if not isinstance(review, PlanReview):
        raise ApprovalError("Invalid execution review.")
    return ExecutionApproval(review._token, review.preview_json)


@dataclass(frozen=True)
class ExecutionResult:
    status: Literal["preserved", "revision_available", "review_required", "stopped", "cancelled"]
    reason: str
    attempts: int
    verification: VerificationResult | None = None
    candidate: tuple[HarTransaction, ...] | None = field(default=None, repr=False)
    _seal: bytes | None = field(default=None, repr=False, compare=False)


def _result_signature(result: ExecutionResult) -> bytes:
    # Explicitly include the retained entries excluded from normal model dumps.
    snapshot = {"status": result.status, "reason": result.reason, "attempts": result.attempts,
                "verification": result.verification.model_dump(mode="json"),
                "candidate": [{**tx.model_dump(), "original_entry": tx.original_entry}
                              for tx in result.candidate]}
    return hmac.digest(_RESULT_KEY, _json(snapshot).encode("utf-8"), hashlib.sha256)


def verified_export_result(result: ExecutionResult) -> ExecutionResult:
    """Return an independent, intact session-issued success or fail value-free.

    The process-local seal binds the complete candidate and report at successful
    execution. This is an in-process integrity boundary, not persistent signing.
    Results from an earlier process cannot be imported or trusted as receipts.
    """
    try:
        result = deepcopy(result)
        if (not isinstance(result, ExecutionResult) or result.status != "preserved"
                or result.reason != "configured_failure_preserved"
                or result.attempts not in (1, 2) or not result.candidate
                or result.verification is None or result.verification.outcome != "preserved"
                or result.verification.reason != "failure_preserved"
                or not isinstance(result._seal, bytes)
                or not hmac.compare_digest(result._seal, _result_signature(result))):
            raise ValueError
        return result
    except (ValueError, TypeError, AttributeError, RecursionError):
        raise ExecutionError("Missing, stale, or mismatched verified execution result.") from None


class ExecutionSession:
    """Explicit request -> local review -> execution -> optional revision steps.

    Attempt counts increment before invoking the request workflow, including
    failures. Missing/stale approval consumes no attempt and stops the session.
    Use result after a terminal error; errors themselves retain existing safe
    client categories/diagnostics. No hidden retries or transformations occur.
    """

    def __init__(self, transactions: Sequence[HarTransaction], failure_pattern: FailurePattern, *,
                 approved_labels: frozenset[str] = frozenset()):
        # Prepare first to enforce selection bounds and scrub inside the boundary.
        self._initial = prepare_model_payload(transactions, failure_pattern, approved_labels=approved_labels)
        self._baseline = tuple(scrub_transactions(transactions).transactions)
        self._labels = frozenset(approved_labels)
        self._prepared = self._initial
        self._attempts = 0
        self._phase = "request"
        self._proposal = None
        self._review = None
        self._frozen_check = None
        self._frozen_roles = ()
        self._frozen_refs = ()
        self._result = None

    @property
    def prepared(self) -> PreparedPayload:
        return self._prepared

    @property
    def attempts(self) -> int:
        return self._attempts

    @property
    def result(self) -> ExecutionResult | None:
        return deepcopy(self._result)

    def _stop(self, status, reason, verification=None, candidate=None):
        self._phase = "stopped"
        self._result = ExecutionResult(status, reason, self._attempts, verification, candidate)
        if status == "preserved":
            object.__setattr__(self._result, "_seal", _result_signature(self._result))
        return self.result

    def cancel(self) -> ExecutionResult:
        if self._phase == "stopped":
            return self.result
        return self._stop("cancelled", "cancelled_by_user")

    def request_proposal(self, approval: Approval | None, *, api_key: str | None = None,
                         transport: Transport | None = None) -> ValidatedProposal:
        if self._phase != "request" or self._attempts >= MAX_MODEL_REQUESTS:
            raise ExecutionError("No further model request is permitted.")
        if not isinstance(approval, Approval) or approval.preview_json != self._prepared.preview_json:
            self._stop("stopped", "request_approval_rejected")
            raise ApprovalError("Missing or stale payload approval.")
        self._attempts += 1
        try:
            proposal = request_capsule_plan(self._prepared, approval, api_key=api_key, transport=transport)
        except KeyboardInterrupt:
            self._stop("cancelled", "cancelled_by_user")
            raise
        except (ValueError, TypeError, RecursionError):
            self._stop("stopped", "request_failed")
            raise
        self._proposal = deepcopy(proposal)
        if proposal.args.needs_review or proposal.replacement_plan is None:
            self._stop("review_required", "model_requires_review")
        else:
            self._phase = "proposal"
        return deepcopy(proposal)

    def review_plan(self, *, definition_refs: Sequence[str] | None = None) -> PlanReview:
        if self._phase != "proposal":
            raise ExecutionError("No executable proposal is pending.")
        try:
            if self._frozen_check is not None:
                if tuple(self._proposal.args.check.refs) != self._frozen_refs:
                    raise ExecutionError("The approved check cannot change.")
                if definition_refs is not None and tuple(definition_refs) != self._frozen_roles:
                    raise ExecutionError("The approved check cannot change.")
                roles = self._frozen_roles
            else:
                roles = tuple(definition_refs) if definition_refs is not None else ()
            check = check_from_proposal(self._proposal.args.check, self._initial.bindings,
                                        definition_refs=roles or None)
            check_json = _json(check.model_dump())
            if self._frozen_check is not None and check_json != self._frozen_check:
                raise ExecutionError("The approved check cannot change.")
            self._review = PlanReview(self._proposal.replacement_plan, check, roles, self._attempts)
            self._phase = "execution_review"
            return self._review
        except (ValueError, TypeError, RecursionError):
            self._stop("review_required", "invalid_or_changed_check")
            raise ExecutionError("Invalid or changed check configuration.") from None

    def execute(self, approval: ExecutionApproval | None) -> ExecutionResult:
        if self._phase != "execution_review":
            raise ExecutionError("No reviewed execution is pending.")
        if (not isinstance(approval, ExecutionApproval) or approval._token is not self._review._token
                or approval._preview != self._review.preview_json):
            self._stop("stopped", "execution_approval_rejected")
            raise ApprovalError("Missing or stale execution approval.")
        review = self._review
        if self._frozen_check is not None and _json(review.check.model_dump()) != self._frozen_check:
            self._stop("stopped", "invalid_or_changed_check")
            raise ExecutionError("The approved check cannot change.")
        if self._frozen_check is None:
            self._frozen_check = _json(review.check.model_dump())
            self._frozen_roles = review._roles
            self._frozen_refs = tuple(self._proposal.args.check.refs)
        check = validate_check(json.loads(self._frozen_check))
        baseline_result = verify_failure(self._baseline, self._baseline, check)
        if baseline_result.outcome != "preserved":
            return self._stop("review_required", "baseline_failure_not_confirmed", baseline_result)
        try:
            fresh = scrub_transactions(self._baseline).transactions
            candidate = apply_replacements(fresh, review.plan).transactions
            verification = verify_failure(self._baseline, candidate, check)
        except KeyboardInterrupt:
            return self.cancel()
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
            return self._stop("stopped", "execution_failed")
        if verification.outcome == "preserved":
            return self._stop("preserved", "configured_failure_preserved", verification, tuple(candidate))
        if verification.outcome == "changed" and self._attempts == 1:
            self._phase = "revision"
            self._result = ExecutionResult("revision_available", "candidate_changed", self._attempts,
                                           verification, tuple(candidate))
            return self.result
        status = "review_required" if verification.outcome in {"not_present", "inconclusive"} else "stopped"
        return self._stop(status, "verification_not_preserved", verification, tuple(candidate))

    def prepare_revision(self) -> PreparedPayload:
        if (self._phase != "revision" or self._attempts != 1
                or self._result.verification.outcome != "changed"):
            raise ExecutionError("No revision is permitted.")
        try:
            self._prepared = prepare_revision_payload(
                self._baseline, self._initial, self._result.verification,
                approved_labels=self._labels,
                prior_replacements=[(item.ref, item.kind) for item in self._proposal.args.replacements],
                check_refs=self._frozen_refs, definition_refs=self._frozen_roles,
            )
        except ValueError:
            self._stop("stopped", "revision_preparation_failed")
            raise ExecutionError("Cannot prepare restricted revision.") from None
        self._phase = "request"
        return self._prepared
