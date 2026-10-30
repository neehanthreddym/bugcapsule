"""Offline capsule export: real pipeline, fabricated fixture, mocked model only."""

from copy import deepcopy
from dataclasses import replace
import http.client
import importlib.util
import json
from pathlib import Path
import socket
import sys
from zipfile import ZipFile

import pytest

from app.agent.execution import ExecutionResult, ExecutionSession, approve_execution
from app.agent import client
from app.agent.workflow import approve_preview
from app.export import (
    FILENAMES, CapsulePreview, ExportError, _har, approve_export, prepare_capsule, write_capsule,
)
from app.replay_template import server
from tests.test_execution import args_for, snapshots, source, transport_for


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Export tests must not use the network.")
    monkeypatch.setattr(http.client, "HTTPSConnection", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)


def verified(transactions=None):
    """Real scrub/replacement/verifier with a fabricated mocked proposal."""
    session = ExecutionSession(source() if transactions is None else transactions, "duplicate_pagination")
    args = args_for(session.prepared, fields=("id", "name", "email", "company"))
    session.request_proposal(approve_preview(session.prepared), api_key="FAKE_TEST_KEY",
                             transport=transport_for([args], []))
    review = session.review_plan()
    return session.execute(approve_execution(review))


def with_request_body(*, same_route=False):
    transactions = source()
    result = []
    for index, tx in enumerate(transactions):
        entry = deepcopy(tx.original_entry)
        request = entry["request"]
        body = {"identifier": 1 if index == 0 else "1", "flag": True, "nullable": None,
                "ordered": [1, "1", True], "password": "FAKE_BODY_PASSWORD_49381"}
        text = json.dumps(body)
        request["postData"] = {"mimeType": "application/json", "text": text}
        request["bodySize"] = len(text.encode("utf-8"))
        request["headers"].append({"name": "Content-Type", "value": "application/json"})
        request["method"] = "POST"
        if same_route:
            request["url"] = transactions[0].url
            request["queryString"] = deepcopy(transactions[0].original_entry["request"]["queryString"])
        result.append(tx.model_copy(update={"method": "POST", "url": request["url"], "original_entry": entry}))
    return result


def test_real_fixture_exact_preview_archive_and_allowlisted_content(tmp_path):
    original = source()
    # Fabricated private metadata that must never enter the package.
    for tx in original:
        tx.original_entry["comment"] = "PRIVATE_COMMENT_47191"
        tx.original_entry["serverIPAddress"] = "PRIVATE_IP_47191"
        tx.original_entry["request"]["headers"].append({"name": "X-Private", "value": "PRIVATE_HEADER_47191"})
    before = snapshots(original)
    result = verified(original)
    preview = prepare_capsule(result)
    assert snapshots(original) == before
    for tx in original:
        for customer in tx.response_json["customers"]:
            for field in ("name", "email", "company"):
                assert customer[field] not in preview.preview_json
    assert list(preview.contents) == list(FILENAMES)
    assert json.loads(preview.preview_json) == preview.contents
    assert len(preview.digest) == 64
    assert not list(tmp_path.iterdir())  # All preparation/review is memory only.
    contents = preview.contents
    contents["transformed.har"] = "local copy mutation"
    assert preview.contents["transformed.har"] != contents["transformed.har"]
    har = json.loads(preview.contents["transformed.har"])
    for packaged, tx in zip(har["log"]["entries"], result.candidate, strict=True):
        request, response = packaged["request"], packaged["response"]
        assert request["url"].startswith("https://replay.example.test/")
        assert request["headers"] == response["headers"] == []
        assert "cookies" not in request and "cookies" not in response
        assert json.loads(response["content"]["text"]) == tx.response_json
        assert response["bodySize"] == response["content"]["size"] == len(response["content"]["text"].encode("utf-8"))
        assert request["bodySize"] == 0
    combined = preview.preview_json
    for private in ("api.example.test", "BUGCAPSULE_FAKE_TOKEN", "BUGCAPSULE_FAKE_SESSION", "PRIVATE_COMMENT_47191",
                    "PRIVATE_IP_47191", "PRIVATE_HEADER_47191", "Maya Chen", "maya.chen@example.test",
                    "original_entry", "private_bindings", "FABRICATED_MODEL_PROSE"):
        assert private not in combined
    report = json.loads(preview.contents["verification.json"])
    assert report["outcome"] == "preserved" and report["reason"] == "failure_preserved"
    assert report["baseline"]["failures"] == report["transformed"]["failures"] == 1
    assert report["transformed"]["equality_groups"][0][0]["record"] == [2]
    assert "customers" not in preview.contents["verification.json"]
    assert "anonymization" in preview.contents["README.md"]
    assert "python3 server.py --port 8000" in preview.contents["README.md"]
    assert "page=1&page_size=3" in preview.contents["README.md"]
    assert preview.contents["server.py"].encode() == Path(server.__file__).read_bytes()
    output = tmp_path / "capsule.zip"
    assert write_capsule(preview, approve_export(preview), output) == output
    with ZipFile(output) as archive:
        assert archive.namelist() == list(FILENAMES)
        for info in archive.infolist():
            assert info.filename in FILENAMES and not info.is_dir()
            assert (info.external_attr >> 16) & 0o170000 == 0o100000
            assert archive.read(info.filename) == preview.contents[info.filename].encode("utf-8")


@pytest.mark.parametrize("state", ["stopped", "cancelled", "revision_available", "review_required"])
def test_non_successful_results_rejected(state):
    with pytest.raises(ExportError):
        prepare_capsule(replace(verified(), status=state))


@pytest.mark.parametrize("outcome", ["changed", "inconclusive", "not_present"])
def test_invalid_verification_outcomes_rejected(outcome):
    result = verified()
    with pytest.raises(ExportError):
        prepare_capsule(replace(result, verification=result.verification.model_copy(update={"outcome": outcome})))


@pytest.mark.parametrize("part", ["json", "retained", "report", "candidate", "unissued", "absent", "no_report"])
def test_seal_rejects_later_mutation_mismatches_and_unissued_results(part):
    result = verified()
    if part == "json":
        result.candidate[0].response_json["customers"][0]["id"] = 7
    elif part == "retained":
        result.candidate[0].original_entry["request"]["url"] = "https://private.example.test/changed"
    elif part == "report":
        result = replace(result, verification=result.verification.model_copy(update={"reason": "invalid_input"}))
    elif part == "candidate":
        result = replace(result, candidate=verified(with_request_body()).candidate)
    elif part == "unissued":
        result = ExecutionResult(result.status, result.reason, result.attempts, result.verification, result.candidate)
    elif part == "absent":
        result = None
    elif part == "no_report":
        result = replace(result, verification=None)
    with pytest.raises(ExportError) as error:
        prepare_capsule(result)
    assert "private.example.test" not in str(error.value)


def test_snapshot_detached_from_later_candidate_mutation(tmp_path):
    result = verified()
    preview = prepare_capsule(result)
    approval = approve_export(preview)
    before = preview.contents
    result.candidate[0].response_json.clear()
    write_capsule(preview, approval, tmp_path / "unchanged.zip")
    assert preview.contents == before
    with pytest.raises(ExportError):
        prepare_capsule(result)


def test_approval_tampering_and_stale_snapshot_create_no_directories(tmp_path):
    preview = prepare_capsule(verified())
    approval = approve_export(preview)
    output = tmp_path / "not-created" / "capsule.zip"
    changed = replace(preview, _files=preview._files[:-1] + (("README.md", b"Changed bytes"),))
    for snapshot, permission in ((preview, None), (changed, approval), (prepare_capsule(verified()), approval)):
        with pytest.raises(ExportError, match="approval"):
            write_capsule(snapshot, permission, output)
        assert not output.parent.exists()
    with pytest.raises(ExportError):
        approve_export(CapsulePreview((("../private", b"no"),)))


def test_refuses_overwrite_and_existing_symlink(tmp_path):
    preview = prepare_capsule(verified())
    approval = approve_export(preview)
    path = tmp_path / "existing.zip"
    path.write_bytes(b"existing bytes")
    link = tmp_path / "link.zip"
    link.symlink_to(path)
    for output in (path, link):
        with pytest.raises(ExportError, match="overwrite"):
            write_capsule(preview, approval, output)
    assert path.read_bytes() == b"existing bytes"


def test_ambiguous_normalized_routes_rejected_during_preparation():
    txs = source()
    entry = deepcopy(txs[1].original_entry)
    entry["request"]["url"] = "https://api.example.test/v1/customers?page_size=3&page=1"
    entry["request"]["queryString"] = [{"name": "page_size", "value": "3"}, {"name": "page", "value": "1"}]
    txs[1] = txs[1].model_copy(update={"url": entry["request"]["url"], "original_entry": entry})
    result = verified(txs)
    assert result.status == "preserved"
    with pytest.raises(ExportError):
        prepare_capsule(result)


def test_supported_request_json_scrubbed_and_size_consistent():
    preview = prepare_capsule(verified(with_request_body(same_route=True)))
    entries = json.loads(preview.contents["transformed.har"])["log"]["entries"]
    assert "FAKE_BODY_PASSWORD_49381" not in preview.preview_json
    for entry in entries:
        request = entry["request"]
        text = request["postData"]["text"]
        assert request["bodySize"] == len(text.encode("utf-8"))
        assert "password" not in json.loads(text)
    assert len(server.build_routes({"log": {"entries": entries}})) == 2


def test_form_body_rejected_instead_of_passed_unchecked():
    transactions = source()
    for tx in transactions:
        request = tx.original_entry["request"]
        request["postData"] = {"mimeType": "application/x-www-form-urlencoded", "text": "safe=value"}
        request["bodySize"] = 10
    with pytest.raises(ExportError):
        _har(transactions)


def driver():
    path = Path(__file__).resolve().parents[1] / "scripts" / "plan_fixture.py"
    spec = importlib.util.spec_from_file_location("export_fixture_driver_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("answer, expected", [("", 1), ("EXPORT", 0)])
def test_driver_export_confirmation_is_separate_and_cancellation_writes_nothing(tmp_path, monkeypatch, capsys, answer, expected):
    module = driver()
    destination = tmp_path / "local" / "capsules"
    monkeypatch.setattr(module, "CAPSULE_DIR", destination)
    monkeypatch.setattr("builtins.input", lambda _: answer)
    assert module._export_fixture(verified()) == expected
    output = capsys.readouterr().out
    assert "Exact capsule files" in output and "Capsule SHA-256" in output
    assert destination.exists() == (answer == "EXPORT")
    if destination.exists():
        assert len(list(destination.glob("*.zip"))) == 1


def test_driver_export_requires_execute_and_retains_execute_only_default(monkeypatch):
    module = driver()
    monkeypatch.setattr(sys, "argv", ["plan_fixture.py", "--export"])
    assert module.main() == 1
    calls = []
    monkeypatch.setattr(module, "_execute_fixture", lambda **kwargs: calls.append(kwargs) or 0)
    for args in (["--execute"], ["--execute", "--export"], ["--export", "--execute"]):
        monkeypatch.setattr(sys, "argv", ["plan_fixture.py", *args])
        assert module.main() == 0
    assert calls == [{"export": False}, {"export": True}, {"export": True}]


@pytest.mark.parametrize("last_answer", ["EXPORT", "cancel"])
def test_full_mocked_execute_export_driver_has_three_separate_approvals(tmp_path, monkeypatch, capsys, last_answer):
    module = driver()
    monkeypatch.setattr(module, "CAPSULE_DIR", tmp_path / "capsules")
    prepared = ExecutionSession(source(), "duplicate_pagination", approved_labels=module.APPROVED_LABELS).prepared
    calls = []
    transport = transport_for([args_for(prepared, fields=("id", "name", "email", "company"))], calls)
    monkeypatch.setattr(client, "_send_via_http", lambda body, key: transport(body))
    monkeypatch.setenv("META_API_KEY", "FAKE_TEST_KEY")
    monkeypatch.setattr(sys, "argv", ["plan_fixture.py", "--execute", "--export"])
    answers = iter(["APPROVE", "EXECUTE", last_answer])
    prompts = []

    def confirm(prompt):
        prompts.append(prompt)
        return next(answers)

    monkeypatch.setattr("builtins.input", confirm)
    assert module.main() == (0 if last_answer == "EXPORT" else 1)
    assert len(calls) == 1 and len(prompts) == 3
    assert [prompt.split()[1] for prompt in prompts] == ["APPROVE", "EXECUTE", "EXPORT"]
    assert (tmp_path / "capsules").exists() == (last_answer == "EXPORT")
    assert '"status": "preserved"' in capsys.readouterr().out
