"""Offline label review and exact model-preview DTOs; no approval or model calls."""

from copy import deepcopy
import hashlib
import http.client
import json
from pathlib import Path
import socket
import urllib.request

from fastapi.testclient import TestClient
import pytest

from app import privacy
from app.agent import client as agent_client, execution, workflow
from app.main import create_app
from app.services import capture_store
from app.services.capture_store import CAPTURE_TTL_SECONDS, CaptureStore
from tests.test_capture_api import assert_error, upload


FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "duplicate-pagination.har"
PRIVATE = "FABRICATED_PREVIEW_PRIVATE_837191"
LABELS = sorted({"customers", "id", "name", "email", "company", "account_status", "created_at", "updated_at",
                 "pagination", "page", "page_size", "has_more"})


@pytest.fixture(autouse=True)
def no_network_or_approval(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Preview preparation cannot request network, planning, execution or approval.")
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(http.client, "HTTPSConnection", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(agent_client, "_send_via_http", forbidden)
    monkeypatch.setattr(workflow, "request_capsule_plan", forbidden)
    monkeypatch.setattr(workflow, "approve_preview", forbidden)
    monkeypatch.setattr(execution, "ExecutionSession", forbidden)
    monkeypatch.setattr(execution, "approve_execution", forbidden)


@pytest.fixture
def client():
    with TestClient(create_app(), base_url="http://127.0.0.1:8000") as local:
        yield local


def confirmed(client, data=None):
    response = upload(client, data)
    assert response.status_code == 201
    capture_id = response.json()["capture_id"]
    assert client.put(f"/api/captures/{capture_id}/selection", json={"entry_ids": [0, 1]}).status_code == 200
    return f"/api/captures/{capture_id}"


def prepare(client, path, **settings):
    return client.post(path + "/model-preview", json={"failure_pattern": "duplicate_pagination", **settings})


def all_fields(payload):
    def visit(node):
        for field in node.get("fields", []):
            yield field["label"]
            yield from visit(field["node"])
        if "items" in node:
            yield from visit(node["items"])
    for entry in payload["transactions"]:
        for target in ("query", "response_json", "request_json"):
            if target in entry:
                yield from visit(entry[target])


def assert_restricted(text):
    for forbidden in (PRIVATE, "BUGCAPSULE_FAKE_TOKEN_DO_NOT_USE", "BUGCAPSULE_FAKE_SESSION", "api.example.test",
                      "app.example.test", "Authorization", "Cookie", '"path":', '"key":',
                      "original_entry", "PrivateBindings", "startedDateTime", "timings"):
        assert forbidden not in text
    for entry in json.loads(FIXTURE.read_bytes())["log"]["entries"]:
        for customer in json.loads(entry["response"]["content"]["text"])["customers"]:
            for key, value in customer.items():
                if isinstance(value, str):
                    assert value not in text


def test_default_aliases_exact_snapshot_digest_and_private_retention(client, monkeypatch):
    path = confirmed(client)
    capture_id = path.rsplit("/", 1)[1]
    record = client.app.state.capture_store._captures[capture_id]
    before = [(deepcopy(tx.original_entry), deepcopy(tx.response_json)) for tx in record.parsed.transactions]
    choices = client.get(path + "/schema-labels")
    assert choices.status_code == 200 and choices.json() == {"labels": LABELS}
    assert choices.headers["Cache-Control"] == "no-store"
    assert record.preview is None  # Offering names grants no label/send approval.
    assert_restricted(choices.text)
    built = []
    real_prepare = capture_store.prepare_model_payload

    def builder(*args, **kwargs):
        result = real_prepare(*args, **kwargs)
        built.append(result)
        return result

    monkeypatch.setattr(capture_store, "prepare_model_payload", builder)
    response = prepare(client, path)
    assert response.status_code == 200 and response.headers["Cache-Control"] == "no-store"
    dto = response.json()
    assert set(dto) == {"preview_id", "preview_json", "byte_count", "sha256", "metadata"}
    assert len(dto["preview_id"]) >= 32
    assert dto["byte_count"] == len(dto["preview_json"].encode("utf-8"))
    assert dto["sha256"] == hashlib.sha256(dto["preview_json"].encode("utf-8")).hexdigest()
    payload = json.loads(dto["preview_json"])
    assert dto["metadata"] == payload["metadata"]
    assert all(label.startswith("field_") for label in all_fields(payload))
    assert record.preview.prepared is built[0]
    assert record.preview.prepared.preview_json == dto["preview_json"]
    assert record.preview.preview_id == dto["preview_id"]
    assert record.preview.entry_ids == (0, 1)
    assert record.preview.failure_pattern == "duplicate_pagination" and record.preview.approved_labels == frozenset()
    assert repr(record.preview.prepared) == "PreparedPayload()"
    assert_restricted(response.text)
    assert [(tx.original_entry, tx.response_json) for tx in record.parsed.transactions] == before
    summary = client.get(path).json()
    assert "preview" not in summary and "bindings" not in json.dumps(summary)


@pytest.mark.parametrize("pattern", ["duplicate_pagination", "missing_null_fields", "inconsistent_identifier_references"])
def test_explicit_labels_and_supported_patterns(client, pattern):
    path = confirmed(client)
    assert client.get(path + "/schema-labels").status_code == 200
    response = prepare(client, path, failure_pattern=pattern, approved_labels=["id", "customers"])
    assert response.status_code == 200
    payload = json.loads(response.json()["preview_json"])
    labels = set(all_fields(payload))
    assert {"id", "customers"} <= labels and "email" not in labels and "name" not in labels
    assert payload["failure_pattern"] == pattern
    assert_restricted(response.text)


@pytest.mark.parametrize("settings", [
    {"failure_pattern": "inconsistent_references"}, {"failure_pattern": PRIVATE}, {"failure_pattern": None},
    {"approved_labels": ["id", "id"]}, {"approved_labels": [PRIVATE]}, {"approved_labels": ["Authorization"]},
    {"approved_labels": ["id"] * 65}, {"approved_labels": [1]}, {"approved_labels": "id"},
    {"approved_labels": None}, {"extra": PRIVATE},
])
def test_invalid_patterns_approvals_and_request_schemas_are_value_free(client, settings):
    path = confirmed(client)
    response = prepare(client, path, **settings)
    assert_error(response, 422, "invalid_preview_settings")
    assert PRIVATE not in response.text


def test_missing_pattern_and_malformed_json_have_safe_errors(client):
    path = confirmed(client)
    assert_error(client.post(path + "/model-preview", json={}), 422, "invalid_preview_settings")
    assert_error(client.post(path + "/model-preview", content=PRIVATE, headers={"Content-Type": "application/json"}),
                 422, "invalid_preview_settings")


def test_both_endpoints_require_confirmed_selection(client):
    path = "/api/captures/" + upload(client).json()["capture_id"]
    assert_error(client.get(path + "/schema-labels"), 409, "selection_required")
    assert_error(prepare(client, path), 409, "selection_required")


def test_selection_invalidation_and_repeated_preparation(client):
    path = confirmed(client)
    record = client.app.state.capture_store._captures[path.rsplit("/", 1)[1]]
    first = prepare(client, path).json()
    original_prepared = record.preview.prepared
    second = prepare(client, path).json()
    assert first["preview_id"] != second["preview_id"]
    assert first["preview_json"] == second["preview_json"] and first["sha256"] == second["sha256"]
    assert record.preview.prepared is not original_prepared
    assert record.preview.preview_id == second["preview_id"]
    third = prepare(client, path, failure_pattern="missing_null_fields", approved_labels=["id"]).json()
    assert third["preview_id"] != second["preview_id"] and third["sha256"] != second["sha256"]
    assert record.preview.approved_labels == frozenset({"id"})
    assert record.preview.failure_pattern == "missing_null_fields"
    assert_error(client.put(path + "/selection", json={"entry_ids": [0, 0]}), 422, "invalid_selection")
    assert record.preview.preview_id == third["preview_id"]  # Invalid selection changed nothing.
    assert client.put(path + "/selection", json={"entry_ids": [0]}).status_code == 200
    assert record.preview is None and record.labels is None
    fourth = prepare(client, path).json()
    assert [tx["entry_id"] for tx in json.loads(fourth["preview_json"])["transactions"]] == [0]
    assert all(label.startswith("field_") for label in all_fields(json.loads(fourth["preview_json"])))


@pytest.mark.parametrize("expired", [False, True])
def test_deletion_expiry_unknown_ids_and_store_isolation(expired):
    clock = [0.0]
    store = CaptureStore(clock=lambda: clock[0])
    with TestClient(create_app(capture_store=store), base_url="http://localhost:8000") as local:
        path = confirmed(local)
        assert prepare(local, path).status_code == 200
        if expired:
            clock[0] += CAPTURE_TTL_SECONDS
        else:
            assert local.delete(path).status_code == 204
        assert_error(local.get(path + "/schema-labels"), 404, "capture_not_found")
        assert_error(prepare(local, path), 404, "capture_not_found")
        assert not store._captures
        with TestClient(create_app(), base_url="http://localhost:8000") as other:
            assert_error(prepare(other, path), 404, "capture_not_found")


def data_with_bodies(bodies):
    har = json.loads(FIXTURE.read_bytes())
    for entry, body in zip(har["log"]["entries"], bodies, strict=True):
        entry["response"]["content"]["text"] = json.dumps(body)
    return json.dumps(har).encode()


def test_names_discovered_from_scrubbed_json_bodies_arrays_and_query_only(client):
    har = json.loads(FIXTURE.read_bytes())
    for entry in har["log"]["entries"]:
        request = entry["request"]
        request["url"] += f"&auth_token={PRIVATE}"
        request["queryString"].append({"name": "auth_token", "value": PRIVATE})
        request["postData"] = {"mimeType": "application/json", "text": json.dumps({
            "client_secret": PRIVATE, "submission": [{"owner_id": 101}], "note": PRIVATE,
        })}
        request["bodySize"] = len(request["postData"]["text"].encode())
        request["headers"].append({"name": "Content-Type", "value": "application/json"})
        entry["response"]["content"]["text"] = json.dumps({"password": PRIVATE, "record": {"owner_id": 101}})
        entry["comment"] = PRIVATE
    path = confirmed(client, json.dumps(har).encode())
    response = client.get(path + "/schema-labels")
    assert response.json() == {"labels": ["note", "owner_id", "page", "page_size", "record", "submission"]}
    assert_restricted(response.text)
    assert_restricted(prepare(client, path).text)


@pytest.mark.parametrize("case", ["long_name", "too_many_names", "deep", "unsupported_body", "repeated_query"])
def test_unsupported_and_oversized_label_choices_fail_safely(client, case):
    if case == "long_name":
        raw = data_with_bodies([{PRIVATE + "é" * 33: 1}, {}])
    elif case == "too_many_names":
        raw = data_with_bodies([{f"left_{index}": index for index in range(32)},
                                {f"right_{index}": index for index in range(32)}])
    elif case == "deep":
        value = {"leaf": PRIVATE}
        for _ in range(privacy.MAX_DEPTH + 1):
            value = {"nested": value}
        raw = data_with_bodies([value, {}])
    else:
        har = json.loads(FIXTURE.read_bytes())
        request = har["log"]["entries"][0]["request"]
        if case == "unsupported_body":
            request["postData"] = {"mimeType": "application/x-www-form-urlencoded", "text": "value=" + PRIVATE}
            request["bodySize"] = len(request["postData"]["text"].encode())
        else:
            request["url"] += "&page=3"
            request["queryString"].append({"name": "page", "value": "3"})
        raw = json.dumps(har).encode()
    path = confirmed(client, raw)
    for response in (client.get(path + "/schema-labels"), prepare(client, path)):
        assert_error(response, 422, "label_choices_unavailable")
        assert PRIVATE not in response.text


def test_omission_metadata_matches_exact_snapshot(client):
    path = confirmed(client, data_with_bodies([{"rows": list(range(12))}, {"rows": list(range(12))}]))
    dto = prepare(client, path).json()
    assert dto["metadata"] == json.loads(dto["preview_json"])["metadata"]
    assert dto["metadata"]["examples_truncated"] is True
    assert dto["metadata"]["examples_omitted"] > 0


def test_schema_names_are_local_choices_and_only_explicitly_approved_names_leave(client):
    dynamic = "fictional_private_schema_name"
    path = confirmed(client, data_with_bodies([{dynamic: PRIVATE}, {dynamic: PRIVATE}]))
    assert dynamic in client.get(path + "/schema-labels").json()["labels"]
    assert dynamic not in prepare(client, path).json()["preview_json"]
    dto = prepare(client, path, approved_labels=[dynamic]).json()
    assert dynamic in set(all_fields(json.loads(dto["preview_json"])))
    assert PRIVATE not in dto["preview_json"]


def test_builder_limit_failure_does_not_leave_a_stale_preview(client, monkeypatch):
    path = confirmed(client)
    assert prepare(client, path).status_code == 200
    record = client.app.state.capture_store._captures[path.rsplit("/", 1)[1]]

    def failure(*args, **kwargs):
        raise privacy.PrivacyError(PRIVATE)

    monkeypatch.setattr(capture_store, "prepare_model_payload", failure)
    assert_error(prepare(client, path), 422, "preview_unavailable")
    assert record.preview is None


def test_expiry_during_preparation_cannot_retain_a_preview(monkeypatch):
    clock = [0.0]
    store = CaptureStore(clock=lambda: clock[0])
    with TestClient(create_app(capture_store=store), base_url="http://localhost:8000") as local:
        path = confirmed(local)
        real = capture_store.prepare_model_payload

        def delayed(*args, **kwargs):
            prepared = real(*args, **kwargs)
            clock[0] += CAPTURE_TTL_SECONDS
            return prepared

        monkeypatch.setattr(capture_store, "prepare_model_payload", delayed)
        assert_error(prepare(local, path), 404, "capture_not_found")
        assert not store._captures


def test_existing_host_origin_and_no_store_boundaries_apply_to_preview(client):
    path = confirmed(client)
    assert_error(client.get(path + "/schema-labels", headers={"Host": "attacker.example.test"}), 400, "invalid_host")
    assert_error(client.post(path + "/model-preview", json={"failure_pattern": "duplicate_pagination"},
                             headers={"Origin": "http://attacker.example.test"}), 403, "invalid_origin")
