"""Offline approved execution; explicitly injected faults are test-only defects."""

from copy import deepcopy
import http.client
import importlib.util
import json
from pathlib import Path
import socket
import sys
import urllib.request

import pytest

from app.agent import client, execution
from app.agent.client import AgentError, AgentIncompleteResponseError
from app.agent.execution import ExecutionError, ExecutionSession, approve_execution
from app.agent.workflow import Approval, ApprovalError, approve_preview
from app.engine.har import parse_har, select_transactions
from app.engine.replace import apply_replacements as real_apply
from app.engine.scrub import scrub_transactions
from app.privacy import PrivacyError, prepare_revision_payload


FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "duplicate-pagination.har"
FAKE_KEY = "BUGCAPSULE_TEST_KEY_NOT_REAL"
PRIVATE = "FABRICATED_MODEL_PROSE_NEVER_SEND_927414"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Live network calls are forbidden.")
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(http.client, "HTTPSConnection", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)


def source():
    return select_transactions(parse_har(FIXTURE), [0, 1])


def snapshots(transactions):
    return [(deepcopy(tx.model_dump()), deepcopy(tx.original_entry)) for tx in transactions]


def node_ref(prepared, entry_id, keys):
    def visit(node):
        binding = prepared.bindings.resolve(node["ref"])
        path = [segment.key if hasattr(segment, "key") else "*" for segment in binding.path]
        if binding.entry_id == entry_id and path == keys:
            return node["ref"]
        for field in node.get("fields", []):
            found = visit(field["node"])
            if found:
                return found
        if "items" in node:
            return visit(node["items"])
        return None
    for entry in prepared.payload["transactions"]:
        if entry["entry_id"] == entry_id:
            return visit(entry["response_json"])
    raise AssertionError("Expected local fixture node not found.")


def args_for(prepared, *, fields=("id",), check_fields=("id",), pattern="duplicate_pagination"):
    refs = [node_ref(prepared, entry, ["customers", "*", field])
            for entry in (0, 1) for field in check_fields]
    return {
        "replacements": [
            {"ref": node_ref(prepared, entry, ["customers", "*", field]),
             "kind": {"id": "id", "name": "name", "email": "email", "company": "text"}[field]}
            for entry in (0, 1) for field in fields
        ],
        "relationships": [{"refs": refs, "reason": PRIVATE}],
        "check": {"type": pattern, "refs": refs, "note": PRIVATE},
        "needs_review": False, "review_reason": PRIVATE,
    }


def response(arguments):
    return {"status": "completed", "output": [{"type": "function_call",
            "name": "propose_capsule_plan", "arguments": json.dumps(arguments), "call_id": "test_call"}]}


def transport_for(arguments_list, calls):
    responses = iter(arguments_list)

    def transport(body):
        calls.append(deepcopy(body))
        value = next(responses)
        if isinstance(value, tuple):
            status, payload = value
        else:
            status, payload = 200, response(value)
        return status, json.dumps(payload).encode()
    return transport


def request(session, arguments, calls):
    return session.request_proposal(approve_preview(session.prepared), api_key=FAKE_KEY,
                                    transport=transport_for([arguments], calls))


def execute_approved(session):
    review = session.review_plan()
    return session.execute(approve_execution(review))


def coherent_body(tx):
    """Test helper only: keep an intentionally faulted candidate internally coherent."""
    response = tx.original_entry["response"]
    text = json.dumps(tx.response_json, ensure_ascii=False)
    response["content"]["text"] = text
    response["content"]["size"] = response["bodySize"] = len(text.encode("utf-8"))
    for header in response["headers"]:
        if header["name"].lower() == "content-length":
            header["value"] = str(response["bodySize"])


def inject_engine_fault(monkeypatch, *, always=False, incoherent=False):
    """Explicit fault injection, NOT an assertion that the real engine has this bug."""
    seen = []

    def faulty_engine(transactions, plan):
        seen.append(snapshots(transactions))
        result = real_apply(transactions, plan)
        if always or len(seen) == 1:
            # Deliberately destroy the selected duplicate in the test candidate.
            result.transactions[1].response_json["customers"][0]["id"] = 77_777_777
            if not incoherent:
                coherent_body(result.transactions[1])
        return result

    monkeypatch.setattr(execution, "apply_replacements", faulty_engine)
    return seen


def test_real_fixture_approved_execution_preserves_failure_without_mutation():
    original = source()
    before = snapshots(original)
    file_before = FIXTURE.read_bytes()
    session = ExecutionSession(original, "duplicate_pagination")
    calls = []
    request(session, args_for(session.prepared, fields=("id", "name", "email", "company")), calls)
    review = session.review_plan()
    assert "replacement_plan" in review.preview_json and "customers" in review.preview_json
    assert "check" in review.preview_json
    # Mutating a returned copy cannot change the pending executable plan.
    review.plan.rules[0].entry_ids.clear()
    result = session.execute(approve_execution(review))
    assert result.status == "preserved" and result.verification.outcome == "preserved"
    assert result.attempts == len(calls) == 1
    assert result.verification.baseline.failures == result.verification.transformed.failures == 1
    first, second = [tx.response_json["customers"] for tx in result.candidate]
    assert first[2] == second[0] and first[0]["id"] != 101
    assert snapshots(original) == before and FIXTURE.read_bytes() == file_before
    with pytest.raises(ExecutionError):
        session.request_proposal(approve_preview(session.prepared), api_key=FAKE_KEY)
    assert session.attempts == 1


@pytest.mark.parametrize("approval", [None, Approval("stale")])
def test_missing_or_stale_send_approval_has_zero_requests_or_transformations(monkeypatch, approval):
    session = ExecutionSession(source(), "duplicate_pagination")
    calls = []
    with pytest.raises(ApprovalError):
        session.request_proposal(approval, api_key=FAKE_KEY,
                                 transport=transport_for([], calls))
    assert session.attempts == 0 and calls == [] and session.result.candidate is None
    assert session.result.reason == "request_approval_rejected"


@pytest.mark.parametrize("approval", [None, Approval("network approval is not execution approval")])
def test_missing_execution_approval_does_not_transform(monkeypatch, approval):
    session = ExecutionSession(source(), "duplicate_pagination")
    request(session, args_for(session.prepared), [])
    session.review_plan()

    def forbidden(*args, **kwargs):
        raise AssertionError("Transformation without human approval.")
    monkeypatch.setattr(execution, "apply_replacements", forbidden)
    with pytest.raises(ApprovalError):
        session.execute(approval)
    assert session.result.reason == "execution_approval_rejected"


def test_fault_injected_first_candidate_then_approved_revision_restarts_baseline(monkeypatch):
    original = source()
    baseline = snapshots(scrub_transactions(original).transactions)
    session = ExecutionSession(original, "duplicate_pagination")
    initial = session.prepared
    first_args = args_for(initial)
    calls = []
    seen = inject_engine_fault(monkeypatch)
    request(session, first_args, calls)
    first = execute_approved(session)
    assert first.status == "revision_available" and first.verification.outcome == "changed"
    revised = session.prepare_revision()
    assert revised.bindings is initial.bindings
    payload = revised.payload
    feedback = payload.pop("revision")
    assert payload == initial.payload
    assert feedback["verification"]["outcome"] == "changed"
    assert feedback["frozen_check"]["refs"] == first_args["check"]["refs"]
    assert feedback["frozen_check"]["definition_refs"] == []
    second_args = args_for(revised, fields=("id", "name", "email", "company"))
    request(session, second_args, calls)
    result = execute_approved(session)
    assert result.status == "preserved" and result.attempts == len(calls) == 2
    assert seen == [baseline, baseline]
    assert result.candidate[1].response_json["customers"][0]["id"] != 77_777_777
    outbound = calls[1]["input"]
    assert outbound == revised.preview_json
    assert "77777777" not in outbound  # The fault-injected candidate value stays local too.
    for forbidden in (PRIVATE, "Maya Chen", "maya.chen@example.test", "api.example.test",
                      "BUGCAPSULE_FAKE", "original_entry", "PrivateBindings", '"path"', '"customers"', "101", "103"):
        assert forbidden not in outbound
    assert calls[1]["max_output_tokens"] == 4096
    assert calls[1]["reasoning"] == {"effort": "low"}
    assert calls[1]["store"] is False
    with pytest.raises(ExecutionError):
        session.prepare_revision()
    with pytest.raises(ExecutionError):
        session.request_proposal(approve_preview(revised), api_key=FAKE_KEY)
    assert session.attempts == 2


def test_initial_send_approval_cannot_authorize_revision_feedback(monkeypatch):
    session = ExecutionSession(source(), "duplicate_pagination")
    initial_approval = approve_preview(session.prepared)
    inject_engine_fault(monkeypatch)
    calls = []
    request(session, args_for(session.prepared), calls)
    execute_approved(session)
    session.prepare_revision()
    with pytest.raises(ApprovalError):
        session.request_proposal(initial_approval, api_key=FAKE_KEY,
                                 transport=transport_for([], calls))
    assert len(calls) == session.attempts == 1


def test_revised_plan_requires_fresh_execution_approval(monkeypatch):
    session = ExecutionSession(source(), "duplicate_pagination")
    seen = inject_engine_fault(monkeypatch)
    request(session, args_for(session.prepared), [])
    first_review = session.review_plan()
    stale = approve_execution(first_review)
    session.execute(stale)
    session.prepare_revision()
    request(session, args_for(session.prepared), [])
    session.review_plan()
    with pytest.raises(ApprovalError):
        session.execute(stale)
    assert len(seen) == 1 and session.attempts == 2


def test_revision_cannot_select_an_easier_check(monkeypatch):
    session = ExecutionSession(source(), "duplicate_pagination")
    seen = inject_engine_fault(monkeypatch)
    request(session, args_for(session.prepared), [])
    execute_approved(session)
    session.prepare_revision()
    request(session, args_for(session.prepared, check_fields=("name",)), [])
    with pytest.raises(ExecutionError, match="changed check"):
        session.review_plan()
    assert session.result.reason == "invalid_or_changed_check" and len(seen) == 1


def test_second_changed_candidate_stops_at_two_requests(monkeypatch):
    session = ExecutionSession(source(), "duplicate_pagination")
    seen = inject_engine_fault(monkeypatch, always=True)
    calls = []
    request(session, args_for(session.prepared), calls)
    execute_approved(session)
    session.prepare_revision()
    request(session, args_for(session.prepared), calls)
    result = execute_approved(session)
    assert result.status == "stopped" and result.verification.outcome == "changed"
    assert len(calls) == len(seen) == session.attempts == 2
    with pytest.raises(ExecutionError):
        session.prepare_revision()


@pytest.mark.parametrize("stage", ["initial", "proposal", "revision"])
def test_cancellation_is_terminal(monkeypatch, stage):
    session = ExecutionSession(source(), "duplicate_pagination")
    calls = []
    if stage != "initial":
        inject_engine_fault(monkeypatch)
        request(session, args_for(session.prepared), calls)
        if stage == "revision":
            execute_approved(session)
            session.prepare_revision()
    result = session.cancel()
    assert result.status == "cancelled" and result.candidate is None
    assert session.attempts == len(calls)
    with pytest.raises(ExecutionError):
        session.request_proposal(approve_preview(session.prepared), api_key=FAKE_KEY)


def fabricated(tmp_path, *, healthy=False, null_ids=False, references=False):
    entries = json.loads(FIXTURE.read_text(encoding="utf-8"))["log"]["entries"]
    if references:
        entries = entries[:1]
        entries[0]["response"]["content"]["text"] = json.dumps({
            "definitions": [{"code": 1}], "links": [{"target": 1}, {"target": 9}],
        })
    else:
        for index, entry in enumerate(entries):
            body = json.loads(entry["response"]["content"]["text"])
            if healthy and index == 1:
                body["customers"][0]["id"] = 106
            if null_ids:
                for customer in body["customers"]:
                    customer["id"] = None
            entry["response"]["content"]["text"] = json.dumps(body)
    file = tmp_path / "fabricated.har"
    file.write_text(json.dumps({"log": {"entries": entries}}), encoding="utf-8")
    return parse_har(file).transactions


@pytest.mark.parametrize("mode, outcome", [("healthy", "not_present"), ("null", "inconclusive")])
def test_baseline_without_confirmed_failure_stops_before_transformation(tmp_path, monkeypatch, mode, outcome):
    session = ExecutionSession(fabricated(tmp_path, healthy=mode == "healthy", null_ids=mode == "null"), "duplicate_pagination")
    request(session, args_for(session.prepared), [])

    def forbidden(*args, **kwargs):
        raise AssertionError("Baseline failure must be confirmed before transformation.")
    monkeypatch.setattr(execution, "apply_replacements", forbidden)
    result = execute_approved(session)
    assert result.status == "review_required" and result.verification.outcome == outcome
    assert result.candidate is None
    with pytest.raises(ExecutionError):
        session.prepare_revision()


def test_inconclusive_candidate_does_not_trigger_revision(monkeypatch):
    session = ExecutionSession(source(), "duplicate_pagination")
    inject_engine_fault(monkeypatch, incoherent=True)
    request(session, args_for(session.prepared), [])
    result = execute_approved(session)
    assert result.status == "review_required" and result.verification.outcome == "inconclusive"
    with pytest.raises(ExecutionError):
        session.prepare_revision()


@pytest.mark.parametrize("failure", ["http", "incomplete", "refusal", "plain", "invalid"])
def test_model_failures_count_once_and_never_retry(failure):
    session = ExecutionSession(source(), "duplicate_pagination")
    args = args_for(session.prepared)
    if failure == "http":
        reply = (503, {"error": {"message": PRIVATE}})
    elif failure == "incomplete":
        reply = (200, {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}, "output": [{"arguments": PRIVATE}]})
    elif failure == "refusal":
        reply = (200, {"status": "completed", "output": [{"type": "message", "content": [{"type": "refusal", "refusal": PRIVATE}]}]})
    elif failure == "plain":
        reply = (200, {"status": "completed", "output": []})
    else:
        args["replacements"][0]["ref"] = "node_9999"
        reply = args
    calls = []
    with pytest.raises(ValueError) as error:
        session.request_proposal(approve_preview(session.prepared), api_key=FAKE_KEY,
                                 transport=transport_for([reply], calls))
    assert PRIVATE not in str(error.value)
    assert session.attempts == len(calls) == 1 and session.result.candidate is None
    with pytest.raises(ExecutionError):
        session.request_proposal(approve_preview(session.prepared), api_key=FAKE_KEY)


def test_failed_revision_counts_as_second_attempt_and_stops(monkeypatch):
    session = ExecutionSession(source(), "duplicate_pagination")
    inject_engine_fault(monkeypatch)
    calls = []
    request(session, args_for(session.prepared), calls)
    execute_approved(session)
    session.prepare_revision()
    with pytest.raises(AgentIncompleteResponseError):
        session.request_proposal(approve_preview(session.prepared), api_key=FAKE_KEY,
                                 transport=transport_for([(200, {"status": "incomplete"})], calls))
    assert session.attempts == len(calls) == 2
    with pytest.raises(ExecutionError):
        session.prepare_revision()


def test_model_needs_review_cannot_be_executed_or_revised():
    session = ExecutionSession(source(), "duplicate_pagination")
    args = args_for(session.prepared)
    args.update(needs_review=True, review_reason=PRIVATE)
    calls = []
    request(session, args, calls)
    assert session.result.status == "review_required" and session.result.candidate is None
    assert session.attempts == len(calls) == 1
    with pytest.raises(ExecutionError):
        session.review_plan()
    with pytest.raises(ExecutionError):
        session.prepare_revision()


def test_definition_reference_roles_cannot_change(tmp_path, monkeypatch):
    session = ExecutionSession(fabricated(tmp_path, references=True), "inconsistent_identifier_references")
    definition = node_ref(session.prepared, 0, ["definitions", "*", "code"])
    reference = node_ref(session.prepared, 0, ["links", "*", "target"])
    args = {"replacements": [{"ref": definition, "kind": "id"}, {"ref": reference, "kind": "id"}],
            "relationships": [], "check": {"type": "inconsistent_identifier_references", "refs": [definition, reference]},
            "needs_review": False, "review_reason": ""}

    def faulty_reference_candidate(transactions, plan):
        result = real_apply(transactions, plan)
        result.transactions[0].response_json["links"][1]["target"] = result.transactions[0].response_json["definitions"][0]["code"]
        coherent_body(result.transactions[0])
        return result
    monkeypatch.setattr(execution, "apply_replacements", faulty_reference_candidate)
    request(session, args, [])
    review = session.review_plan(definition_refs=[definition])
    assert session.execute(approve_execution(review)).status == "revision_available"
    prepared = session.prepare_revision()
    assert prepared.payload["revision"]["frozen_check"]["definition_refs"] == [definition]
    request(session, args, [])
    with pytest.raises(ExecutionError):
        session.review_plan(definition_refs=[reference])
    assert session.result.reason == "invalid_or_changed_check"


def test_revision_boundary_rejects_arbitrary_feedback_and_nonchanged_results(monkeypatch):
    session = ExecutionSession(source(), "duplicate_pagination")
    inject_engine_fault(monkeypatch)
    initial = session.prepared
    args = args_for(initial)
    request(session, args, [])
    result = execute_approved(session)
    for invalid in (result.verification.model_copy(update={"reason": PRIVATE}),
                    result.verification.model_copy(update={"outcome": "inconclusive"})):
        with pytest.raises(PrivacyError) as error:
            prepare_revision_payload(source(), initial, invalid,
                                     prior_replacements=[(args["check"]["refs"][0], "id")],
                                     check_refs=args["check"]["refs"])
        assert PRIVATE not in str(error.value)


@pytest.mark.parametrize("answers, expected_attempts", [(["no"], 0), (["APPROVE", "no"], 1), (["APPROVE", "EXECUTE"], 1)])
def test_execute_driver_confirmations_are_offline(monkeypatch, capsys, answers, expected_attempts):
    script = Path(__file__).resolve().parents[1] / "scripts" / "plan_fixture.py"
    monkeypatch.setattr(sys, "path", list(sys.path))
    spec = importlib.util.spec_from_file_location("execution_fixture_driver_test", script)
    driver = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(driver)
    calls = []

    def offline_send(body, key):
        assert key == FAKE_KEY
        # Test-only proposal refs are resolved from an independently prepared
        # identical fixture snapshot, never sent as local paths.
        session = ExecutionSession(source(), "duplicate_pagination", approved_labels=driver.APPROVED_LABELS)
        return transport_for([args_for(session.prepared)], calls)(body)

    monkeypatch.setattr(client, "_send_via_http", offline_send)
    monkeypatch.setenv("META_API_KEY", FAKE_KEY)
    monkeypatch.setattr(sys, "argv", [str(script), "--execute"])
    replies = iter(answers)
    monkeypatch.setattr("builtins.input", lambda _prompt: next(replies))
    result = driver.main()
    assert len(calls) == expected_attempts
    assert result == (0 if answers[-1] == "EXECUTE" else 1)
    output = capsys.readouterr().out
    assert f'"attempts": {expected_attempts}' in output
    assert PRIVATE not in output


def test_execute_driver_revision_requires_two_new_confirmations(monkeypatch, capsys):
    script = Path(__file__).resolve().parents[1] / "scripts" / "plan_fixture.py"
    monkeypatch.setattr(sys, "path", list(sys.path))
    spec = importlib.util.spec_from_file_location("execution_revision_driver_test", script)
    driver = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(driver)
    prepared = ExecutionSession(source(), "duplicate_pagination", approved_labels=driver.APPROVED_LABELS).prepared
    calls = []
    transport = transport_for([args_for(prepared), args_for(prepared, fields=("id", "name"))], calls)

    def offline_send(body, key):
        assert key == FAKE_KEY
        return transport(body)

    seen = inject_engine_fault(monkeypatch)
    monkeypatch.setattr(client, "_send_via_http", offline_send)
    monkeypatch.setenv("META_API_KEY", FAKE_KEY)
    monkeypatch.setattr(sys, "argv", [str(script), "--execute"])
    answers = iter(["APPROVE", "EXECUTE", "APPROVE", "EXECUTE"])
    prompts = []

    def confirm(prompt):
        prompts.append(prompt)
        return next(answers)

    monkeypatch.setattr("builtins.input", confirm)
    assert driver.main() == 0
    assert len(calls) == len(seen) == 2 and len(prompts) == 4
    output = capsys.readouterr().out
    assert '"status": "revision_available"' in output
    assert '"status": "preserved"' in output and '"attempts": 2' in output
    assert PRIVATE not in output and "77777777" not in output
