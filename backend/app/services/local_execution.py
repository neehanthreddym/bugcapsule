"""Value-free DTO construction from actual local execution and verifier evidence.

Never serialize ExecutionResult, candidate transactions, selectors or private maps.
This adapter only copies the verifier's fixed codes, counts and safe locations.
"""

from app.agent.execution import ExecutionResult
from app.api.schemas import ExecutionReport
from app.verify.schemas import (
    Evidence, FieldState, Location, RecordCount, ReferenceResolution, VerificationResult,
)


LIMITATIONS = (
    "Preserved means only the configured failure was preserved; it is not a privacy guarantee.",
    "Untargeted business data may remain private. Complete application compatibility is not established.",
    "No additional AI request, automatic revision, export or download occurred during local execution.",
)


def _location(value):
    return Location(entry_id=value.entry_id, target=value.target, refs=tuple(value.refs), record=tuple(value.record))


def _evidence(value):
    if value is None:
        return None
    return Evidence(
        observations=value.observations, failures=value.failures, distinct_identities=value.distinct_identities,
        record_counts=tuple(RecordCount(location=_location(item.location), count=item.count) for item in value.record_counts),
        equality_groups=tuple(tuple(_location(loc) for loc in group) for group in value.equality_groups),
        fields=tuple(FieldState(location=_location(item.location), state=item.state, value_type=item.value_type) for item in value.fields),
        definition_locations=tuple(_location(loc) for loc in value.definition_locations),
        resolutions=tuple(ReferenceResolution(location=_location(item.location), state=item.state,
                                              definitions=tuple(_location(loc) for loc in item.definitions)) for item in value.resolutions),
    )


def execution_report(result: ExecutionResult, *, execution_id: str, proposal_id: str,
                     review_id: str, review_sha256: str) -> ExecutionReport:
    verification = result.verification
    safe_verification = None if verification is None else VerificationResult(
        check=verification.check, outcome=verification.outcome, reason=verification.reason,
        baseline=_evidence(verification.baseline), transformed=_evidence(verification.transformed),
    )
    return ExecutionReport(
        execution_id=execution_id, proposal_id=proposal_id, review_id=review_id, review_sha256=review_sha256,
        attempt=result.attempts, status=result.status, candidate_created=result.candidate is not None,
        reason=result.reason, verification=safe_verification,
        limitations=list(LIMITATIONS),
    )
