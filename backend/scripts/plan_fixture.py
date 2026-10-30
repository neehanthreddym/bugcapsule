"""Interactive Step 8 driver for the synthetic duplicate-pagination fixture only.

Parses/selects the fixture's two transactions, prepares the restricted
payload with explicitly configured approved labels, shows the exact preview,
requires affirmative terminal confirmation bound to that snapshot, makes at
most one model request, and prints the validated proposal with concise
usage/latency or a safe failure. Default mode never applies transformations,
exports, or implies verification passed. Optional --execute adds local plan approval
and bounded verification/revision. --execute --export adds separate exact-file
review and export approval; only that mode can write a local capsule ZIP.
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent.client import AgentError, AgentIncompleteResponseError  # noqa: E402
from app.agent.execution import (  # noqa: E402
    MAX_MODEL_REQUESTS, ExecutionError, ExecutionSession, approve_execution,
)
from app.agent.workflow import (  # noqa: E402
    ApprovalError,
    PlanValidationError,
    approve_preview,
    request_capsule_plan,
)
from app.config import ConfigError  # noqa: E402
from app.engine.har import HarParseError, HarSelectionError, parse_har, select_transactions  # noqa: E402
from app.privacy import PrivacyError, prepare_model_payload  # noqa: E402
from app.export import ExportError, approve_export, prepare_capsule, write_capsule  # noqa: E402


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
CAPSULE_DIR = Path(__file__).resolve().parents[2] / "local" / "capsules"


def _fail(message: str) -> int:
    print(f"Error: {message}")
    return 1


def _confirm(keyword: str, description: str) -> bool:
    try:
        return input(f"Type {keyword} to {description}: ").strip() == keyword
    except (EOFError, KeyboardInterrupt):
        return False


def _execution_evidence(result) -> None:
    safe = {"status": result.status, "reason": result.reason, "attempts": result.attempts}
    if result.verification is not None:
        verification = result.verification
        safe["verification"] = {"outcome": verification.outcome, "reason": verification.reason}
        for name, evidence in (("baseline", verification.baseline), ("candidate", verification.transformed)):
            if evidence is not None:
                safe["verification"][name] = {
                    "observations": evidence.observations, "failures": evidence.failures,
                    "distinct_identities": evidence.distinct_identities,
                }
    print(json.dumps(safe, ensure_ascii=True, indent=2))
    print("Verification covers only the configured failure. Candidate remains private; no export or file writes.")


def _export_fixture(result) -> int:
    try:
        preview = prepare_capsule(result)
        print("Exact capsule files (JSON filename-to-content map):")
        print(preview.preview_json)
        print(f"Capsule SHA-256: {preview.digest}")
        print("Review all contents, including routing and untargeted values. No anonymization guarantee.")
        if not _confirm("EXPORT", "write exactly these four files as a local ZIP"):
            print("Export cancelled; no files written.")
            return 1
        path = write_capsule(preview, approve_export(preview), CAPSULE_DIR / f"capsule-{preview.digest}.zip")
        print(f"Approved capsule written: {path}")
        return 0
    except ExportError as error:
        return _fail(str(error))


def _execute_fixture(*, export=False) -> int:
    print("BugCapsule approved fixture execution (at most two model requests).")
    try:
        selected = select_transactions(parse_har(FIXTURE_PATH), [0, 1])
        session = ExecutionSession(selected, FAILURE_PATTERN, approved_labels=APPROVED_LABELS)
    except (HarParseError, HarSelectionError, PrivacyError, ValueError) as error:
        return _fail(str(error))
    for _ in range(MAX_MODEL_REQUESTS):
        print("Exact payload preview to send:")
        print(session.prepared.preview_json)
        if not _confirm(APPROVAL_KEYWORD, "send this exact snapshot (one request, no retries)"):
            _execution_evidence(session.cancel())
            return 1
        try:
            session.request_proposal(approve_preview(session.prepared))
            if session.result is not None and session.result.status == "review_required":
                _execution_evidence(session.result)
                return 1
            review = session.review_plan()
            print("Resolved replacement plan and selected check (local only):")
            print(review.preview_json)
            if not _confirm("EXECUTE", "apply this exact plan and check locally"):
                _execution_evidence(session.cancel())
                return 1
            result = session.execute(approve_execution(review))
            _execution_evidence(result)
            if result.status == "revision_available":
                session.prepare_revision()
                continue
            if result.status == "preserved" and export:
                return _export_fixture(result)
            return 0 if result.status == "preserved" else 1
        except (ApprovalError, PlanValidationError, AgentError, ConfigError, ExecutionError) as error:
            print(f"Safe failure: {error}")
            if isinstance(error, AgentIncompleteResponseError):
                print("Incomplete diagnostics: " + json.dumps(error.diagnostics.as_dict(), sort_keys=True))
            if session.result is not None:
                _execution_evidence(session.result)
            else:
                _execution_evidence(session.cancel())
            return 1
        except KeyboardInterrupt:
            _execution_evidence(session.cancel())
            return 1
    return _fail("Request budget exhausted.")


def main() -> int:
    args = sys.argv[1:]
    if args and len(args) == len(set(args)) and set(args) <= {"--execute", "--export"}:
        if "--execute" not in args:
            return _fail("--export requires --execute.")
        return _execute_fixture(export="--export" in args)
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
