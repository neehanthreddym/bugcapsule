"""Interactive Step 8 driver for the synthetic duplicate-pagination fixture only.

Parses/selects the fixture's two transactions, prepares the restricted
payload with explicitly configured approved labels, shows the exact preview,
requires affirmative terminal confirmation bound to that snapshot, makes at
most one model request, and prints the validated proposal with concise
usage/latency or a safe failure. Never applies transformations, exports, or
implies verification passed.
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent.client import AgentError, AgentIncompleteResponseError  # noqa: E402
from app.agent.workflow import (  # noqa: E402
    ApprovalError,
    PlanValidationError,
    approve_preview,
    request_capsule_plan,
)
from app.config import ConfigError  # noqa: E402
from app.engine.har import HarParseError, HarSelectionError, parse_har, select_transactions  # noqa: E402
from app.privacy import PrivacyError, prepare_model_payload  # noqa: E402


FIXTURE_PATH = Path(__file__).resolve().parents[2] / "fixtures" / "duplicate-pagination.har"
FAILURE_PATTERN = "duplicate_pagination"
APPROVED_LABELS = frozenset({
    "account_status",
    "company",
    "created_at",
    "customers",
    "email",
    "has_more",
    "id",
    "name",
    "page",
    "page_size",
    "pagination",
    "updated_at",
})
APPROVAL_KEYWORD = "APPROVE"


def _fail(message: str) -> int:
    print(f"Error: {message}")
    return 1


def main() -> int:
    if len(sys.argv) != 1:
        return _fail("This driver only runs the committed duplicate-pagination fixture.")
    print("BugCapsule Step 8 fixture planner (synthetic fixture only).")
    try:
        parsed = parse_har(FIXTURE_PATH)
        selected = select_transactions(parsed, [0, 1])
        prepared = prepare_model_payload(
            selected, FAILURE_PATTERN, approved_labels=APPROVED_LABELS
        )
    except (HarParseError, HarSelectionError, PrivacyError, ValueError) as error:
        return _fail(str(error))
    print(f"Fixture: {FIXTURE_PATH.name}")
    print(f"Failure pattern: {FAILURE_PATTERN}")
    print(f"Approved labels: {', '.join(sorted(APPROVED_LABELS))}")
    print("Exact payload preview to send:")
    print(prepared.preview_json)
    try:
        approval = approve_preview(prepared)
    except ApprovalError as error:
        return _fail(str(error))
    try:
        answer = input(
            f'Type {APPROVAL_KEYWORD} to send the exact preview above '
            "(one request, no retries): "
        ).strip()
    except (EOFError, KeyboardInterrupt):
        return _fail("Aborted; zero requests sent.")
    if answer != APPROVAL_KEYWORD:
        return _fail("Not approved; zero requests sent.")
    if approval.preview_json != prepared.preview_json:
        return _fail("Snapshot changed; renewed approval required. Zero requests sent.")
    start = time.perf_counter()
    try:
        proposal = request_capsule_plan(prepared, approval)
    except (ApprovalError, PlanValidationError, AgentError, ConfigError) as error:
        latency_ms = int((time.perf_counter() - start) * 1000)
        print(f"Safe failure after {latency_ms} ms: {error}")
        if isinstance(error, AgentIncompleteResponseError):
            print("Incomplete diagnostics: " + json.dumps(error.diagnostics.as_dict(), sort_keys=True))
        print("No transformations applied. No verification run. No export.")
        return 1
    except KeyboardInterrupt:
        return _fail("Interrupted.")
    latency_ms = int((time.perf_counter() - start) * 1000)
    safe_proposal = {
        "failure_pattern": proposal.failure_pattern,
        "replacements": [
            {"ref": item.ref, "kind": item.kind} for item in proposal.args.replacements
        ],
        "relationships": [
            {"refs": list(item.refs), "reason": item.reason}
            for item in proposal.args.relationships
        ],
        "check": {
            "type": proposal.args.check.type,
            "refs": list(proposal.args.check.refs),
            "note": proposal.args.check.note,
        },
        "needs_review": proposal.args.needs_review,
        "review_reason": proposal.args.review_reason,
    }
    print("Validated proposal for human review:")
    print(json.dumps(safe_proposal, ensure_ascii=True, indent=2))
    if proposal.replacement_plan is None:
        print("Local ReplacementPlan: none (needs review); validated nothing to execute.")
    else:
        print(
            f"Local ReplacementPlan validated: "
            f"{len(proposal.replacement_plan.rules)} rule(s); not executed."
        )
    if proposal.usage:
        usage = " ".join(f"{key}={proposal.usage[key]}" for key in sorted(proposal.usage))
    else:
        usage = "usage unavailable"
    print(f"Usage: {usage}; latency_ms={latency_ms}")
    print(
        "Structurally valid only; semantic correctness not verified. "
        "No transformations applied. No verification run. No export."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
