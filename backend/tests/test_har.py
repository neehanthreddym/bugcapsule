"""Local HAR parser and selection contracts using fabricated inline captures only."""

import base64
from copy import deepcopy
import json

from pydantic import ValidationError
import pytest

from app.engine.har import HarParseError, HarSelectionError, parse_har, select_transactions
from app.schemas import HarTransaction, ParsedHar


SECRET = "FABRICATED_PRIVATE_MARKER_7391"


def entry(url="https://api.example.test/items", body=None, mime="application/json"):
    return {
        "request": {
            "method": "GET",
            "url": url,
            "headers": [{"name": "Authorization", "value": SECRET}],
            "cookies": [{"name": "session", "value": SECRET}],
        },
        "response": {
            "status": 200,
            "content": {"mimeType": mime, "text": json.dumps(body)},
        },
        "_private": {"nested": [SECRET]},
    }


def write_capture(tmp_path, entries):
    path = tmp_path / "fabricated.har"
    path.write_text(json.dumps({"log": {"entries": entries}}), encoding="utf-8")
    return path


def test_filter_ids_groups_and_original_entries(tmp_path):
    entries = [
        entry("HTTPS://user:password@API.EXAMPLE.test:443/items?token=private#secret", {"a": 1}),
        entry(mime="text/html"),
        entry("https://api.example.test/next", [1, 2]),
        entry("http://API.EXAMPLE.test:80/path", False),
        entry("https://api.example.test:8443/path", "value"),
        entry("https://other.example.test/path", None),
    ]
    before = deepcopy(entries)
    path = write_capture(tmp_path, entries)
    original_file = path.read_bytes()
    parsed = parse_har(path)

    assert isinstance(parsed, ParsedHar)
    assert [tx.entry_id for tx in parsed.transactions] == [0, 2, 3, 4, 5]
    assert parsed.by_origin == {
        "https://api.example.test": [0, 2],
        "http://api.example.test": [3],
        "https://api.example.test:8443": [4],
        "https://other.example.test": [5],
    }
    assert [(skip.entry_id, skip.reason) for skip in parsed.skipped_entries] == [
        (1, "unsupported MIME type")
    ]
    for tx in parsed.transactions:
        assert isinstance(tx, HarTransaction)
        assert tx.method == "GET"
        assert tx.status == 200
        assert tx.url == before[tx.entry_id]["request"]["url"]
        assert tx.original_entry == before[tx.entry_id]
        assert tx.response_json == json.loads(before[tx.entry_id]["response"]["content"]["text"])
    assert entries == before
    assert path.read_bytes() == original_file
    assert "original_entry" not in parsed.transactions[0].model_dump()
    assert SECRET not in repr(parsed)
    assert SECRET not in repr(parsed.transactions[0])

    # Neither parsed bodies nor selection modify the retained original entry.
    parsed.transactions[0].response_json["a"] = 999
    selected = select_transactions(parsed, [2, 0])
    assert [tx.entry_id for tx in selected] == [2, 0]
    assert parsed.transactions[0].original_entry == before[0]
    assert [tx.entry_id for tx in parsed.transactions] == [0, 2, 3, 4, 5]


@pytest.mark.parametrize("mime", [
    "application/json", "APPLICATION/JSON; charset=UTF-8",
    " application/problem+JSON ; charset=utf-8", "Application/vnd.example+json",
])
@pytest.mark.parametrize("encoded", [False, True])
def test_mime_base64_and_exact_json_types(tmp_path, mime, encoded):
    body = {
        "items": [None, True, False, 7, 2.5, "7", {"empty": []}],
        "missing": None,
        "label": "héllo",
    }
    original = entry(body=body, mime=mime)
    if encoded:
        content = original["response"]["content"]
        content["text"] = base64.b64encode(
            json.dumps(body, ensure_ascii=False).encode("utf-8")
        ).decode("ascii")
        content["encoding"] = "base64"
    tx = parse_har(write_capture(tmp_path, [original])).transactions[0]
    assert tx.response_json == body
    assert [type(value) for value in tx.response_json["items"]] == [
        type(None), bool, bool, int, float, str, dict
    ]
    assert tx.original_entry == original


@pytest.mark.parametrize("body", [None, True, False, 0, 1.25, "héllo", [], [3, 2, 1], {}])
def test_top_level_json_values(tmp_path, body):
    parsed_body = parse_har(write_capture(tmp_path, [entry(body=body)])).transactions[0].response_json
    assert parsed_body == body
    assert type(parsed_body) is type(body)


@pytest.mark.parametrize("url, origin", [
    ("http://EXAMPLE.test:80/a?q=1#frag", "http://example.test"),
    ("https://EXAMPLE.test:443/a", "https://example.test"),
    ("http://example.test:443/a", "http://example.test:443"),
    ("https://user:password@[2001:DB8::1]:443/a", "https://[2001:db8::1]"),
    ("http://[::1]:8080/a", "http://[::1]:8080"),
])
def test_normalized_origin(tmp_path, url, origin):
    tx = parse_har(write_capture(tmp_path, [entry(url)])).transactions[0]
    assert tx.origin == origin
    assert tx.url == url


@pytest.mark.parametrize("mime", [
    "text/json", "application/jsonp", "application/+json", "image/png", "text/html",
])
def test_unsupported_mime_does_not_require_response_text(tmp_path, mime):
    original = entry(mime=mime)
    del original["response"]["content"]["text"]
    capture = parse_har(write_capture(tmp_path, [original]))
    assert capture.transactions == []
    assert capture.by_origin == {}
    assert capture.skipped_entries[0].entry_id == 0
    assert capture.skipped_entries[0].reason == "unsupported MIME type"


def test_other_unsupported_content_has_fixed_reasons(tmp_path):
    encoded = entry(body={"private": SECRET})
    encoded["response"]["content"]["encoding"] = SECRET
    parsed = parse_har(write_capture(tmp_path, [entry("ftp://example.test/a"), encoded]))
    assert parsed.transactions == []
    assert [skip.reason for skip in parsed.skipped_entries] == [
        "unsupported URL scheme", "unsupported content encoding"
    ]
    assert SECRET not in repr(parsed)


@pytest.mark.parametrize("capture", [
    None, [], {}, {"log": None}, {"log": []}, {"log": SECRET},
    {"log": {}}, {"log": {"entries": None}}, {"log": {"entries": {}}},
    {"log": {"entries": SECRET}},
])
def test_invalid_envelopes(tmp_path, capture):
    path = tmp_path / "fabricated.har"
    path.write_text(json.dumps(capture), encoding="utf-8")
    with pytest.raises(HarParseError, match="HAR log") as error:
        parse_har(path)
    assert SECRET not in str(error.value)


def test_empty_capture_is_valid_but_not_selectable(tmp_path):
    capture = parse_har(write_capture(tmp_path, []))
    assert capture.transactions == []
    assert capture.skipped_entries == []
    with pytest.raises(HarSelectionError, match="unknown"):
        select_transactions(capture, [0])


@pytest.mark.parametrize("raw", [b"{not-json", b"\xff", b'{"log": NaN}'])
def test_invalid_capture_json_or_utf8(tmp_path, raw):
    path = tmp_path / "fabricated.har"
    path.write_bytes(raw)
    with pytest.raises(HarParseError, match="valid UTF-8 JSON"):
        parse_har(path)


def test_file_read_error_is_private(tmp_path):
    with pytest.raises(HarParseError, match="Could not read") as error:
        parse_har(tmp_path / SECRET)
    assert SECRET not in str(error.value)


@pytest.mark.parametrize("keys, value, expected", [
    ((), None, "entry must be an object"),
    (("request",), None, "request and response"),
    (("response",), SECRET, "request and response"),
    (("request", "method"), None, "request.method"),
    (("request", "method"), "", "request.method"),
    (("request", "method"), "GET BAD", "request.method"),
    (("request", "url"), None, "request.url"),
    (("request", "url"), "", "request.url"),
    (("request", "url"), SECRET, "request.url"),
    (("request", "url"), f"https://{SECRET}:invalid/a", "request.url"),
    (("request", "url"), f"https://[{SECRET}]/a", "request.url"),
    (("request", "url"), "https:///path", "request.url"),
    (("request", "url"), "https://example.test:/path", "request.url"),
    (("request", "url"), "https://example.test:65536/path", "request.url"),
    (("request", "url"), "https://example.test/\nsecret", "request.url"),
    (("request", "url"), "https://example.test/\\secret", "request.url"),
    (("response", "status"), SECRET, "response.status"),
    (("response", "status"), True, "response.status"),
    (("response", "status"), 0, "response.status"),
    (("response", "status"), 600, "response.status"),
    (("response", "content"), [], "response.content"),
    (("response", "content", "mimeType"), None, "content.mimeType"),
    (("response", "content", "mimeType"), "", "content.mimeType"),
    (("response", "content", "text"), None, "content.text"),
    (("response", "content", "encoding"), None, "content.encoding"),
    (("response", "content", "encoding"), 1, "content.encoding"),
])
def test_malformed_fields_identify_entry_without_values(tmp_path, keys, value, expected):
    original = entry(url=f"https://user:{SECRET}@example.test/path?secret={SECRET}", body=SECRET)
    if not keys:
        original = value
    else:
        target = original
        for key in keys[:-1]:
            target = target[key]
        target[keys[-1]] = value
    with pytest.raises(HarParseError) as error:
        parse_har(write_capture(tmp_path, [entry(), original]))
    message = str(error.value)
    assert "Entry 1:" in message
    assert expected in message
    assert SECRET not in message
    assert "https://" not in message
    assert "Authorization" not in message
    assert "input_value" not in message
    assert error.value.__suppress_context__ or error.value.__context__ is None


@pytest.mark.parametrize("keys", [
    ("request",), ("response",), ("request", "method"), ("request", "url"),
    ("response", "status"), ("response", "content"),
    ("response", "content", "mimeType"), ("response", "content", "text"),
])
def test_missing_required_fields(tmp_path, keys):
    original = entry()
    target = original
    for key in keys[:-1]:
        target = target[key]
    del target[keys[-1]]
    with pytest.raises(HarParseError, match="Entry 0:"):
        parse_har(write_capture(tmp_path, [original]))


def test_required_fields_do_not_disappear_for_unsupported_mime(tmp_path):
    original = entry(mime="image/png")
    del original["request"]["method"]
    with pytest.raises(HarParseError, match="Entry 0: invalid request.method"):
        parse_har(write_capture(tmp_path, [original]))


@pytest.mark.parametrize("text", [
    f'{{"private": "{SECRET}",}}', "", "NaN", "Infinity", "-Infinity", "1e400",
])
@pytest.mark.parametrize("encoded", [False, True])
def test_invalid_declared_json_is_error_not_skip(tmp_path, text, encoded):
    original = entry()
    content = original["response"]["content"]
    content["text"] = text
    if encoded:
        content["text"] = base64.b64encode(text.encode()).decode()
        content["encoding"] = "base64"
    with pytest.raises(HarParseError, match="Entry 0:") as error:
        parse_har(write_capture(tmp_path, [original]))
    assert SECRET not in str(error.value)
    assert "input_value" not in str(error.value)


@pytest.mark.parametrize("text", [
    f"!!{SECRET}!!", "e30", "e30=\n", "e30=extraneous", "é", "/w==",
])
def test_strict_base64_and_utf8_errors(tmp_path, text):
    original = entry()
    original["response"]["content"].update(encoding="base64", text=text)
    with pytest.raises(HarParseError, match="Entry 0: invalid base64 or UTF-8") as error:
        parse_har(write_capture(tmp_path, [original]))
    assert SECRET not in str(error.value)


@pytest.mark.parametrize("ids, expected", [
    ([], "1–10"), ([0] * 11, "1–10"), ([0, 0], "unique"),
    ([-1], "nonnegative"), ([999], "unknown"), ([1], "unsupported"),
    ([0, 2], "exactly one API origin"), ([0, 3], "exactly one API origin"),
    ([True], "integers"), ([0.0], "integers"), ([SECRET], "integers"),
    ([None], "integers"), (SECRET, "sequence"), (None, "sequence"),
    ({0}, "sequence"),
])
def test_invalid_selections(tmp_path, ids, expected):
    capture = parse_har(write_capture(tmp_path, [
        entry(), entry(mime="text/html"), entry("https://other.example.test/a"),
        entry("https://api.example.test:8443/a"),
    ]))
    before = capture.model_dump()
    with pytest.raises(HarSelectionError, match=expected) as error:
        select_transactions(capture, ids)
    assert SECRET not in str(error.value)
    assert capture.model_dump() == before


def test_ten_transaction_selection_from_larger_capture(tmp_path):
    capture = parse_har(write_capture(tmp_path, [
        entry(f"https://API.EXAMPLE.test:443/items?page={index}", {"page": index})
        for index in range(15)
    ]))
    assert len(capture.transactions) == 15
    ids = list(range(14, 4, -1))
    selected = select_transactions(capture, ids)
    assert [tx.entry_id for tx in selected] == ids
    assert len(selected) == 10
    assert select_transactions(capture, [0]) == [capture.transactions[0]]
    with pytest.raises(HarSelectionError, match="1–10"):
        select_transactions(capture, list(range(11)))


def test_pydantic_validation_text_hides_private_input():
    with pytest.raises(ValidationError) as error:
        HarTransaction(
            entry_id=0, method=SECRET, url=SECRET, origin=SECRET,
            status=SECRET, response_json={"private": SECRET}, original_entry={},
        )
    assert SECRET not in str(error.value)
    assert "input_value" not in str(error.value)
