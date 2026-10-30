"""In-process local API tests: fabricated data, no sockets or model requests."""

from copy import deepcopy
import http.client
import json
from pathlib import Path
import socket
from tempfile import NamedTemporaryFile

from fastapi.testclient import TestClient
import pytest

from app.api import captures
from app.engine.har import HarParseError, parse_har
from app.main import LOCAL_BROWSER_ORIGINS, create_app
from app.schemas import HarTransaction
from app.services.capture_store import CAPTURE_TTL_SECONDS, MAX_CAPTURES, CaptureStore


FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "duplicate-pagination.har"
SENSITIVE = "FABRICATED_API_PRIVATE_883741"
JSON_HEADERS = {"Content-Type": "application/json"}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Capture API tests must not use sockets or model calls.")
    # Keep asyncio's internal socketpair available; block all socket connections.
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(http.client, "HTTPSConnection", forbidden)


@pytest.fixture
def client():
    with TestClient(create_app(), base_url="http://127.0.0.1:8000") as client:
        yield client


def upload(client, body=None, **kwargs):
    return client.post("/api/captures", content=FIXTURE.read_bytes() if body is None else body,
                       headers=kwargs.pop("headers", JSON_HEADERS), **kwargs)


def fabricated(*, cross_origin=False, many=False, skipped=False):
    har = json.loads(FIXTURE.read_bytes())
    entries = har["log"]["entries"]
    if cross_origin:
        other = deepcopy(entries[0])
        other["request"]["url"] = "https://other.example.test/items"
        other["request"]["queryString"] = []
        entries.append(other)
    if many:
        for number in range(2, 12):
            extra = deepcopy(entries[0])
            extra["request"]["url"] = f"https://api.example.test/items?page={number}"
            extra["request"]["queryString"] = [{"name": "page", "value": str(number)}]
            entries.append(extra)
    if skipped:
        extra = deepcopy(entries[0])
        extra["response"]["content"] = {"mimeType": "text/html", "text": SENSITIVE}
        entries.append(extra)
    return json.dumps(har).encode()


def assert_error(response, status, code):
    assert response.status_code == status, response.text
    value = response.json()
    assert set(value) == {"error"}
    assert set(value["error"]) == {"code", "message"}
    assert value["error"]["code"] == code
    assert SENSITIVE not in response.text and "BUGCAPSULE_FAKE" not in response.text
    assert "input" not in value["error"] and "traceback" not in response.text
    assert response.headers["Cache-Control"] == "no-store"


def test_real_fixture_import_summary_selection_delete_and_private_store(client, monkeypatch):
    def forbidden_dump(*args, **kwargs):
        raise AssertionError("Private transaction serialization is forbidden at this boundary.")
    monkeypatch.setattr(HarTransaction, "model_dump", forbidden_dump)
    response = upload(client)
    assert response.status_code == 201
    summary = response.json()
    capture_id = summary["capture_id"]
    assert len(capture_id) >= 32 and capture_id not in FIXTURE.read_text()
    assert set(summary) == {"capture_id", "entry_count", "supported_count", "skipped_count", "transactions",
                            "origin_groups", "skipped_entries", "selection"}
    assert summary["entry_count"] == summary["supported_count"] == 2
    assert summary["skipped_count"] == 0 and summary["skipped_entries"] == []
    assert summary["origin_groups"] == [{"origin": "https://api.example.test", "entry_ids": [0, 1]}]
    assert summary["selection"] == {"entry_ids": [], "count": 0, "origin": None}
    assert summary["transactions"] == [
        {"entry_id": index, "method": "GET", "status": 200, "origin": "https://api.example.test",
         "display_path": "/v1/customers"} for index in (0, 1)
    ]
    # Raw private fields remain local; they never appear in returned summaries.
    stored = client.app.state.capture_store._captures[capture_id].parsed
    assert stored.transactions[0].response_json["customers"][0]["id"] == 101
    assert "BUGCAPSULE_FAKE_TOKEN" in json.dumps(stored.transactions[0].original_entry)
    for customer in stored.transactions[0].response_json["customers"]:
        for field in ("name", "email", "company"):
            assert customer[field] not in response.text
    for forbidden in ("Authorization", "Cookie", "original_entry", "response_json", "postData", "page_size", "BUGCAPSULE_FAKE"):
        assert forbidden not in response.text
    assert client.get(f"/api/captures/{capture_id}").json() == summary
    selected = client.put(f"/api/captures/{capture_id}/selection", json={"entry_ids": [1, 0]})
    assert selected.status_code == 200
    assert selected.json() == {"entry_ids": [1, 0], "count": 2, "origin": "https://api.example.test"}
    assert client.get(f"/api/captures/{capture_id}").json()["selection"] == selected.json()
    removed = client.delete(f"/api/captures/{capture_id}")
    assert removed.status_code == 204 and removed.content == b""
    assert_error(client.get(f"/api/captures/{capture_id}"), 404, "capture_not_found")
    assert capture_id not in client.app.state.capture_store._captures


def test_summary_normalizes_origin_and_omits_url_userinfo_query_fragment(client):
    har = json.loads(FIXTURE.read_bytes())
    for entry in har["log"]["entries"]:
        entry["request"]["url"] = f"HTTPS://{SENSITIVE}:{SENSITIVE}@API.EXAMPLE.TEST:443/v1/customers?token={SENSITIVE}#{SENSITIVE}"
        entry["request"]["queryString"] = [{"name": "token", "value": SENSITIVE}]
        entry["comment"] = SENSITIVE
    response = upload(client, json.dumps(har).encode())
    assert response.status_code == 201 and SENSITIVE not in response.text
    assert response.json()["origin_groups"] == [{"origin": "https://api.example.test", "entry_ids": [0, 1]}]
    assert all(tx["display_path"] == "/v1/customers" for tx in response.json()["transactions"])


def test_origin_groups_skips_and_stable_original_ids(client):
    response = upload(client, fabricated(cross_origin=True, skipped=True))
    assert response.status_code == 201
    summary = response.json()
    assert summary["entry_count"] == 4 and summary["supported_count"] == 3
    assert summary["origin_groups"] == [
        {"origin": "https://api.example.test", "entry_ids": [0, 1]},
        {"origin": "https://other.example.test", "entry_ids": [2]},
    ]
    assert summary["skipped_entries"] == [{"entry_id": 3, "reason": "unsupported MIME type"}]
    assert SENSITIVE not in response.text


@pytest.mark.parametrize("entry_ids", [[], [0, 0], [-1], [9999], [3], [0, 2], list(range(11)),
                                      [True], [False], [0.0], ["0"], [None], SENSITIVE, None])
def test_invalid_selection_preserves_previous_valid_selection(client, entry_ids):
    capture_id = upload(client, fabricated(cross_origin=True, skipped=True)).json()["capture_id"]
    path = f"/api/captures/{capture_id}/selection"
    valid = client.put(path, json={"entry_ids": [1, 0]}).json()
    response = client.put(path, json={"entry_ids": entry_ids})
    assert_error(response, 422, "invalid_selection")
    assert client.get(f"/api/captures/{capture_id}").json()["selection"] == valid


@pytest.mark.parametrize("body", [[], {}, {"extra": SENSITIVE}, {"entry_ids": [0], "private": SENSITIVE}])
def test_malformed_selection_schema_is_value_free(client, body):
    capture_id = upload(client).json()["capture_id"]
    assert_error(client.put(f"/api/captures/{capture_id}/selection", json=body), 422, "invalid_selection")
    assert client.get(f"/api/captures/{capture_id}").json()["selection"]["count"] == 0


def test_malformed_selection_json_is_value_free(client):
    capture_id = upload(client).json()["capture_id"]
    response = client.put(f"/api/captures/{capture_id}/selection", content=SENSITIVE, headers=JSON_HEADERS)
    assert_error(response, 422, "invalid_selection")


def test_selection_ten_from_larger_capture(client):
    summary = upload(client, fabricated(many=True)).json()
    assert summary["supported_count"] == 12
    response = client.put(f"/api/captures/{summary['capture_id']}/selection", json={"entry_ids": list(range(10))})
    assert response.status_code == 200 and response.json()["count"] == 10


@pytest.mark.parametrize("body", [b"", SENSITIVE.encode(), b"[]", b'{"log":null}', b'{"log":{"entries":{}}}',
                                  b'{"log":{"entries":[{}]}}', b"\xff\xfe"])
def test_invalid_json_envelope_and_fields_have_safe_errors(client, body):
    assert_error(upload(client, body), 400, "invalid_har")
    assert client.app.state.capture_store._captures == {}


def test_invalid_declared_json_body_has_safe_error(client):
    har = json.loads(FIXTURE.read_bytes())
    har["log"]["entries"][0]["response"]["content"]["text"] = SENSITIVE
    assert_error(upload(client, json.dumps(har).encode()), 400, "invalid_har")


@pytest.mark.parametrize("headers", [{}, {"Content-Type": "text/plain"}, {"Content-Type": "application/octet-stream"},
                                     {"Content-Type": "multipart/form-data"}, {"Content-Type": "application/problem+json"},
                                     {"Content-Type": "application/json", "Content-Encoding": "gzip"}])
def test_unsupported_media_type(client, headers):
    assert_error(upload(client, headers=headers), 415, "unsupported_media_type")


def test_json_mime_parameters_supported(client):
    assert upload(client, headers={"Content-Type": "Application/JSON; charset=utf-8"}).status_code == 201


def test_exact_size_boundary_and_actual_bytes_not_content_length(client):
    assert captures.MAX_IMPORT_BYTES == 16 * 1024 * 1024
    raw = FIXTURE.read_bytes()
    padded = raw + b" " * (captures.MAX_IMPORT_BYTES - len(raw))
    assert upload(client, padded, headers={**JSON_HEADERS, "Content-Length": "1"}).status_code == 201
    response = upload(client, padded + b" ", headers={**JSON_HEADERS, "Content-Length": "1"})
    assert_error(response, 413, "payload_too_large")
    response = upload(client, iter([raw, b" " * captures.MAX_IMPORT_BYTES]))
    assert_error(response, 413, "payload_too_large")


def test_streamed_cumulative_limit_stops_before_reading_remaining_chunks(monkeypatch):
    monkeypatch.setattr(captures, "MAX_IMPORT_BYTES", 100)
    delivered = []
    app = create_app()

    async def chunked_app(scope, receive, send):
        if scope["type"] != "http":
            return await app(scope, receive, send)
        chunks = iter([b"x" * 60, b"y" * 60, b"z" * 60])

        async def chunked_receive():
            chunk = next(chunks)
            delivered.append(chunk)
            return {"type": "http.request", "body": chunk, "more_body": True}

        await app(scope, chunked_receive, send)

    with TestClient(chunked_app, base_url="http://localhost:8000") as local:
        assert_error(upload(local, b"ignored", headers={**JSON_HEADERS, "Content-Length": "1"}), 413, "payload_too_large")
    assert [len(chunk) for chunk in delivered] == [60, 60]
    assert app.state.capture_store._captures == {}


@pytest.mark.parametrize("mode", ["success", "parse_failure", "oversize", "capacity"])
def test_temporary_files_removed_on_all_outcomes(client, monkeypatch, tmp_path, mode):
    created = []
    parsed_paths = []

    def temporary(**kwargs):
        temporary = NamedTemporaryFile(**kwargs, dir=tmp_path)
        created.append(Path(temporary.name))
        return temporary

    def parse(path):
        parsed_paths.append(path)
        assert Path(path).exists()
        if mode == "parse_failure":
            raise HarParseError(SENSITIVE)
        return parse_har(path)

    monkeypatch.setattr(captures, "NamedTemporaryFile", temporary)
    monkeypatch.setattr(captures, "parse_har", parse)
    if mode == "oversize":
        monkeypatch.setattr(captures, "MAX_IMPORT_BYTES", 1)
    if mode == "capacity":
        for _ in range(MAX_CAPTURES):
            assert upload(client).status_code == 201
    response = upload(client)
    expected = {"success": 201, "parse_failure": 400, "oversize": 413, "capacity": 409}[mode]
    assert response.status_code == expected and SENSITIVE not in response.text
    assert created and all(not path.exists() for path in created)
    assert list(tmp_path.iterdir()) == []
    assert bool(parsed_paths) == (mode != "oversize")


@pytest.mark.parametrize("method", ["get", "put", "delete"])
def test_unknown_and_expired_ids_have_same_safe_response(method):
    clock = [10.0]
    store = CaptureStore(clock=lambda: clock[0])
    with TestClient(create_app(capture_store=store), base_url="http://localhost:8000") as local:
        capture_id = upload(local).json()["capture_id"]
        path = f"/api/captures/{capture_id}"
        local.put(path + "/selection", json={"entry_ids": [0]})
        clock[0] += CAPTURE_TTL_SECONDS - 1
        assert local.get(path).status_code == 200
        clock[0] += 1  # Reading a summary does not extend the absolute TTL.
        suffix = "/selection" if method == "put" else ""
        kwargs = {"json": {"entry_ids": [0]}} if method == "put" else {}
        expired = getattr(local, method)(path + suffix, **kwargs)
        unknown = getattr(local, method)(f"/api/captures/{SENSITIVE}" + suffix, **kwargs)
        assert_error(expired, 404, "capture_not_found")
        assert expired.json() == unknown.json()
        assert store._captures == {}


def test_capacity_no_eviction_delete_and_expiry_release_slots():
    assert MAX_CAPTURES == 5 and CAPTURE_TTL_SECONDS == 1800
    clock = [0.0]
    store = CaptureStore(clock=lambda: clock[0])
    with TestClient(create_app(capture_store=store), base_url="http://localhost:8000") as local:
        ids = [upload(local).json()["capture_id"] for _ in range(MAX_CAPTURES)]
        assert len(set(ids)) == MAX_CAPTURES
        assert_error(upload(local), 409, "capture_capacity")
        assert all(local.get(f"/api/captures/{capture_id}").status_code == 200 for capture_id in ids)
        assert local.delete(f"/api/captures/{ids[0]}").status_code == 204
        assert upload(local).status_code == 201
        clock[0] += CAPTURE_TTL_SECONDS
        assert upload(local).status_code == 201
        assert len(store._captures) == 1


def test_capture_and_application_isolation_reset_and_input_ownership(client):
    first = upload(client).json()["capture_id"]
    second = upload(client).json()["capture_id"]
    assert first != second
    client.put(f"/api/captures/{first}/selection", json={"entry_ids": [0]})
    assert client.get(f"/api/captures/{second}").json()["selection"]["count"] == 0
    with TestClient(create_app(), base_url="http://localhost:8000") as other:
        assert_error(other.get(f"/api/captures/{first}"), 404, "capture_not_found")
    client.delete(f"/api/captures/{first}")
    assert client.get(f"/api/captures/{second}").status_code == 200
    client.app.state.capture_store.clear()
    assert_error(client.get(f"/api/captures/{second}"), 404, "capture_not_found")
    parsed = parse_har(FIXTURE)
    summary = client.app.state.capture_store.add(parsed)
    parsed.transactions.clear()
    summary.transactions.clear()
    assert client.app.state.capture_store.summary(summary.capture_id).supported_count == 2


@pytest.mark.parametrize("host", ["localhost", "LOCALHOST:8000", "127.0.0.1:8000", "[::1]:8000"])
def test_health_and_docs_remain_available_for_loopback_hosts(client, host):
    assert client.get("/health", headers={"Host": host}).json() == {"status": "ok"}
    assert client.get("/docs", headers={"Host": host}).status_code == 200
    assert client.get("/openapi.json", headers={"Host": host}).status_code == 200


@pytest.mark.parametrize("host", ["attacker.example.test", "localhost.attacker.example.test", "0.0.0.0:8000",
                                  "127.0.0.1.attacker.example.test", "localhost:99999", "localhost:",
                                  "localhost@attacker.example.test", "localhost/path", "localhost#fragment", "localhost?", "localhost#"])
def test_unexpected_or_malformed_hosts_rejected_before_reading_capture(client, host):
    response = upload(client, SENSITIVE.encode(), headers={**JSON_HEADERS, "Host": host,
                                                        "X-Forwarded-Host": "localhost:8000"})
    assert_error(response, 400, "invalid_host")
    assert client.app.state.capture_store._captures == {}


def test_duplicate_host_and_origin_headers_rejected(client):
    assert_error(client.get("/health", headers=[("Host", "localhost"), ("Host", "attacker.example.test")]), 400, "invalid_host")
    headers = [("Content-Type", "application/json"), ("Origin", "http://localhost:5173"), ("Origin", "http://attacker.example.test")]
    assert_error(upload(client, headers=headers), 403, "invalid_origin")


@pytest.mark.parametrize("origin", ["null", "http://attacker.example.test", "http://localhost:5174", "https://localhost:5173",
                                    "http://localhost:5173.attacker.example.test", "http://localhost:5173/"])
def test_unexpected_origin_rejects_state_changes_without_mutating_store(client, origin):
    capture_id = upload(client).json()["capture_id"]
    path = f"/api/captures/{capture_id}"
    headers = {"Origin": origin}
    assert_error(upload(client, headers={**JSON_HEADERS, **headers}), 403, "invalid_origin")
    assert_error(client.put(path + "/selection", json={"entry_ids": [0]}, headers=headers), 403, "invalid_origin")
    assert_error(client.delete(path, headers=headers), 403, "invalid_origin")
    assert client.get(path).json()["selection"]["count"] == 0


@pytest.mark.parametrize("origin", sorted(LOCAL_BROWSER_ORIGINS))
def test_explicit_local_browser_origins_and_preflight(client, origin):
    preflight = client.options("/api/captures", headers={"Origin": origin,
                               "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type"})
    assert preflight.status_code == 200
    assert preflight.headers["Access-Control-Allow-Origin"] == origin
    response = upload(client, headers={**JSON_HEADERS, "Origin": origin})
    assert response.status_code == 201 and response.headers["Access-Control-Allow-Origin"] == origin
    assert "Access-Control-Allow-Credentials" not in response.headers
    assert response.headers["Access-Control-Allow-Origin"] != "*"


def test_disallowed_preflight_and_unmatched_route_errors_are_safe(client):
    for headers, code, status in (
        ({"Origin": "http://attacker.example.test", "Access-Control-Request-Method": "POST"}, "invalid_origin", 403),
        ({"Origin": "http://localhost:5173", "Access-Control-Request-Method": "PATCH"}, "invalid_request", 400),
        ({"Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": SENSITIVE}, "invalid_request", 400),
    ):
        assert_error(client.options("/api/captures", headers=headers), status, code)
    assert_error(client.get(f"/{SENSITIVE}"), 404, "not_found")
    assert_error(client.put("/health", content=SENSITIVE), 405, "method_not_allowed")
