"""Explicit loopback-only tests of extracted capsules in isolated subprocesses."""

from contextlib import contextmanager
from copy import deepcopy
import http.client
import json
import os
from pathlib import Path
import selectors
import socket
import subprocess
import sys
from zipfile import ZipFile

import pytest

from app.export import approve_export, prepare_capsule, write_capsule
from app.replay_template import server
from tests.test_export import verified, with_request_body


@pytest.fixture(autouse=True)
def loopback_only(monkeypatch):
    connect = socket.create_connection

    def guarded(address, *args, **kwargs):
        assert address[0] == "127.0.0.1", "Only explicit loopback requests are permitted."
        return connect(address, *args, **kwargs)

    def forbidden(*args, **kwargs):
        raise AssertionError("No paid/external network calls.")

    monkeypatch.setattr(socket, "create_connection", guarded)
    monkeypatch.setattr(http.client, "HTTPSConnection", forbidden)


@contextmanager
def running(tmp_path, result):
    preview = prepare_capsule(result)
    archive = write_capsule(preview, approve_export(preview), tmp_path / "capsule.zip")
    extracted = tmp_path / "extracted"
    with ZipFile(archive) as capsule:
        capsule.extractall(extracted)
    elsewhere = tmp_path / "unrelated-working-directory"
    elsewhere.mkdir()
    # Minimal environment, no inherited API keys or PYTHONPATH; isolated Python
    # with site packages disabled cannot import the installed project pipeline.
    process = subprocess.Popen(
        [sys.executable, "-I", "-S", str(extracted / "server.py"), "--port", "0"],
        cwd=elsewhere, env={"PATH": os.defpath, "PYTHONIOENCODING": "utf-8"},
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        with selectors.DefaultSelector() as ready:
            ready.register(process.stdout, selectors.EVENT_READ)
            assert ready.select(timeout=5), "Standalone server did not start within timeout."
        line = process.stdout.readline().strip()
        assert line.startswith("Replay listening on http://127.0.0.1:"), line
        port = int(line.rsplit(":", 1)[1])
        assert port > 0
        yield port, json.loads(preview.contents["transformed.har"]), process
    finally:
        process.terminate()
        try:
            output, errors = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            output, errors = process.communicate(timeout=5)
        assert process.poll() is not None
        # Request paths/bodies are not logged, even on errors.
        assert output == "" and errors == ""


def request(port, path, *, method="GET", body=None, raw=None, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    data = json.dumps(body).encode("utf-8") if raw is None and body is not None else raw
    headers = dict(headers or {})
    if data is not None:
        headers.setdefault("Content-Type", "application/json")
    try:
        connection.request(method, path, body=data, headers=headers)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def test_standalone_two_pages_known_duplicate_fixed_headers_no_upstream(tmp_path):
    with running(tmp_path, verified()) as (port, har, process):
        pages = []
        for page, entry in enumerate(har["log"]["entries"], 1):
            # Sorted normalized query matching is independent of original order.
            status, headers, data = request(port, f"/v1/customers?page_size=3&page={page}")
            expected = json.loads(entry["response"]["content"]["text"])
            assert status == entry["response"]["status"] == 200
            assert json.loads(data) == expected
            assert int(headers["Content-Length"]) == len(data)
            assert headers["Cache-Control"] == "no-store"
            assert headers["X-Content-Type-Options"] == "nosniff"
            assert "Set-Cookie" not in headers and "Location" not in headers
            assert "X-Request-ID" not in headers and "api.example.test" not in str(headers)
            pages.append(expected["customers"])
        assert pages[0][2] == pages[1][0]
        assert {row["id"] for row in pages[0]} & {row["id"] for row in pages[1]} == {pages[0][2]["id"]}
        assert len({row["id"] for rows in pages for row in rows}) == 5
        assert process.poll() is None


@pytest.mark.parametrize("path, method, status", [
    ("/v1/customers?page=99&page_size=3", "GET", 404),
    ("/v1/customers?page=1&page_size=3", "POST", 404),
    ("/../transformed.har", "GET", 404),
    ("/server.py", "GET", 404),
    ("https://private.example.test/v1/customers?page=1", "GET", 400),
])
def test_unmatched_requests_are_local_errors_without_route_echo(tmp_path, path, method, status):
    with running(tmp_path, verified()) as (port, _, _):
        code, headers, data = request(port, path, method=method)
        assert code == status
        assert "error" in json.loads(data)
        assert path.encode() not in data and b"private.example.test" not in data
        assert "Location" not in headers


def test_standalone_typed_json_request_matching_absent_null_and_order(tmp_path):
    with running(tmp_path, verified(with_request_body(same_route=True))) as (port, har, _):
        path = "/v1/customers?page=1&page_size=3"
        for entry in har["log"]["entries"]:
            body = json.loads(entry["request"]["postData"]["text"])
            # Object order doesn't matter; scalar types, nulls and array order do.
            reordered = dict(reversed(list(body.items())))
            code, _, data = request(port, path, method="POST", body=reordered)
            assert code == 200 and json.loads(data) == json.loads(entry["response"]["content"]["text"])
        original = json.loads(har["log"]["entries"][0]["request"]["postData"]["text"])
        for identifier in (True, 1.0, None):
            body = {**original, "identifier": identifier}
            assert request(port, path, method="POST", body=body)[0] == 404
        changed = {**original, "ordered": list(reversed(original["ordered"]))}
        assert request(port, path, method="POST", body=changed)[0] == 404
        assert request(port, path, method="POST")[0] == 404
        assert request(port, path, method="POST", raw=b"null")[0] == 404
        assert request(port, path, method="POST", raw=b'{"identifier":1,"identifier":1}')[0] == 400
        assert request(port, path, method="POST", raw=b"not json")[0] == 400
        assert request(port, path, method="POST", raw=b"NaN")[0] == 400
        assert request(port, path, method="POST", raw=b"x", headers={"Content-Type": "text/plain"})[0] == 400


def test_ambiguous_packaged_routes_fail_before_server_binds():
    har = json.loads(prepare_capsule(verified()).contents["transformed.har"])
    har["log"]["entries"][1]["request"] = deepcopy(har["log"]["entries"][0]["request"])
    with pytest.raises(server.ReplayError):
        server.make_server(har, 0)


def test_template_route_matching_preserves_query_repeats_blanks_and_types():
    assert server.route_key("GET", "/x?b=&a=2&a=1") == server.route_key("GET", "/x?a=1&b=&a=2")
    assert server.route_key("GET", "/x?a=1&a=1") != server.route_key("GET", "/x?a=1")
    assert server.route_key("POST", "/x", True, 1) != server.route_key("POST", "/x", True, True)
    assert server.route_key("POST", "/x", True, 1) != server.route_key("POST", "/x", True, "1")
    assert server.route_key("POST", "/x", True, None) != server.route_key("POST", "/x")
