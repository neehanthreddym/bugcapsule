"""Offline web execution: mocked planning, real local replacement and verification."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import http.client
import json
from pathlib import Path
import socket
from threading import Event
import urllib.request

from fastapi.testclient import TestClient
import pytest

from app.agent import client as agent_client, execution
from app.agent.execution import verified_export_result
from app.main import create_app
from app.services import capture_store
from app.services.capture_store import CAPTURE_TTL_SECONDS, CaptureStore
from tests.test_capture_api import upload
from tests.test_execution import args_for, coherent_body, response


FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "duplicate-pagination.har"
PRIVATE = "FABRICATED_EXECUTION_ERROR_PRIVATE_839191"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("No actual network or revision calls are allowed.")
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(http.client, "HTTPSConnection", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(agent_client, "_send_via_http", forbidden)
    monkeypatch.setattr(execution.ExecutionSession, "prepare_revision", forbidden)


@pytest.fixture
def env():
    replies, calls, key_reads = [], [], []

    def transport(body):
        calls.append(deepcopy(body))
        assert replies, "Unexpected model request, including during local execution."
        return 200, json.dumps(response(replies.pop(0))).encode()

    def key():
        key_reads.append(True)
        return "FABRICATED_KEY_NOT_REAL"

    clock = [0.0]
    store = CaptureStore(clock=lambda: clock[0], planning_transport=transport, key_provider=key)
    with TestClient(create_app(capture_store=store), base_url="http://localhost:8000") as local:
        yield local, store, replies, calls, key_reads, clock


def planned(env, *, raw=None, pattern="duplicate_pagination", alter=None):
    local, store, replies, *_ = env
    path = "/api/captures/" + upload(local, raw).json()["capture_id"]
    assert local.put(path + "/selection", json={"entry_ids": [0, 1]}).status_code == 200
    preview = local.post(path + "/model-preview", json={"failure_pattern": pattern}).json()
    record = store._captures[path.rsplit("/", 1)[1]]
    args = args_for(record.preview.prepared, fields=("id", "name", "email", "company"), pattern=pattern)
    args["relationships"][0]["reason"] = "Offline mock: matching pseudonymized identities."
    args["check"]["note"] = "Offline mock proposal; not a live model call."
    args["review_reason"] = ""
    if alter is not None:
        alter(args)
    replies.append(args)
    reply = local.post(path + "/plan", json={"preview_id": preview["preview_id"], "sha256": preview["sha256"], "approved": True})
    assert reply.status_code == 200, reply.text
    return path, reply.json(), record


@contextmanager
def execution_only(monkeypatch, store):
    """After mocked planning, fail any attempted planning/key access during execution."""
    def forbidden(*args, **kwargs):
        raise AssertionError("Local execution must not access model configuration or request planning.")
    with monkeypatch.context() as guard:
        guard.setattr(store, "_key_provider", forbidden)
        guard.setattr(store, "_planning_transport", forbidden)
        guard.setattr(execution, "request_capsule_plan", forbidden)
        guard.setattr(capture_store, "approve_preview", forbidden)
        yield


def run(local, path, proposal, **overrides):
    review = proposal["execution_review"]
    return local.post(path + "/execute", json={
        "proposal_id": proposal["proposal_id"], "review_id": review["review_id"] or "blocked",
        "sha256": review["sha256"] or "0" * 64, "approved": True, **overrides,
    })


def safe_error(reply, status, code):
    assert reply.status_code == status, reply.text
    assert reply.json()["error"]["code"] == code
    assert PRIVATE not in reply.text and "BUGCAPSULE_FAKE" not in reply.text
    assert "api.example.test" not in reply.text and "input_value" not in reply.text
    assert reply.headers["Cache-Control"] == "no-store"


def test_real_fixture_approval_binding_counts_duplicate_locations_and_retained_seal(env, monkeypatch):
    local, store, _, calls, keys, _ = env
    path, proposal, record = planned(env)
    planning = record.planning
    info = proposal["execution_review"]
    assert info["status"] == "ready" and info["attempt"] == 1
    assert info["sha256"] == hashlib.sha256(info["review_json"].encode()).hexdigest()
    configuration = json.loads(info["review_json"])
    assert set(configuration) == {"attempt", "replacement_plan", "check", "definition_refs"}
    assert configuration["definition_refs"] == [] and configuration["attempt"] == 1
    assert configuration["check"]["pages"][0]["fields"][0]["path"] == [{"key": "customers"}, {"each": "*"}, {"key": "id"}]
    assert "Offline mock" not in info["review_json"]  # Prose is not the executable binding.
    assert planning.session._frozen_check is None
    before = [(deepcopy(tx.response_json), deepcopy(tx.original_entry)) for tx in record.parsed.transactions]
    baseline = [(deepcopy(tx.response_json), deepcopy(tx.original_entry)) for tx in planning.session._baseline]
    actual_results = []
    real_report = capture_store.execution_report

    def retain_exact_result(result, **kwargs):
        actual_results.append(result)
        return real_report(result, **kwargs)

    monkeypatch.setattr(capture_store, "execution_report", retain_exact_result)
    with execution_only(monkeypatch, store):
        reply = run(local, path, proposal)
    assert reply.status_code == 200 and reply.headers["Cache-Control"] == "no-store"
    dto = reply.json()
    assert dto["proposal_id"] == proposal["proposal_id"] and dto["review_id"] == info["review_id"]
    assert dto["review_sha256"] == info["sha256"] and dto["attempt"] == 1
    assert dto["status"] == "preserved" and dto["candidate_created"] is True
    verification = dto["verification"]
    assert verification["outcome"] == "preserved" and verification["reason"] == "failure_preserved"
    for evidence in (verification["baseline"], verification["transformed"]):
        assert evidence["observations"] == 6 and evidence["distinct_identities"] == 5 and evidence["failures"] == 1
        assert [item["count"] for item in evidence["record_counts"]] == [3, 3]
        assert [(loc["entry_id"], loc["record"]) for loc in evidence["equality_groups"][0]] == [(0, [2]), (1, [0])]
    assert planning.execution_result is actual_results[0]
    assert planning.execution_id == dto["execution_id"]
    assert verified_export_result(planning.execution_result).status == "preserved"  # Seal check only; no exporter/files.
    assert json.loads(planning.session._frozen_check) == configuration["check"]
    assert [(tx.response_json, tx.original_entry) for tx in record.parsed.transactions] == before
    assert [(tx.response_json, tx.original_entry) for tx in planning.session._baseline] == baseline
    assert len(calls) == len(keys) == 1
    for forbidden in ("BUGCAPSULE_FAKE", "original_entry", '"candidate":', "PrivateBindings", '"path":', '"key":', "_seal", "customers", "api.example.test"):
        assert forbidden not in reply.text
    for body, _ in before:
        for customer in body["customers"]:
            for key in ("name", "email", "company"):
                assert customer[key] not in reply.text
    assert "execution_result" not in local.get(path).json()


def test_review_digest_binds_configuration_not_model_prose(env):
    first = planned(env)[1]
    def different_prose(args):
        args["relationships"][0]["reason"] = "Different untrusted model prose."
        args["review_reason"] = "Different note."
        args["check"]["note"] = "Changed prose without changing the check."
    second = planned(env, alter=different_prose)[1]
    assert first["execution_review"]["review_id"] != second["execution_review"]["review_id"]
    assert first["execution_review"]["sha256"] == second["execution_review"]["sha256"]
    def different_check(args):
        args["check"]["refs"] = [args["replacements"][1]["ref"], args["replacements"][5]["ref"]]
    third = planned(env, alter=different_check)[1]
    assert third["execution_review"]["sha256"] != first["execution_review"]["sha256"]


@pytest.mark.parametrize("case", ["false", "missing", "proposal", "review", "digest", "selection", "preview", "expired", "deleted", "tampered", "attempt"])
def test_execution_preconditions_prevent_work_and_do_not_consume_approval(env, monkeypatch, case):
    local, store, _, calls, keys, clock = env
    path, proposal, record = planned(env)
    planning = record.planning
    overrides = {}
    expected = (409, "stale_execution_review")
    if case == "false":
        overrides["approved"] = False
        expected = (403, "execution_approval_required")
    elif case == "proposal":
        overrides["proposal_id"] = "unknown"
    elif case == "review":
        overrides["review_id"] = "unknown"
    elif case == "digest":
        overrides["sha256"] = "0" * 64
    elif case == "selection":
        local.put(path + "/selection", json={"entry_ids": [0]})
    elif case == "preview":
        local.post(path + "/model-preview", json={"failure_pattern": "missing_null_fields"})
    elif case == "expired":
        clock[0] += CAPTURE_TTL_SECONDS
        expected = (404, "capture_not_found")
    elif case == "deleted":
        local.delete(path)
        expected = (404, "capture_not_found")
    elif case == "tampered":
        # TEST-ONLY tampering of the private review snapshot must invalidate the digest.
        planning.execution_review._preview += " "
    elif case == "attempt":
        planning.execution_review_info.attempt += 1
    def forbidden(*args, **kwargs):
        raise AssertionError("Rejected execution must never reach the session executor.")
    monkeypatch.setattr(execution.ExecutionSession, "execute", forbidden)
    with execution_only(monkeypatch, store):
        if case == "missing":
            info = proposal["execution_review"]
            reply = local.post(path + "/execute", json={"proposal_id": proposal["proposal_id"], "review_id": info["review_id"], "sha256": info["sha256"]})
            expected = (403, "execution_approval_required")
        else:
            reply = run(local, path, proposal, **overrides)
    safe_error(reply, *expected)
    assert not planning.execution_consumed and planning.execution_result is None
    assert len(calls) == len(keys) == 1


@pytest.mark.parametrize("extra", [{"rules": [PRIVATE]}, {"definition_refs": ["node_0007"]}, {"candidate": PRIVATE}, {"approved": 1}])
def test_browser_rules_roles_and_coerced_approval_are_not_authoritative(env, extra):
    local, _, _, calls, _, _ = env
    path, proposal, record = planned(env)
    safe_error(run(local, path, proposal, **extra), 422, "invalid_execution_request")
    assert not record.planning.execution_consumed and len(calls) == 1


@pytest.mark.parametrize("kind", ["roles", "check", "model_review"])
def test_unresolved_check_roles_and_needs_review_are_blocked(env, monkeypatch, kind):
    def alter(args):
        if kind == "check":
            args["check"]["refs"] = args["check"]["refs"][:1]
        if kind == "model_review":
            args["needs_review"] = True
            args["review_reason"] = "Human review required."
    local, store, _, calls, _, _ = env
    path, proposal, record = planned(env, pattern="inconsistent_identifier_references" if kind == "roles" else "duplicate_pagination", alter=alter)
    assert proposal["execution_review"]["status"] == "blocked" and proposal["execution_review"]["review_id"] is None
    def forbidden(*args, **kwargs):
        raise AssertionError("Blocked configuration cannot execute.")
    monkeypatch.setattr(execution.ExecutionSession, "execute", forbidden)
    with execution_only(monkeypatch, store):
        safe_error(run(local, path, proposal), 409, "execution_blocked")
    assert not record.planning.execution_consumed and len(calls) == 1


def test_duplicate_and_concurrent_executions_run_once_without_store_lock(env, monkeypatch):
    local, store, _, calls, _, _ = env
    path, proposal, record = planned(env)
    real_apply = execution.apply_replacements
    entered, release, applied = Event(), Event(), []
    def slow_apply(transactions, plan):
        applied.append(True)
        entered.set()
        assert release.wait(timeout=5)
        return real_apply(transactions, plan)
    monkeypatch.setattr(execution, "apply_replacements", slow_apply)
    with execution_only(monkeypatch, store), ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(run, local, path, proposal)
        try:
            assert entered.wait(timeout=3)
            safe_error(run(local, path, proposal), 409, "execution_consumed")
            assert local.get(path).status_code == 200
            assert local.get("/health").json() == {"status": "ok"}
        finally:
            release.set()
        assert first.result(timeout=3).json()["verification"]["outcome"] == "preserved"
        safe_error(run(local, path, proposal), 409, "execution_consumed")
    assert len(applied) == len(calls) == 1 and record.planning.session.attempts == 1


@pytest.mark.parametrize("change", ["selection", "preview", "deleted", "expired"])
def test_upstream_invalidation_discards_in_flight_execution_results(env, monkeypatch, change):
    local, store, _, calls, _, clock = env
    path, proposal, record = planned(env)
    old_planning = record.planning
    real_apply = execution.apply_replacements
    entered, release = Event(), Event()
    def slow_apply(transactions, plan):
        entered.set()
        assert release.wait(timeout=5)
        return real_apply(transactions, plan)
    monkeypatch.setattr(execution, "apply_replacements", slow_apply)
    with execution_only(monkeypatch, store), ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(run, local, path, proposal)
        try:
            assert entered.wait(timeout=3)
            if change == "selection":
                local.put(path + "/selection", json={"entry_ids": [0]})
            elif change == "preview":
                local.post(path + "/model-preview", json={"failure_pattern": "missing_null_fields"})
            elif change == "deleted":
                local.delete(path)
            else:
                clock[0] += CAPTURE_TTL_SECONDS
        finally:
            release.set()
        safe_error(first.result(timeout=3), 404 if change in {"deleted", "expired"} else 409,
                   "capture_not_found" if change in {"deleted", "expired"} else "stale_execution_review")
    assert old_planning.execution_result is None and len(calls) == 1
    if change in {"selection", "preview"}:
        assert record.planning is None
    else:
        assert not store._captures


def test_completed_result_is_invalidated_by_selection_or_preview_change(env):
    local, _, _, _, _, _ = env
    path, proposal, record = planned(env)
    assert run(local, path, proposal).status_code == 200
    local.put(path + "/selection", json={"entry_ids": [1, 0]})
    assert record.planning is None
    safe_error(run(local, path, proposal), 409, "stale_execution_review")


def test_changed_outcome_uses_clearly_fault_injected_candidate_and_real_verifier(env, monkeypatch):
    local, store, _, calls, _, _ = env
    path, proposal, record = planned(env)
    real_apply = execution.apply_replacements
    def deliberately_faulted_engine(transactions, plan):
        # TEST-ONLY fault: break the duplicate after real replacement, keeping HAR
        # representations coherent. The real deterministic verifier must detect it.
        result = real_apply(transactions, plan)
        result.transactions[1].response_json["customers"][0]["id"] = 77_777_777
        coherent_body(result.transactions[1])
        return result
    monkeypatch.setattr(execution, "apply_replacements", deliberately_faulted_engine)
    with execution_only(monkeypatch, store):
        reply = run(local, path, proposal)
    assert reply.status_code == 200
    assert reply.json()["status"] == "revision_available" and reply.json()["verification"]["outcome"] == "changed"
    assert reply.json()["verification"]["transformed"]["failures"] == 0
    assert record.planning.execution_result.verification.outcome == "changed"
    assert len(calls) == 1 and "77777777" not in reply.text


@pytest.mark.parametrize("mode, outcome", [("healthy", "not_present"), ("null", "inconclusive")])
def test_fabricated_healthy_or_null_identity_baselines_stop_before_replacement(env, monkeypatch, mode, outcome):
    # Clearly labeled TEST VARIANTS of the fabricated HAR, not the canonical fixture.
    har = json.loads(FIXTURE.read_bytes())
    for index, entry in enumerate(har["log"]["entries"]):
        body = json.loads(entry["response"]["content"]["text"])
        if mode == "healthy" and index == 1:
            body["customers"][0]["id"] = 106
        elif mode == "null":
            for customer in body["customers"]:
                customer["id"] = None
        entry["response"]["content"]["text"] = json.dumps(body)
    local, store, _, calls, _, _ = env
    path, proposal, record = planned(env, raw=json.dumps(har).encode())
    def forbidden(*args, **kwargs):
        raise AssertionError("Unconfirmed baseline failure cannot reach replacement.")
    monkeypatch.setattr(execution, "apply_replacements", forbidden)
    with execution_only(monkeypatch, store):
        reply = run(local, path, proposal)
    assert reply.status_code == 200 and reply.json()["verification"]["outcome"] == outcome
    assert reply.json()["status"] == "review_required" and reply.json()["candidate_created"] is False
    assert record.planning.execution_result.candidate is None and len(calls) == 1


def test_real_conflicting_replacement_kinds_produce_actual_execution_error(env):
    def conflict(args):
        # Same customer's name appears on both pages, so incompatible kinds are
        # a semantic conflict detected by the real engine, not tool-schema validation.
        args["replacements"][5]["kind"] = "text"
    local, _, _, calls, _, _ = env
    path, proposal, record = planned(env, alter=conflict)
    reply = run(local, path, proposal)
    assert reply.status_code == 200
    assert reply.json()["status"] == "stopped" and reply.json()["reason"] == "execution_failed"
    assert reply.json()["verification"] is None and not reply.json()["candidate_created"]
    assert record.planning.execution_result.reason == "execution_failed" and len(calls) == 1


def test_unexpected_execution_errors_are_value_free_and_consumed(env, monkeypatch):
    local, _, _, calls, _, _ = env
    path, proposal, record = planned(env)
    def unexpected(*args, **kwargs):
        raise RuntimeError(PRIVATE)
    monkeypatch.setattr(execution, "apply_replacements", unexpected)
    safe_error(run(local, path, proposal), 500, "local_execution_failed")
    assert record.planning.execution_result is None and record.planning.execution_consumed
    safe_error(run(local, path, proposal), 409, "execution_consumed")
    assert len(calls) == 1


def test_host_origin_boundaries_apply_to_execution(env):
    local, _, _, _, _, _ = env
    path, proposal, record = planned(env)
    info = proposal["execution_review"]
    body = {"proposal_id": proposal["proposal_id"], "review_id": info["review_id"], "sha256": info["sha256"], "approved": True}
    safe_error(local.post(path + "/execute", json=body, headers={"Origin": "http://attacker.example.test"}), 403, "invalid_origin")
    safe_error(local.post(path + "/execute", json=body, headers={"Host": "attacker.example.test"}), 400, "invalid_host")
    assert not record.planning.execution_consumed
