"""Credential scrubbing contracts using fabricated data and the synthetic fixture."""

import base64
from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import pytest

from app.engine.har import parse_har, select_transactions
from app.engine.scrub import ScrubError, scrub_transactions


FIXTURE_PATH = Path(__file__).resolve().parents[2] / "fixtures" / "duplicate-pagination.har"
SECRET = "BUGCAPSULE_FABRICATED_SECRET_9274"


def fixture_entries():
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))["log"]["entries"]


def parse_entries(tmp_path, entries):
    path = tmp_path / "fabricated.har"
    path.write_text(json.dumps({"log": {"entries": entries}}), encoding="utf-8")
    return parse_har(path).transactions


def snapshots(transactions):
    return [(deepcopy(tx.model_dump()), deepcopy(tx.original_entry)) for tx in transactions]


def headers(part):
    return {item["name"].lower(): item["value"] for item in part["headers"]}


def decoded_text(content):
    if content.get("encoding") == "base64":
        return base64.b64decode(content["text"], validate=True).decode("utf-8")
    return content["text"]


def assert_body_sizes(part, content):
    size = len(decoded_text(content).encode("utf-8"))
    assert part["bodySize"] == size
    assert part["headersSize"] == -1
    if "size" in content:
        assert content["size"] == size
    for header in part["headers"]:
        if header["name"].lower() == "content-length":
            assert header["value"] == str(size)


def test_real_fixture_scrub_is_independent_and_preserves_duplicate(tmp_path):
    selected = select_transactions(parse_har(FIXTURE_PATH), [0, 1])
    before = snapshots(selected)
    file_before = FIXTURE_PATH.read_bytes()
    result = scrub_transactions(selected)
    assert snapshots(selected) == before
    assert FIXTURE_PATH.read_bytes() == file_before
    assert [tx.entry_id for tx in result.transactions] == [0, 1]

    for original, cleaned in zip(selected, result.transactions, strict=True):
        retained = json.dumps(cleaned.model_dump()) + json.dumps(cleaned.original_entry)
        assert "BUGCAPSULE_FAKE_TOKEN_DO_NOT_USE" not in retained
        assert "BUGCAPSULE_FAKE_SESSION" not in retained
        assert cleaned.response_json == original.response_json
        assert cleaned.original_entry is not original.original_entry
        assert cleaned.response_json is not original.response_json
        for part in ("request", "response"):
            assert cleaned.original_entry[part]["cookies"] == []
        assert_body_sizes(cleaned.original_entry["response"], cleaned.original_entry["response"]["content"])

    first, second = [tx.response_json["customers"] for tx in result.transactions]
    assert [item["id"] for item in first] == [101, 102, 103]
    assert [item["id"] for item in second] == [103, 104, 105]
    assert {item["id"] for item in first} & {item["id"] for item in second} == {103}
    assert first[2] == second[0]
    assert [asdict(item) for item in result.removals] == [
        {"entry_id": entry_id, "category": category, "count": count}
        for entry_id in (0, 1)
        for category, count in (("request_cookies", 1), ("request_headers", 2))
    ]
    assert scrub_transactions(result.transactions).removals == []
    # Reparse the retained scrubbed HAR entries through the unchanged parser.
    reparsed = parse_entries(tmp_path, [tx.original_entry for tx in result.transactions])
    assert [tx.response_json for tx in reparsed] == [tx.response_json for tx in result.transactions]
    result.transactions[0].original_entry["request"]["queryString"][0]["value"] = "99"
    result.transactions[0].response_json["customers"][0]["id"] = 999
    assert snapshots(selected) == before


def test_mixed_case_credential_headers_and_all_cookie_arrays(tmp_path):
    entry = fixture_entries()[0]
    sensitive = [
        "aUtHoRiZaTiOn", "PROXY-AUTHORIZATION", "Cookie", "sEt-CoOkIe",
        "X-API-Key", "x-AuTh-ToKeN", "X_API.KEY",
    ]
    for part in (entry["request"], entry["response"]):
        part["headers"] = [{"name": name, "value": SECRET} for name in sensitive] + [
            {"name": "X-Request-ID", "value": "fabricated-business-id"},
            {"name": "Authorization-Policy", "value": "ordinary-policy"},
        ]
        part["cookies"] = [{"name": "anything", "value": SECRET}]
    result = scrub_transactions(parse_entries(tmp_path, [entry]))
    for part in ("request", "response"):
        scrubbed = result.transactions[0].original_entry[part]
        assert headers(scrubbed) == {
            "x-request-id": "fabricated-business-id", "authorization-policy": "ordinary-policy"
        }
        assert scrubbed["cookies"] == []
    assert SECRET not in json.dumps(result.transactions[0].original_entry)
    assert {item.category: item.count for item in result.removals} == {
        "request_headers": len(sensitive), "response_headers": len(sensitive),
        "request_cookies": 1, "response_cookies": 1,
    }


@pytest.mark.parametrize("har_query", [True, False])
def test_url_userinfo_and_query_credentials_preserve_pagination(tmp_path, har_query):
    entry = fixture_entries()[0]
    entry["request"]["url"] = (
        f"https://user:{SECRET}@api.example.test/v1/customers?"
        f"page=1&access%5Ftoken={SECRET}&page_size=3&cursor=a%2Bb%3D&"
        f"X-API-Key={SECRET}&page_token=next&tag=a&tag=b&empty="
    )
    entry["request"]["queryString"] = [
        {"name": name, "value": value}
        for name, value in parse_qsl(urlsplit(entry["request"]["url"]).query, keep_blank_values=True)
    ]
    if not har_query:
        del entry["request"]["queryString"]
    tx = scrub_transactions(parse_entries(tmp_path, [entry])).transactions[0]
    assert tx.url == tx.original_entry["request"]["url"]
    assert urlsplit(tx.url).netloc == "api.example.test"
    expected = [("page", "1"), ("page_size", "3"), ("cursor", "a+b="),
                ("page_token", "next"), ("tag", "a"), ("tag", "b"), ("empty", "")]
    assert parse_qsl(urlsplit(tx.url).query, keep_blank_values=True) == expected
    assert [(item["name"], item["value"]) for item in tx.original_entry["request"]["queryString"]] == expected
    assert tx.origin == "https://api.example.test"
    assert SECRET not in json.dumps(tx.original_entry)


def test_recursive_json_exact_name_policy_preserves_business_data(tmp_path):
    entry = fixture_entries()[0]
    body = {
        "Access_Token": SECRET, "CLIENT.SECRET": SECRET,
        "customers": [
            {"id": 101, "password": SECRET, "profile": {"Auth-Token": SECRET, "name": "Fictional"}},
            {"id": 103, "sessionId": SECRET, "metadata": [{"API KEY": SECRET, "missing": None}]},
            {"id": 105},
        ],
        "items": [None, False, True, 7, 2.5, "7"],
        "pagination": {"page": 1, "page_size": 3, "cursor": None, "page_token": "next"},
        "password_hint": "ordinary hint", "token_count": 4, "session_count": 2,
    }
    entry["response"]["content"]["text"] = json.dumps(body)
    tx = scrub_transactions(parse_entries(tmp_path, [entry])).transactions[0]
    assert tx.response_json == {
        "customers": [
            {"id": 101, "profile": {"name": "Fictional"}},
            {"id": 103, "metadata": [{"missing": None}]}, {"id": 105},
        ],
        "items": [None, False, True, 7, 2.5, "7"],
        "pagination": {"page": 1, "page_size": 3, "cursor": None, "page_token": "next"},
        "password_hint": "ordinary hint", "token_count": 4, "session_count": 2,
    }
    assert [type(value) for value in tx.response_json["items"]] == [type(None), bool, bool, int, float, str]
    assert json.loads(tx.original_entry["response"]["content"]["text"]) == tx.response_json
    assert SECRET not in json.dumps(tx.original_entry)


@pytest.mark.parametrize("encoded", [False, True])
def test_json_request_and_response_representations_and_sizes(tmp_path, encoded):
    entry = fixture_entries()[0]
    entry["request"]["method"] = "POST"
    body = {"name": "Fictional Marín", "credentials": {"refreshToken": SECRET}, "ids": [103, 104], "null": None}
    entry["request"]["postData"] = {
        "mimeType": "Application/vnd.example+JSON; charset=UTF-8", "text": json.dumps(body),
    }
    entry["request"]["headers"] += [{"name": "Content-Length", "value": "9999"}]
    entry["response"]["content"]["text"] = json.dumps({"id_token": SECRET, "name": "Marín"})
    # HAR response text may be decoded even when the original wire was compressed.
    entry["response"]["headers"] += [{"name": "Content-Encoding", "value": "gzip"}]
    entry["response"]["content"]["compression"] = 23
    if encoded:
        for content in (entry["request"]["postData"], entry["response"]["content"]):
            content["encoding"] = "base64"
            content["text"] = base64.b64encode(content["text"].encode()).decode()
    result = scrub_transactions(parse_entries(tmp_path, [entry]))
    tx = result.transactions[0]
    request, response = tx.original_entry["request"], tx.original_entry["response"]
    assert json.loads(decoded_text(request["postData"])) == {
        "name": "Fictional Marín", "credentials": {}, "ids": [103, 104], "null": None,
    }
    assert json.loads(decoded_text(response["content"])) == tx.response_json == {"name": "Marín"}
    assert SECRET not in decoded_text(request["postData"])
    assert SECRET not in decoded_text(response["content"])
    assert_body_sizes(request, request["postData"])
    assert_body_sizes(response, response["content"])
    assert "content-encoding" not in headers(response)
    assert "compression" not in response["content"]
    if encoded:
        assert request["postData"]["encoding"] == response["content"]["encoding"] == "base64"


@pytest.mark.parametrize("representation", ["params", "text", "both"])
def test_form_body_parameters_and_text_stay_consistent(tmp_path, representation):
    entry = fixture_entries()[0]
    post = {"mimeType": "application/x-www-form-urlencoded; charset=utf-8"}
    if representation != "text":
        post["params"] = [{"name": "client_secret", "value": SECRET},
                          {"name": "page", "value": "2"}, {"name": "cursor", "value": "a+b="}]
    if representation != "params":
        post["text"] = f"client_secret={SECRET}&page=2&cursor=a%2Bb%3D"
    entry["request"]["postData"] = post
    entry["request"]["headers"].append({"name": "Content-Length", "value": "1000"})
    tx = scrub_transactions(parse_entries(tmp_path, [entry])).transactions[0]
    cleaned = tx.original_entry["request"]["postData"]
    assert cleaned["params"] == [{"name": "page", "value": "2"}, {"name": "cursor", "value": "a+b="}]
    assert parse_qsl(cleaned["text"]) == [("page", "2"), ("cursor", "a+b=")]
    assert SECRET not in json.dumps(tx.original_entry)
    assert_body_sizes(tx.original_entry["request"], cleaned)


@pytest.mark.parametrize("post", [
    {"mimeType": "multipart/form-data; boundary=fake", "text": SECRET},
    {"mimeType": "text/plain", "text": SECRET},
    {"mimeType": "application/json", "text": SECRET, "encoding": "gzip"},
    {"mimeType": "application/json", "text": SECRET},
    {"mimeType": "application/json", "text": f"!!{SECRET}!!", "encoding": "base64"},
    {"mimeType": "application/json", "text": "/w==", "encoding": "base64"},
    {"mimeType": "application/json", "text": "{}", "params": [{"name": "password", "value": SECRET}]},
    {"mimeType": "application/json"},
    {"mimeType": "application/json; charset=iso-8859-1", "text": "{}"},
    {"text": SECRET},
    None,
    {"mimeType": "application/x-www-form-urlencoded"},
    {"mimeType": "application/x-www-form-urlencoded", "params": [{"name": "a", "value": SECRET, "fileName": SECRET}]},
    {"mimeType": "application/x-www-form-urlencoded", "text": "a=1", "params": [{"name": "a", "value": SECRET}]},
])
def test_unsupported_or_malformed_request_bodies_fail_without_values(tmp_path, post):
    entries = fixture_entries()
    entries[1]["request"]["postData"] = post
    transactions = parse_entries(tmp_path, entries)
    before = snapshots(transactions)
    with pytest.raises(ScrubError, match="Entry 1:") as error:
        scrub_transactions(transactions)
    assert SECRET not in str(error.value)
    assert "BUGCAPSULE_FAKE" not in str(error.value)
    assert "https://" not in str(error.value)
    assert "input_value" not in str(error.value)
    assert error.value.__suppress_context__
    assert snapshots(transactions) == before


@pytest.mark.parametrize("change", ["compressed", "missing", "mime", "url", "response", "query"])
def test_conflicting_or_unavailable_representations_fail_closed(tmp_path, change):
    entry = fixture_entries()[0]
    if change == "compressed":
        entry["request"]["headers"].append({"name": "Content-Encoding", "value": "gzip"})
    elif change == "missing":
        entry["request"]["bodySize"] = 100
    elif change == "mime":
        entry["request"]["postData"] = {"mimeType": "application/json", "text": "{}"}
        entry["request"]["headers"].append({"name": "Content-Type", "value": "text/plain"})
    elif change == "query":
        entry["request"]["queryString"].append({"name": "extra", "value": SECRET})
    transactions = parse_entries(tmp_path, [entry])
    if change == "url":
        transactions = [transactions[0].model_copy(update={"url": f"https://example.test/{SECRET}"})]
    elif change == "response":
        transactions[0].response_json["password"] = SECRET
    with pytest.raises(ScrubError, match="Entry 0:") as error:
        scrub_transactions(transactions)
    assert SECRET not in str(error.value)


def test_summary_has_only_ids_categories_and_counts(tmp_path):
    entry = fixture_entries()[0]
    entry["response"]["content"]["text"] = json.dumps({"password": SECRET, "token": SECRET})
    result = scrub_transactions(parse_entries(tmp_path, [entry]))
    summary = [asdict(item) for item in result.removals]
    assert all(set(item) == {"entry_id", "category", "count"} for item in summary)
    assert all(type(item["count"]) is int and item["count"] > 0 for item in summary)
    assert {item["category"]: item["count"] for item in summary}["response_json"] == 2
    assert SECRET not in json.dumps(summary)
    assert SECRET not in repr(result.removals)


def test_unknown_names_and_business_values_are_not_claimed_anonymous(tmp_path):
    entry = fixture_entries()[0]
    entry["response"]["content"]["text"] = json.dumps({"customer_id": 103, "business_note": SECRET})
    tx = scrub_transactions(parse_entries(tmp_path, [entry])).transactions[0]
    assert tx.response_json == {"customer_id": 103, "business_note": SECRET}
