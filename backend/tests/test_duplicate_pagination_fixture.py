"""Exercise the entirely fabricated pagination HAR through the real local parser."""

from datetime import datetime, timedelta
import json
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from app.engine.har import parse_har, select_transactions


FIXTURE_PATH = (
    Path(__file__).resolve().parents[2] / "fixtures" / "duplicate-pagination.har"
)


def test_duplicate_pagination_fixture_parses_and_preserves_fake_credentials():
    capture = parse_har(FIXTURE_PATH)
    assert len(capture.transactions) == 2
    assert [tx.entry_id for tx in capture.transactions] == [0, 1]
    assert capture.skipped_entries == []
    assert capture.by_origin == {"https://api.example.test": [0, 1]}

    selected = select_transactions(capture, [0, 1])
    assert len(selected) == 2
    assert all(tx.method == "GET" and tx.status == 200 for tx in selected)
    first, second = [tx.response_json for tx in selected]
    first_ids = [customer["id"] for customer in first["customers"]]
    second_ids = [customer["id"] for customer in second["customers"]]
    assert first_ids == [101, 102, 103]
    assert second_ids == [103, 104, 105]
    assert set(first_ids) & set(second_ids) == {103}
    assert first["customers"][2] == second["customers"][0]
    assert first["pagination"] == {"page": 1, "page_size": 3, "has_more": True}
    assert second["pagination"] == {"page": 2, "page_size": 3, "has_more": False}

    raw_entries = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))["log"]["entries"]
    for tx, raw_entry in zip(selected, raw_entries, strict=True):
        assert tx.original_entry == raw_entry
        request = tx.original_entry["request"]
        headers = {header["name"].lower(): header["value"] for header in request["headers"]}
        assert headers["authorization"] == "Bearer BUGCAPSULE_FAKE_TOKEN_DO_NOT_USE"
        assert headers["cookie"] == "session=BUGCAPSULE_FAKE_SESSION"
        assert [(cookie["name"], cookie["value"]) for cookie in request["cookies"]] == [
            ("session", "BUGCAPSULE_FAKE_SESSION")
        ]

    # Full records have one consistent schema, with no missing/null-field failure.
    customers = {customer["id"]: customer for body in (first, second) for customer in body["customers"]}
    assert len(customers) == 5
    for customer in customers.values():
        assert set(customer) == {
            "id", "name", "email", "company", "account_status", "created_at", "updated_at"
        }
        assert type(customer["id"]) is int
        assert all(isinstance(value, str) and value for key, value in customer.items() if key != "id")
        assert customer["email"].endswith("@example.test")
        assert datetime.fromisoformat(customer["created_at"]) <= datetime.fromisoformat(customer["updated_at"])


def test_duplicate_pagination_fixture_har_metadata_is_consistent():
    log = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))["log"]
    assert log["version"] == "1.2"
    assert log["creator"]["name"] and log["creator"]["version"]
    assert "entirely fabricated" in log["comment"].lower()
    assert len(log["pages"]) == 1
    assert len(log["entries"]) == 2
    page = log["pages"][0]
    previous_end = datetime.fromisoformat(page["startedDateTime"])
    request_ids = []

    for number, entry in enumerate(log["entries"], start=1):
        assert entry["pageref"] == page["id"]
        started = datetime.fromisoformat(entry["startedDateTime"])
        assert started >= previous_end
        assert entry["time"] == sum(entry["timings"].values())
        assert all(value >= 0 for value in entry["timings"].values())
        previous_end = started + timedelta(milliseconds=entry["time"])

        request, response = entry["request"], entry["response"]
        assert request["url"] == (
            f"https://api.example.test/v1/customers?page={number}&page_size=3"
        )
        assert parse_qsl(urlsplit(request["url"]).query) == [
            (item["name"], item["value"]) for item in request["queryString"]
        ] == [("page", str(number)), ("page_size", "3")]
        request_headers = {header["name"].lower(): header["value"] for header in request["headers"]}
        assert request_headers["accept"] == "application/json"
        assert request_headers["origin"] == "https://app.example.test"
        assert request_headers["referer"] == "https://app.example.test/customers"
        assert request["headersSize"] == response["headersSize"] == -1
        assert request["bodySize"] == 0

        headers = {header["name"].lower(): header["value"] for header in response["headers"]}
        assert response["status"] == 200
        assert headers["content-type"] == response["content"]["mimeType"] == "application/json; charset=utf-8"
        assert headers["cache-control"] == "no-store"
        request_ids.append(headers["x-request-id"])
        text = response["content"]["text"]
        size = len(text.encode("utf-8"))
        assert response["content"]["size"] == response["bodySize"] == size
        assert int(headers["content-length"]) == size
        # The non-ASCII customer name ensures sizes count bytes, not characters.
        assert size > len(text)
        body = json.loads(text)
        assert set(body) == {"customers", "pagination"}
        assert set(body["pagination"]) == {"page", "page_size", "has_more"}
    assert len(set(request_ids)) == 2
