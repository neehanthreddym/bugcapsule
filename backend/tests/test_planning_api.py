"""Approval-bound web planning: real validation, mocked transport, no paid calls."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import http.client
import json
import socket
from threading import Event
import urllib.request

from fastapi.testclient import TestClient
import pytest

from app.agent import client as agent_client, execution
from app.agent.client import build_request_body
from app.config import ConfigError
from app.main import create_app
from app.services.capture_store import CAPTURE_TTL_SECONDS, CaptureStore
from tests.test_capture_api import upload
from tests.test_execution import args_for, response


PRIVATE = "FABRICATED_PROVIDER_ERROR_NEVER_DISPLAY_938191"
FAKE_KEY = "FABRICATED_BACKEND_KEY_NOT_REAL"


@pytest.fixture(autouse=True)
def no_network_or_execution(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Planning tests cannot use sockets, execution, verification or execution approval.")
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(http.client, "HTTPSConnection", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(agent_client, "_send_via_http", forbidden)
    monkeypatch.setattr(execution, "apply_replacements", forbidden)
    monkeypatch.setattr(execution, "verify_failure", forbidden)
    monkeypatch.setattr(execution, "approve_execution", forbidden)
    monkeypatch.setattr(execution.ExecutionSession, "execute", forbidden)
    monkeypatch.setattr(execution.ExecutionSession, "prepare_revision", forbidden)


class MockProvider:
    def __init__(self):
        self.calls = []
        self.body = None
        self.failure = None
        self.status = 200
        self.entered = None
        self.release = None

    def __call__(self, request):
        self.calls.append(deepcopy(request))
        if self.entered is not None:
            self.entered.set()
            assert self.release.wait(timeout=5), "Test transport was not released."
        if self.failure is not None:
            raise self.failure
        return self.status, json.dumps(self.body).encode()


@pytest.fixture
def setup():
    provider = MockProvider()
    key_reads = []

    def key_provider():
        key_reads.append(True)
        return FAKE_KEY

    clock = [0.0]
    store = CaptureStore(clock=lambda: clock[0], planning_transport=provider, key_provider=key_provider)
    app = create_app(capture_store=store)
    with TestClient(app, base_url="http://127.0.0.1:8000") as local:
        path = "/api/captures/" + upload(local).json()["capture_id"]
        assert local.put(path + "/selection", json={"entry_ids": [0, 1]}).status_code == 200
        preview = local.post(path + "/model-preview", json={"failure_pattern": "duplicate_pagination", "approved_labels": []}).json()
        record = store._captures[path.rsplit("/", 1)[1]]
        args = args_for(record.preview.prepared, fields=("id", "name", "email", "company"))
        args["relationships"][0]["reason"] = "Equal pseudonymized tokens suggest the same customer."
        args["check"]["note"] = "Offline mock proposal; not a new live model call."
        args["review_reason"] = ""
        provider.body = {**response(args), "usage": {"input_tokens": 300, "output_tokens": 800, "total_tokens": 1100}}
        yield local, store, provider, path, preview, record, clock, key_reads


def plan(local, path, preview, **overrides):
    return local.post(path + "/plan", json={"preview_id": preview["preview_id"], "sha256": preview["sha256"],
                                            "approved": True, **overrides})


def assert_safe_error(reply, status, code):
    assert reply.status_code == status, reply.text
    assert reply.json()["error"]["code"] == code
    for private in (PRIVATE, FAKE_KEY, "BUGCAPSULE_FAKE", "api.example.test", "Traceback", "input_value"):
        assert private not in reply.text
    assert reply.headers["Cache-Control"] == "no-store"


def test_success_exact_retained_input_and_explicit_private_review(setup):
    local, store, provider, path, preview, record, _, key_reads = setup
    originals = [(deepcopy(tx.response_json), deepcopy(tx.original_entry)) for tx in record.parsed.transactions]
    retained = record.preview.prepared
    reply = plan(local, path, preview)
    assert reply.status_code == 200
    dto = reply.json()
    assert dto["preview_id"] == preview["preview_id"] and dto["sha256"] == preview["sha256"]
    assert len(dto["proposal_id"]) >= 32
    assert dto["validation"] == "schema_validated" and dto["verification"] == dto["transformations"] == "not_run"
    assert dto["needs_review"] is False
    assert dto["check"]["resolution"] == "resolved"
    assert dto["check"]["identity_groups"] == [["node_0007"], ["node_0024"]]
    assert dto["check"]["locations"][0] == {"ref": "node_0007", "entry_id": 0, "target": "response_json",
                                             "path": [{"key": "customers"}, {"each": "*"}, {"key": "id"}]}
    assert dto["resolved_replacements"] and dto["relationships"]
    assert dto["usage"] == {"input_tokens": 300, "output_tokens": 800, "total_tokens": 1100}
    assert dto["diagnostics"]["attempts"] == 1 and dto["diagnostics"]["request_attempted"] is True
    assert dto["diagnostics"]["latency_ms"] >= 0
    assert provider.calls == [build_request_body(retained.preview_json)]
    assert provider.calls[0]["input"] == retained.preview_json == preview["preview_json"]
    assert record.planning.session.prepared is retained
    assert record.planning.session.attempts == 1 and record.planning.session.result is None
    assert record.planning.proposal is not None and record.planning.proposal_id == dto["proposal_id"]
    assert len(key_reads) == 1
    for forbidden in (FAKE_KEY, "BUGCAPSULE_FAKE", "original_entry", "PrivateBindings", "response_json\": {", "api.example.test"):
        assert forbidden not in reply.text
    for tx in record.parsed.transactions:
        for customer in tx.response_json["customers"]:
            for key in ("name", "email", "company"):
                assert customer[key] not in reply.text
    assert [(tx.response_json, tx.original_entry) for tx in record.parsed.transactions] == originals
    assert "proposal_id" not in local.get(path).json()  # Capture summaries don't serialize private planning state.
    assert_safe_error(plan(local, path, preview), 409, "planning_consumed")
    assert len(provider.calls) == 1


@pytest.mark.parametrize("case", ["false", "missing", "wrong_id", "wrong_digest", "changed_selection", "new_preview", "expired", "deleted"])
def test_preconditions_reject_before_key_access_or_network(setup, case):
    local, store, provider, path, preview, record, clock, key_reads = setup
    body = {"preview_id": preview["preview_id"], "sha256": preview["sha256"], "approved": True}
    expected = (409, "stale_preview")
    if case == "false":
        body["approved"] = False
        expected = (403, "approval_required")
    elif case == "missing":
        del body["approved"]
        expected = (403, "approval_required")
    elif case == "wrong_id":
        body["preview_id"] = "stale_id"
    elif case == "wrong_digest":
        body["sha256"] = "0" * 64
    elif case == "changed_selection":
        local.put(path + "/selection", json={"entry_ids": [0]})
    elif case == "new_preview":
        new = local.post(path + "/model-preview", json={"failure_pattern": "duplicate_pagination"}).json()
        assert new["sha256"] == preview["sha256"] and new["preview_id"] != preview["preview_id"]
    elif case == "expired":
        clock[0] += CAPTURE_TTL_SECONDS
        expected = (404, "capture_not_found")
    elif case == "deleted":
        local.delete(path)
        expected = (404, "capture_not_found")
    assert_safe_error(local.post(path + "/plan", json=body), *expected)
    assert provider.calls == [] and key_reads == [] and record.planning_consumed is False


@pytest.mark.parametrize("extra", [{"payload": PRIVATE}, {"selectors": [PRIVATE]}, {"api_key": PRIVATE}, {"approved": 1}, {"sha256": PRIVATE}])
def test_browser_cannot_supply_keys_payloads_selectors_or_coerced_approval(setup, extra):
    local, _, provider, path, preview, _, _, key_reads = setup
    assert_safe_error(plan(local, path, preview, **extra), 422, "invalid_plan_request")
    assert provider.calls == [] and key_reads == []


def test_missing_key_is_safe_before_reservation_and_request_attempt(setup):
    local, store, provider, path, preview, record, _, _ = setup

    def missing():
        raise ConfigError(PRIVATE)

    store._key_provider = missing
    reply = plan(local, path, preview)
    assert_safe_error(reply, 503, "missing_meta_configuration")
    assert reply.json()["error"]["diagnostics"]["attempts"] == 0
    assert reply.json()["error"]["diagnostics"]["request_attempted"] is False
    assert record.planning is None and not record.planning_consumed and provider.calls == []


@pytest.mark.parametrize("mode, code, status", [
    ("timeout", "planning_timeout", 504), ("network", "planning_transport_error", 502),
    ("http", "planning_http_error", 502), ("incomplete", "planning_incomplete", 502),
    ("unknown_incomplete", "planning_incomplete", 502), ("invalid_proposal", "planning_invalid_proposal", 502),
    ("refusal", "planning_response_error", 502), ("missing_tool", "planning_response_error", 502),
    ("unexpected", "planning_failed", 500),
])
def test_attempted_failures_consume_submission_without_retry_or_unsafe_output(setup, mode, code, status):
    local, _, provider, path, preview, record, _, _ = setup
    if mode == "timeout":
        provider.failure = TimeoutError(PRIVATE)
    elif mode == "network":
        provider.failure = OSError(PRIVATE)
    elif mode == "unexpected":
        provider.failure = RuntimeError(PRIVATE)
    elif mode == "http":
        provider.status, provider.body = 503, {"error": {"message": PRIVATE}}
    elif mode in {"incomplete", "unknown_incomplete"}:
        provider.body = {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens" if mode == "incomplete" else PRIVATE},
                         "usage": {"input_tokens": 300, "output_tokens": 4096, "total_tokens": 4396,
                                   "output_tokens_details": {"reasoning_tokens": 4094}}, "output": [{"arguments": PRIVATE}]}
    elif mode == "invalid_proposal":
        arguments = args_for(record.preview.prepared)
        arguments["replacements"][0]["ref"] = "node_9999"
        arguments["review_reason"] = PRIVATE
        provider.body = response(arguments)
    elif mode == "refusal":
        provider.body = {"status": "completed", "output": [{"type": "message", "content": [{"type": "refusal", "refusal": PRIVATE}]}]}
    else:
        provider.body = {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": PRIVATE}]}]}
    reply = plan(local, path, preview)
    assert_safe_error(reply, status, code)
    diagnostics = reply.json()["error"]["diagnostics"]
    assert diagnostics["attempts"] == 1 and diagnostics["request_attempted"] is True
    if mode == "http":
        assert diagnostics["http_status"] == 503
    if "incomplete" in mode:
        assert diagnostics["incomplete"]["reason"] == ("max_output_tokens" if mode == "incomplete" else "unknown")
        assert diagnostics["incomplete"]["max_output_tokens"] == 4096
        assert diagnostics["incomplete"]["reasoning_tokens"] == 4094
    assert record.planning.session.attempts == 1 and record.planning.proposal is None
    assert_safe_error(plan(local, path, preview), 409, "planning_consumed")
    # New IDs/settings or reselecting cannot create a fresh session after failure.
    local.put(path + "/selection", json={"entry_ids": [1, 0]})
    new = local.post(path + "/model-preview", json={"failure_pattern": "missing_null_fields", "approved_labels": ["id"]}).json()
    assert record.planning is None and record.planning_consumed
    assert_safe_error(plan(local, path, new), 409, "planning_consumed")
    assert len(provider.calls) == 1


def test_concurrent_double_submission_and_other_capture_requests_do_not_block(setup):
    local, store, provider, path, preview, record, _, _ = setup
    provider.entered, provider.release = Event(), Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(plan, local, path, preview)
        try:
            assert provider.entered.wait(timeout=3)
            assert record.planning.session.attempts == 1
            assert_safe_error(plan(local, path, preview), 409, "planning_consumed")
            # These require the store lock, so prove it isn't held during I/O.
            assert local.get(path).status_code == 200
            assert upload(local).status_code == 201
            assert local.get("/health").json() == {"status": "ok"}
        finally:
            provider.release.set()
        assert first.result(timeout=3).status_code == 200
    assert len(provider.calls) == 1 and record.planning.session.attempts == 1


@pytest.mark.parametrize("change", ["selection", "preview", "deleted", "expired"])
def test_stale_responses_are_discarded_and_cannot_restore_planning_state(setup, change):
    local, store, provider, path, preview, record, clock, _ = setup
    provider.entered, provider.release = Event(), Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(plan, local, path, preview)
        try:
            assert provider.entered.wait(timeout=3)
            if change == "selection":
                local.put(path + "/selection", json={"entry_ids": [0]})
            elif change == "preview":
                new = local.post(path + "/model-preview", json={"failure_pattern": "missing_null_fields"}).json()
                assert_safe_error(plan(local, path, new), 409, "planning_consumed")
            elif change == "deleted":
                local.delete(path)
            else:
                clock[0] += CAPTURE_TTL_SECONDS
        finally:
            provider.release.set()
        result = first.result(timeout=3)
        assert_safe_error(result, 404 if change in {"deleted", "expired"} else 409,
                          "capture_not_found" if change in {"deleted", "expired"} else "stale_preview")
    assert len(provider.calls) == 1
    if change in {"deleted", "expired"}:
        assert not store._captures
    else:
        assert record.planning is None and record.planning_consumed


@pytest.mark.parametrize("kind", ["needs_review", "roles", "invalid_check"])
def test_ambiguous_proposals_remain_review_only(setup, kind):
    local, _, provider, path, preview, record, _, _ = setup
    pattern = "inconsistent_identifier_references" if kind == "roles" else "duplicate_pagination"
    if kind == "roles":
        preview = local.post(path + "/model-preview", json={"failure_pattern": pattern}).json()
    arguments = args_for(record.preview.prepared, pattern=pattern)
    arguments["review_reason"] = ""
    if kind == "needs_review":
        arguments.update(needs_review=True, review_reason="Evidence requires human review.", replacements=[])
    if kind == "invalid_check":
        arguments["check"]["refs"] = arguments["check"]["refs"][:1]
    provider.body = response(arguments)
    reply = plan(local, path, preview)
    assert reply.status_code == 200 and reply.json()["verification"] == "not_run"
    if kind == "needs_review":
        assert reply.json()["needs_review"] is True and reply.json()["resolved_replacements"] == []
    else:
        assert reply.json()["check"]["resolution"] == ("definition_reference_roles_required" if kind == "roles" else "invalid_configuration")


def test_changed_local_input_rejects_context_before_network(setup):
    local, _, provider, path, preview, record, _, _ = setup
    record.parsed.transactions[0].response_json["customers"][0]["id"] = 999
    assert_safe_error(plan(local, path, preview), 409, "stale_preview")
    assert provider.calls == [] and not record.planning_consumed


def test_existing_local_service_boundaries_protect_planning(setup):
    local, _, provider, path, preview, _, _, key_reads = setup
    body = {"preview_id": preview["preview_id"], "sha256": preview["sha256"], "approved": True}
    assert_safe_error(local.post(path + "/plan", json=body, headers={"Origin": "http://attacker.example.test"}), 403, "invalid_origin")
    assert_safe_error(local.post(path + "/plan", json=body, headers={"Host": "attacker.example.test"}), 400, "invalid_host")
    assert provider.calls == [] and key_reads == []
