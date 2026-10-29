"""Offline agent tests with injected mock transport; never live network calls."""

from copy import deepcopy
import http.client
import importlib.util
import json
from pathlib import Path
import socket
import sys
from types import SimpleNamespace
import urllib.request

import pytest

from app.agent import client as agent_client
from app.agent.client import (
    AgentError,
    AgentHttpError,
    AgentIncompleteResponseError,
    AgentResponseError,
    AgentTransportError,
)
from app.agent.workflow import (
    Approval,
    ApprovalError,
    PlanValidationError,
    approve_preview,
    request_capsule_plan,
)
from app.config import ConfigError, get_meta_api_key
from app.engine.har import parse_har, select_transactions
from app.engine import replace as replace_engine
from app.privacy import prepare_model_payload


FIXTURE_PATH = Path(__file__).resolve().parents[2] / "fixtures" / "duplicate-pagination.har"
FAKE_KEY = "TEST_FAKE_KEY_12345"
BANNED_ORIGINALS = [
    "Maya Chen",
    "Owen Patel",
    "BUGCAPSULE_FAKE_TOKEN_DO_NOT_USE",
    "BUGCAPSULE_FAKE_SESSION",
    "api.example.test",
    "app.example.test",
    "https://",
    "original_entry",
    "PrivateBindings",
    "bindings",
]


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Network access is forbidden in agent tests.")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(http.client, "HTTPSConnection", forbidden)


def fixture_source():
    return select_transactions(parse_har(FIXTURE_PATH), [0, 1])


def prepared_fixture(approved_labels=frozenset()):
    return prepare_model_payload(
        fixture_source(), "duplicate_pagination", approved_labels=approved_labels
    )


def local_field(prepared, node, key):
    for field in node.get("fields", []):
        binding = prepared.bindings.resolve(field["node"]["ref"])
        if getattr(binding.path[-1], "key", None) == key:
            return field
    raise AssertionError("Expected local field was not found.")


def leaf_refs(prepared):
    payload = prepared.payload
    refs = {}
    for index, entry in enumerate(payload["transactions"]):
        rows = local_field(prepared, entry["response_json"], "customers")["node"]
        items = rows["items"]
        for key in ("id", "name", "email"):
            refs[f"{key}_{index}"] = local_field(prepared, items, key)["node"]["ref"]
    refs["root_0"] = payload["transactions"][0]["response_json"]["ref"]
    refs["rows_0"] = local_field(
        prepared, payload["transactions"][0]["response_json"], "customers"
    )["node"]["ref"]
    return refs


def valid_args(refs):
    return {
        "replacements": [
            {"ref": refs["id_0"], "kind": "id"},
            {"ref": refs["id_1"], "kind": "id"},
            {"ref": refs["name_0"], "kind": "name"},
        ],
        "relationships": [
            {"refs": [refs["id_0"], refs["id_1"]], "reason": "Same entity across pages."}
        ],
        "check": {
            "type": "duplicate_pagination",
            "refs": [refs["id_0"], refs["id_1"]],
            "note": "Repeated record across pages.",
        },
        "needs_review": False,
        "review_reason": "",
    }


def envelope(output, *, status="completed", usage=None):
    body = {"status": status, "model": "muse-spark-1.3", "output": output}
    if usage is not None:
        body["usage"] = usage
    return body


def call_item(arguments, *, name="propose_capsule_plan"):
    payload = arguments if isinstance(arguments, str) else json.dumps(arguments)
    return {
        "type": "function_call",
        "name": name,
        "arguments": payload,
        "call_id": "call_123",
    }


def mock_transport(calls, status, body):
    raw = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")

    def _transport(request_body):
        calls.append(deepcopy(request_body))
        return status, raw

    return _transport


def test_successful_parsing_and_local_resolution(monkeypatch):
    monkeypatch.setattr(
        replace_engine, "apply_replacements", lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("Replacements must not execute.")
        ),
    )
    source = fixture_source()
    before = [(deepcopy(tx.model_dump()), deepcopy(tx.original_entry)) for tx in source]
    prepared = prepare_model_payload(source, "duplicate_pagination")
    preview_before = prepared.preview_json
    approval = approve_preview(prepared)
    refs = leaf_refs(prepared)
    args = valid_args(refs)
    calls: list = []
    transport = mock_transport(
        calls,
        200,
        envelope(
            [call_item(args)],
            usage={"input_tokens": 120, "output_tokens": 60, "total_tokens": 180},
        ),
    )
    proposal = request_capsule_plan(prepared, approval, api_key=FAKE_KEY, transport=transport)
    assert len(calls) == 1
    assert proposal.failure_pattern == "duplicate_pagination"
    assert proposal.usage == {"input_tokens": 120, "output_tokens": 60, "total_tokens": 180}
    assert [item.ref for item in proposal.args.replacements] == [
        refs["id_0"], refs["id_1"], refs["name_0"],
    ]
    assert proposal.replacement_plan is not None
    assert len(proposal.replacement_plan.rules) == 2
    grouped = {
        (rule.target, rule.kind): sorted(rule.entry_ids)
        for rule in proposal.replacement_plan.rules
    }
    assert grouped[("response_json", "id")] == [0, 1]
    assert grouped[("response_json", "name")] == [0]
    request_body = calls[0]
    assert request_body["input"] == preview_before
    assert request_body["model"] == "muse-spark-1.3"
    assert request_body["store"] is False
    assert request_body["tool_choice"] == "auto"
    assert request_body["parallel_tool_calls"] is False
    assert request_body["max_output_tokens"] == 4096
    assert request_body["reasoning"] == {"effort": "low"}
    assert prepared.preview_json == preview_before
    assert approval.preview_json == preview_before
    assert [(deepcopy(tx.model_dump()), deepcopy(tx.original_entry)) for tx in source] == before


def test_missing_approval_or_changed_snapshot_zero_requests():
    prepared = prepared_fixture()
    other = prepared_fixture(approved_labels=frozenset({"id"}))
    other_approval = approve_preview(other)
    calls: list = []
    transport = mock_transport(calls, 200, envelope([call_item(valid_args(leaf_refs(prepared)))]))
    with pytest.raises(ApprovalError):
        request_capsule_plan(prepared, None, api_key=FAKE_KEY, transport=transport)
    with pytest.raises(ApprovalError):
        request_capsule_plan(prepared, other_approval, api_key=FAKE_KEY, transport=transport)
    with pytest.raises(ApprovalError):
        request_capsule_plan(
            prepared, Approval(preview_json="tampered"), api_key=FAKE_KEY, transport=transport
        )
    assert calls == []


def test_outbound_request_exact_snapshot_and_no_private():
    prepared = prepared_fixture(approved_labels=frozenset({"id"}))
    approval = approve_preview(prepared)
    refs = leaf_refs(prepared)
    calls: list = []
    transport = mock_transport(calls, 200, envelope([call_item(valid_args(refs))]))
    request_capsule_plan(prepared, approval, api_key=FAKE_KEY, transport=transport)
    assert len(calls) == 1
    request_body = calls[0]
    assert request_body["input"] == prepared.preview_json
    assert json.loads(request_body["input"]) == prepared.payload
    assert len(request_body["tools"]) == 1
    tool = request_body["tools"][0]
    assert tool["type"] == "function"
    assert tool["name"] == "propose_capsule_plan"
    assert set(tool) == {"type", "name", "description", "parameters"}
    text = json.dumps(request_body, ensure_ascii=True)
    for banned in BANNED_ORIGINALS:
        assert banned not in text
    for original_id in ("101", "102", "103", "104", "105"):
        assert original_id not in text
    # The key travels only in the Authorization header, never the JSON body.
    assert "Authorization" not in text
    assert FAKE_KEY not in text


def test_needs_review_with_empty_replacements_builds_no_plan():
    prepared = prepared_fixture()
    approval = approve_preview(prepared)
    refs = leaf_refs(prepared)
    args = valid_args(refs)
    args["replacements"] = []
    args["needs_review"] = True
    args["review_reason"] = "Insufficient evidence for confident replacements."
    calls: list = []
    transport = mock_transport(calls, 200, envelope([call_item(args)]))
    proposal = request_capsule_plan(prepared, approval, api_key=FAKE_KEY, transport=transport)
    assert proposal.replacement_plan is None
    assert proposal.args.needs_review is True
    assert len(calls) == 1


@pytest.mark.parametrize("mutate", [
    "unknown_tool",
    "multiple_calls",
    "malformed_json",
    "extra_fields",
    "invented_ref",
    "root_target",
    "object_target",
    "integer_wrong_kind",
    "conflicting_kinds",
    "check_mismatch",
    "empty_confident",
    "needs_review_no_reason",
])
def test_invalid_tool_calls_rejected(mutate):
    prepared = prepared_fixture()
    approval = approve_preview(prepared)
    refs = leaf_refs(prepared)
    args = valid_args(refs)
    if mutate == "unknown_tool":
        output = [call_item(args, name="other_tool")]
    elif mutate == "multiple_calls":
        output = [call_item(args), call_item(args)]
    elif mutate == "malformed_json":
        output = [call_item("not json{")]
    elif mutate == "extra_fields":
        args["extra"] = "nope"
        output = [call_item(args)]
    elif mutate == "invented_ref":
        args["replacements"][0]["ref"] = "node_9999"
        output = [call_item(args)]
    elif mutate == "root_target":
        args["replacements"] = [{"ref": refs["root_0"], "kind": "id"}]
        output = [call_item(args)]
    elif mutate == "object_target":
        args["replacements"] = [{"ref": refs["rows_0"], "kind": "text"}]
        output = [call_item(args)]
    elif mutate == "integer_wrong_kind":
        args["replacements"] = [{"ref": refs["id_0"], "kind": "email"}]
        output = [call_item(args)]
    elif mutate == "conflicting_kinds":
        args["replacements"] = [
            {"ref": refs["id_0"], "kind": "id"},
            {"ref": refs["id_0"], "kind": "text"},
        ]
        output = [call_item(args)]
    elif mutate == "check_mismatch":
        args["check"]["type"] = "missing_null_fields"
        output = [call_item(args)]
    elif mutate == "empty_confident":
        args["replacements"] = []
        output = [call_item(args)]
    else:
        args["needs_review"] = True
        args["review_reason"] = "  "
        output = [call_item(args)]
    calls: list = []
    transport = mock_transport(calls, 200, envelope(output))
    with pytest.raises((AgentResponseError, PlanValidationError)):
        request_capsule_plan(prepared, approval, api_key=FAKE_KEY, transport=transport)
    assert len(calls) == 1


def test_refusal_plain_text_incomplete_and_http_failures():
    prepared = prepared_fixture()
    approval = approve_preview(prepared)
    refusal = envelope([{
        "type": "message",
        "role": "assistant",
        "content": [{"type": "refusal", "refusal": "bad"}],
    }])
    plain = envelope([{
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "hello"}],
    }])
    incomplete = envelope([], status="incomplete")
    cases = [
        (200, refusal, AgentResponseError),
        (200, plain, AgentResponseError),
        (200, incomplete, AgentResponseError),
        (500, envelope([]), AgentHttpError),
        (429, envelope([]), AgentHttpError),
    ]
    for status, body, error in cases:
        calls: list = []
        transport = mock_transport(calls, status, body)
        with pytest.raises(error):
            request_capsule_plan(prepared, approval, api_key=FAKE_KEY, transport=transport)
        assert len(calls) == 1


def test_response_too_large_and_malformed_envelope():
    prepared = prepared_fixture()
    approval = approve_preview(prepared)
    calls: list = []
    transport = mock_transport(calls, 200, b"x" * (65_536 + 1))
    with pytest.raises(AgentResponseError):
        request_capsule_plan(prepared, approval, api_key=FAKE_KEY, transport=transport)
    assert len(calls) == 1
    calls.clear()
    transport = mock_transport(calls, 200, b"not json")
    with pytest.raises(AgentResponseError):
        request_capsule_plan(prepared, approval, api_key=FAKE_KEY, transport=transport)
    assert len(calls) == 1


def test_client_rejects_private_objects_without_network():
    prepared = prepared_fixture()
    calls: list = []

    def _transport(request_body):
        calls.append(request_body)
        return 200, b"{}"

    for bad_snapshot in (prepared, prepared.bindings, prepared.payload, None, b"{}", 123):
        with pytest.raises(AgentError):
            agent_client.request_tool_call(bad_snapshot, api_key=FAKE_KEY, transport=_transport)
    source = fixture_source()
    with pytest.raises(AgentError):
        agent_client.request_tool_call(source[0], api_key=FAKE_KEY, transport=_transport)
    assert calls == []
    with pytest.raises(AgentError):
        agent_client.request_tool_call(prepared.preview_json, api_key="", transport=_transport)
    assert calls == []


def test_no_input_mutation_and_no_transport_retry():
    prepared = prepared_fixture()
    approval = approve_preview(prepared)
    preview_before = prepared.preview_json
    payload_before = prepared.payload
    calls: list = []
    transport = mock_transport(calls, 500, envelope([]))
    with pytest.raises(AgentHttpError):
        request_capsule_plan(prepared, approval, api_key=FAKE_KEY, transport=transport)
    assert len(calls) == 1
    assert prepared.preview_json == preview_before
    assert prepared.payload == payload_before
    assert approval.preview_json == preview_before


def test_config_key_loading_uses_fake_env_only(monkeypatch):
    monkeypatch.setenv("META_API_KEY", FAKE_KEY)
    assert get_meta_api_key() == FAKE_KEY
    monkeypatch.setenv("META_API_KEY", "   ")
    with pytest.raises(ConfigError):
        get_meta_api_key()
    monkeypatch.setenv("META_API_KEY", "has space")
    with pytest.raises(ConfigError):
        get_meta_api_key()
    monkeypatch.delenv("META_API_KEY", raising=False)
    with pytest.raises(ConfigError):
        get_meta_api_key()


def test_transport_error_is_explicit():
    prepared = prepared_fixture()
    approval = approve_preview(prepared)

    def _failing(request_body):
        raise TimeoutError

    with pytest.raises(AgentTransportError):
        request_capsule_plan(prepared, approval, api_key=FAKE_KEY, transport=_failing)


DIAGNOSTIC_SECRET = "FABRICATED_PROVIDER_SECRET_DO_NOT_LOG"


def incomplete_envelope(*, details=None, usage=None):
    body = envelope([
        call_item('{"partial": "' + DIAGNOSTIC_SECRET),
        {"type": "message", "content": [{"type": "output_text", "text": DIAGNOSTIC_SECRET}]},
    ], status="incomplete", usage=usage)
    if details is not None:
        body["incomplete_details"] = details
    body["error"] = {"message": DIAGNOSTIC_SECRET, "code": DIAGNOSTIC_SECRET}
    return body


def capture_incomplete(body):
    calls = []
    with pytest.raises(AgentIncompleteResponseError, match="^Incomplete response\\.$") as caught:
        agent_client.request_tool_call(
            '{"synthetic":true}', api_key=FAKE_KEY,
            transport=mock_transport(calls, 200, body),
        )
    assert len(calls) == 1
    assert calls[0]["max_output_tokens"] == 4096
    assert calls[0]["reasoning"] == {"effort": "low"}
    assert DIAGNOSTIC_SECRET not in str(caught.value)
    assert DIAGNOSTIC_SECRET not in repr(caught.value.diagnostics)
    assert FAKE_KEY not in repr(caught.value.diagnostics)
    assert caught.value.args == ("Incomplete response.",)
    return caught.value.diagnostics.as_dict()


def test_token_limit_incompletion_preserves_diagnostics_without_reading_arguments(monkeypatch):
    prepared = prepared_fixture()
    approval = approve_preview(prepared)
    preview_before = prepared.preview_json
    calls = []

    def forbidden(*args, **kwargs):
        raise AssertionError("Incomplete output must not be parsed or applied.")

    monkeypatch.setattr(agent_client, "_single_arguments", forbidden)
    monkeypatch.setattr(replace_engine, "apply_replacements", forbidden)
    body = incomplete_envelope(details={"reason": "max_output_tokens", "debug": DIAGNOSTIC_SECRET}, usage={
        "input_tokens": 1200, "output_tokens": 4096, "total_tokens": 5296,
        "output_tokens_details": {"reasoning_tokens": 4094, "debug": DIAGNOSTIC_SECRET},
        "provider_note": DIAGNOSTIC_SECRET,
    })
    with pytest.raises(AgentIncompleteResponseError) as caught:
        request_capsule_plan(
            prepared, approval, api_key=FAKE_KEY,
            transport=mock_transport(calls, 200, body),
        )
    assert len(calls) == 1
    assert caught.value.diagnostics.as_dict() == {
        "status": "incomplete", "reason": "max_output_tokens", "max_output_tokens": 4096,
        "input_tokens": 1200, "output_tokens": 4096, "total_tokens": 5296,
        "reasoning_tokens": 4094,
    }
    assert DIAGNOSTIC_SECRET not in repr(caught.value.diagnostics)
    assert prepared.preview_json == approval.preview_json == preview_before


@pytest.mark.parametrize("details, expected", [
    (None, "unknown"), ({}, "unknown"), ([], "unknown"),
    (DIAGNOSTIC_SECRET, "unknown"), ({"reason": None}, "unknown"),
    ({"reason": []}, "unknown"), ({"reason": True}, "unknown"),
    ({"reason": DIAGNOSTIC_SECRET}, "unknown"),
    ({"reason": "MAX_OUTPUT_TOKENS"}, "unknown"),
    ({"reason": "max_output_tokens"}, "max_output_tokens"),
    ({"reason": "content_filter"}, "content_filter"),
])
def test_incomplete_reason_is_an_exact_allowlist(details, expected):
    diagnostics = capture_incomplete(incomplete_envelope(details=details))
    assert diagnostics == {"status": "incomplete", "reason": expected, "max_output_tokens": 4096}


@pytest.mark.parametrize("usage", [None, {}, [], DIAGNOSTIC_SECRET])
def test_absent_or_malformed_incomplete_usage_is_omitted(usage):
    assert capture_incomplete(incomplete_envelope(usage=usage)) == {
        "status": "incomplete", "reason": "unknown", "max_output_tokens": 4096,
    }


@pytest.mark.parametrize("bad_count", [None, True, False, -1, 1.5, "2048", [], {}, 10_000_001])
def test_incomplete_counts_reject_invalid_numbers(bad_count):
    usage = {"input_tokens": bad_count, "output_tokens": bad_count, "total_tokens": bad_count,
             "output_tokens_details": {"reasoning_tokens": bad_count}}
    assert capture_incomplete(incomplete_envelope(usage=usage)) == {
        "status": "incomplete", "reason": "unknown", "max_output_tokens": 4096,
    }


@pytest.mark.parametrize("count", [0, 10_000_000])
def test_incomplete_count_boundaries_are_preserved(count):
    usage = {"input_tokens": count, "output_tokens": count, "total_tokens": count,
             "output_tokens_details": {"reasoning_tokens": count}}
    assert capture_incomplete(incomplete_envelope(usage=usage)) == {
        "status": "incomplete", "reason": "unknown", "max_output_tokens": 4096,
        "input_tokens": count, "output_tokens": count, "total_tokens": count,
        "reasoning_tokens": count,
    }


@pytest.mark.parametrize("output_details, reasoning", [
    (None, {}), ([], {}), (DIAGNOSTIC_SECRET, {}),
    ({"reasoning_tokens": 32, "debug": DIAGNOSTIC_SECRET}, {"reasoning_tokens": 32}),
])
def test_incomplete_usage_retains_independently_valid_fields(output_details, reasoning):
    usage = {"input_tokens": 120, "output_tokens": DIAGNOSTIC_SECRET, "total_tokens": True,
             "output_tokens_details": output_details, "extra": DIAGNOSTIC_SECRET}
    assert capture_incomplete(incomplete_envelope(usage=usage)) == {
        "status": "incomplete", "reason": "unknown", "max_output_tokens": 4096,
        "input_tokens": 120, **reasoning,
    }


@pytest.mark.parametrize("details, usage, expected", [
    ({"reason": "max_output_tokens"}, {
        "input_tokens": 1200, "output_tokens": 4096, "total_tokens": 5296,
        "output_tokens_details": {"reasoning_tokens": 4094},
    }, {"status": "incomplete", "reason": "max_output_tokens", "max_output_tokens": 4096,
        "input_tokens": 1200, "output_tokens": 4096, "total_tokens": 5296,
        "reasoning_tokens": 4094}),
    ({"reason": DIAGNOSTIC_SECRET}, {"input_tokens": DIAGNOSTIC_SECRET},
     {"status": "incomplete", "reason": "unknown", "max_output_tokens": 4096}),
])
def test_driver_incomplete_output_is_allowlisted_and_still_fails(monkeypatch, capsys, details, usage, expected):
    script = Path(__file__).resolve().parents[1] / "scripts" / "plan_fixture.py"
    monkeypatch.setattr(sys, "path", list(sys.path))
    spec = importlib.util.spec_from_file_location("plan_fixture_diagnostics_test", script)
    driver = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(driver)
    calls = []
    transport = mock_transport(calls, 200, incomplete_envelope(details=details, usage=usage))

    def request_offline(prepared, approval):
        return request_capsule_plan(prepared, approval, api_key=FAKE_KEY, transport=transport)

    monkeypatch.setattr(driver, "request_capsule_plan", request_offline)
    monkeypatch.setattr(sys, "argv", [str(script)])
    monkeypatch.setattr("builtins.input", lambda _prompt: "APPROVE")
    clock = iter([0.0, 8.623])
    monkeypatch.setattr(driver, "time", SimpleNamespace(perf_counter=lambda: next(clock)))
    assert driver.main() == 1
    output = capsys.readouterr()
    assert output.err == ""
    # The existing pre-send approval preview is unchanged; the failure path
    # must never repeat that snapshot or print request/response contents.
    failure = "Safe failure after" + output.out.split("Safe failure after", 1)[1]
    assert failure == (
        "Safe failure after 8623 ms: Incomplete response.\n"
        "Incomplete diagnostics: " + json.dumps(expected, sort_keys=True) + "\n"
        "No transformations applied. No verification run. No export.\n"
    )
    assert len(calls) == 1 and calls[0]["max_output_tokens"] == 4096
    for forbidden in (DIAGNOSTIC_SECRET, FAKE_KEY, "arguments", "instructions", "tools", "transactions"):
        assert forbidden not in failure
    assert DIAGNOSTIC_SECRET not in output.out
