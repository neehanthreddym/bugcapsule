"""Explicit replacement contracts over scrubbed, entirely fabricated captures."""

import base64
from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import pytest

from app.engine.har import parse_har, select_transactions
from app.engine.replace import ReplacementError, apply_replacements, validate_plan
from app.engine.scrub import scrub_transactions


FIXTURE_PATH = Path(__file__).resolve().parents[2] / "fixtures" / "duplicate-pagination.har"
PRIVATE = "FABRICATED_SELECTED_VALUE_DO_NOT_REPORT"


def rule(path, kind="id", target="response_json", ids=None):
    return {"entry_ids": [0] if ids is None else ids, "target": target,
            "path": path, "kind": kind}


def key(name):
    return {"key": name}


def plan(*rules):
    return validate_plan({"rules": list(rules)})


def snapshots(transactions):
    return [(deepcopy(tx.model_dump()), deepcopy(tx.original_entry)) for tx in transactions]


def fabricated(tmp_path, bodies, requests=None, base64_response=False):
    template = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))["log"]["entries"][0]
    entries = []
    for index, body in enumerate(bodies):
        entry = deepcopy(template)
        content = entry["response"]["content"]
        content["text"] = json.dumps(body)
        if base64_response:
            content["encoding"] = "base64"
            content["text"] = base64.b64encode(content["text"].encode()).decode()
        if requests:
            entry["request"].update(deepcopy(requests[index]))
        entries.append(entry)
    path = tmp_path / "fabricated.har"
    path.write_text(json.dumps({"log": {"entries": entries}}), encoding="utf-8")
    return scrub_transactions(parse_har(path).transactions).transactions


def body_text(content):
    if content.get("encoding") == "base64":
        return base64.b64decode(content["text"], validate=True).decode("utf-8")
    return content["text"]


def assert_sizes(part, content):
    size = len(body_text(content).encode("utf-8"))
    assert part["bodySize"] == size
    assert part["headersSize"] == -1
    if "size" in content:
        assert content["size"] == size
    for header in part["headers"]:
        if header["name"].lower() == "content-length":
            assert header["value"] == str(size)


def assert_synchronized(transactions):
    for tx in transactions:
        request, response = tx.original_entry["request"], tx.original_entry["response"]
        assert json.loads(body_text(response["content"])) == tx.response_json
        assert_sizes(response, response["content"])
        assert tx.url == request["url"]
        assert parse_qsl(urlsplit(tx.url).query, keep_blank_values=True) == [
            (item["name"], item["value"]) for item in request["queryString"]
        ]
        if "postData" in request:
            assert_sizes(request, request["postData"])


def test_real_scrubbed_fixture_preserves_duplicate_and_replaces_all_targets():
    source = scrub_transactions(select_transactions(parse_har(FIXTURE_PATH), [0, 1])).transactions
    before = snapshots(source)
    replacement_plan = plan(*[
        rule([key("customers"), {"each": "*"}, key(field)], kind, ids=[0, 1])
        for field, kind in (("id", "id"), ("name", "name"),
                            ("email", "email"), ("company", "text"))
    ])
    plan_before = replacement_plan.model_dump()
    result = apply_replacements(source, replacement_plan)
    again = apply_replacements(source, replacement_plan)
    assert snapshots(result.transactions) == snapshots(again.transactions)
    assert result.replacements == again.replacements
    assert snapshots(source) == before
    assert replacement_plan.model_dump() == plan_before
    assert [tx.entry_id for tx in result.transactions] == [0, 1]
    first, second = [tx.response_json["customers"] for tx in result.transactions]
    assert first[2] == second[0]
    assert len({item["id"] for item in first + second}) == 5
    assert {item["id"] for item in first} & {item["id"] for item in second} == {first[2]["id"]}
    assert len({item["name"] for item in first + second}) == 5
    assert len({item["email"] for item in first + second}) == 5
    assert len({item["company"] for item in first + second}) == 5
    original_records = [item for tx in source for item in tx.response_json["customers"]]
    for field in ("id", "name", "email", "company"):
        originals = {item[field] for item in original_records}
        assert not originals & {item[field] for item in first + second}
    assert all(type(item["id"]) is int for item in first + second)
    assert all(item["email"].endswith("@example.test") for item in first + second)
    assert all(item["name"].startswith("Synthetic Person ") for item in first + second)
    assert all(item["company"].startswith("Synthetic text ") for item in first + second)
    retained = json.dumps([tx.original_entry for tx in result.transactions])
    for original in original_records:
        for field in ("name", "email", "company"):
            assert original[field] not in retained
    for old, new in zip(source, result.transactions, strict=True):
        assert old.response_json["pagination"] == new.response_json["pagination"]
        for old_record, new_record in zip(old.response_json["customers"], new.response_json["customers"], strict=True):
            for field in ("account_status", "created_at", "updated_at"):
                assert old_record[field] == new_record[field]
    assert sum(item.count for item in result.replacements) == 24
    assert_synchronized(result.transactions)
    result.transactions[0].response_json["customers"][0]["id"] = -999
    result.transactions[0].original_entry["request"]["queryString"][0]["value"] = "99"
    assert snapshots(source) == before


def test_cross_field_references_type_distinctions_and_null_missing_preservation(tmp_path):
    source = fabricated(tmp_path, [
        {"customer": {"id": 1}, "items": [
            {"owner_id": 1}, {"owner_id": 2}, {"owner_id": "1"},
            {"owner_id": "2"}, {"owner_id": True}, {"owner_id": None}, {},
        ], "pagination": {"page": 1, "count": 7, "cursor": None}},
        {"owner_id": 1, "second_id": "1"},
    ])
    result = apply_replacements(source, plan(
        rule([key("customer"), key("id")]),
        rule([key("items"), {"each": "*"}, key("owner_id")]),
        rule([key("owner_id")], ids=[1]), rule([key("second_id")], ids=[1]),
    ))
    first, second = [tx.response_json for tx in result.transactions]
    values = [item.get("owner_id") for item in first["items"]]
    assert first["customer"]["id"] == values[0] == second["owner_id"]
    assert type(values[0]) is int and type(values[1]) is int
    assert type(values[2]) is str and type(values[3]) is str
    assert values[2] == second["second_id"]
    assert len({values[0], values[1], values[2], values[3]}) == 4
    assert values[4] is True and values[5] is None
    assert first["items"][6] == {}
    assert first["pagination"] == source[0].response_json["pagination"]
    assert_synchronized(result.transactions)


@pytest.mark.parametrize("encoded", [False, True])
def test_json_request_references_and_base64_representations(tmp_path, encoded):
    text = json.dumps({"owner_id": 41, "name": "Fictional Marín", "optional": None})
    post = {"mimeType": "Application/vnd.example+JSON; charset=UTF-8", "text": text}
    if encoded:
        post.update(encoding="base64", text=base64.b64encode(text.encode()).decode())
    source = fabricated(tmp_path, [{"customer": {"id": 41}, "keep": [None, False]}], [{
        "method": "POST", "postData": post,
        "headers": [{"name": "Content-Length", "value": "999"}],
    }], base64_response=encoded)
    result = apply_replacements(source, plan(
        rule([key("customer"), key("id")]),
        rule([key("owner_id")], target="request_json"),
        rule([key("name")], "name", "request_json"),
    ))
    tx = result.transactions[0]
    body = json.loads(body_text(tx.original_entry["request"]["postData"]))
    assert body["owner_id"] == tx.response_json["customer"]["id"]
    assert body["owner_id"] != 41
    assert body["name"].startswith("Synthetic Person ")
    assert body["optional"] is None
    assert tx.response_json["keep"] == [None, False]
    if encoded:
        assert tx.original_entry["response"]["content"]["encoding"] == "base64"
        assert tx.original_entry["request"]["postData"]["encoding"] == "base64"
    assert_synchronized(result.transactions)


def test_query_and_url_share_string_replacements_with_response(tmp_path):
    url = f"https://api.example.test/v1/customers?owner_id={PRIVATE}&page=2&cursor=a%2Bb%3D&empty="
    source = fabricated(tmp_path, [{"id": PRIVATE, "count": 1}], [{
        "url": url, "queryString": [{"name": name, "value": value}
                                    for name, value in parse_qsl(urlsplit(url).query, keep_blank_values=True)],
    }])
    result = apply_replacements(source, plan(
        rule([key("id")]), rule([key("owner_id")], target="query"),
    ))
    tx = result.transactions[0]
    assert dict(parse_qsl(urlsplit(tx.url).query))["owner_id"] == tx.response_json["id"]
    assert urlsplit(tx.url).path == "/v1/customers"
    assert parse_qsl(urlsplit(tx.url).query, keep_blank_values=True)[1:] == [
        ("page", "2"), ("cursor", "a+b="), ("empty", "")
    ]
    assert PRIVATE not in json.dumps(tx.original_entry)
    assert PRIVATE not in json.dumps(tx.model_dump())
    assert PRIVATE not in json.dumps([asdict(item) for item in result.replacements])
    assert_synchronized(result.transactions)


@pytest.mark.parametrize("kind, original, reserved, expected", [
    ("id", 1, 1_000_001, 1_000_002),
    ("id", "synthetic-id-000001", "synthetic-id-000002", "synthetic-id-000003"),
    ("email", "synthetic-000001@example.test", "synthetic-000002@example.test", "synthetic-000003@example.test"),
    ("name", "Synthetic Person 000001", "Synthetic Person 000002", "Synthetic Person 000003"),
    ("text", "Synthetic text 000001", "Synthetic text 000002", "Synthetic text 000003"),
])
def test_allocation_avoids_originals_and_untargeted_collisions(tmp_path, kind, original, reserved, expected):
    source = fabricated(tmp_path, [{"target": original, "keep": reserved}])
    tx = apply_replacements(source, plan(rule([key("target")], kind))).transactions[0]
    assert tx.response_json == {"target": expected, "keep": reserved}
    assert type(tx.response_json["target"]) is type(original)


def test_distinct_originals_do_not_collapse_into_synthetic_looking_values(tmp_path):
    source = fabricated(tmp_path, [{"ids": [1_000_001, 2, 3, 2]}])
    tx = apply_replacements(source, plan(rule([key("ids"), {"each": "*"}]))).transactions[0]
    assert tx.response_json["ids"] == [1_000_002, 1_000_003, 1_000_004, 1_000_003]


def test_literal_keys_are_not_expressions_or_wildcards(tmp_path):
    source = fabricated(tmp_path, [{"*": {"a.b[0]": PRIVATE}}])
    tx = apply_replacements(source, plan(rule([key("*"), key("a.b[0]")], "text"))).transactions[0]
    assert tx.response_json == {"*": {"a.b[0]": "Synthetic text 000001"}}


def test_missing_and_null_intermediate_branches_remain_untouched(tmp_path):
    source = fabricated(tmp_path, [{"customer": {"id": 7}}, {"customer": None}, {}])
    result = apply_replacements(source, plan(rule([key("customer"), key("id")], ids=[0, 1, 2])))
    assert result.transactions[0].response_json["customer"]["id"] != 7
    assert result.transactions[1].response_json == {"customer": None}
    assert result.transactions[2].response_json == {}


@pytest.mark.parametrize("value", [None, True, False])
def test_matched_nulls_and_booleans_are_not_replaced(tmp_path, value):
    source = fabricated(tmp_path, [{"id": value}])
    result = apply_replacements(source, plan(rule([key("id")])))
    assert result.transactions[0].response_json["id"] is value
    assert result.replacements == []


@pytest.mark.parametrize("data", [
    None, [], {}, {"rules": []}, {"rules": PRIVATE},
    {"rules": [rule([key("id")], "unsupported")]},
    {"rules": [rule([key("id")], ids=[True])]},
    {"rules": [rule([key("id")], ids=[-1])]},
    {"rules": [rule([key("id")], ids=[0, 0])]},
    {"rules": [rule([key("id")], ids=[])]},
    {"rules": [rule([])]},
    {"rules": [rule([PRIVATE])]},
    {"rules": [rule([{"index": 0}])]},
    {"rules": [rule([{"key": PRIVATE, "each": "*"}])]},
    {"rules": [rule([key("id")], target="url_path")]},
    {"rules": [rule([{"each": "*"}], target="query")]},
    {"rules": [rule([key("a"), key("b")], target="query")]},
    {"rules": [rule([key("id")])], PRIVATE: PRIVATE},
])
def test_malformed_plans_have_value_free_errors(tmp_path, data):
    source = fabricated(tmp_path, [{"id": PRIVATE}])
    with pytest.raises(ReplacementError, match="Invalid replacement plan") as error:
        apply_replacements(source, data)
    assert PRIVATE not in str(error.value)
    assert "input_value" not in str(error.value)
    assert error.value.__suppress_context__


@pytest.mark.parametrize("value, kind", [
    (1.5, "id"), (1, "email"), (1, "name"), (1, "text"), ([], "id"), ({}, "text"),
])
def test_incompatible_target_types_fail_without_mutation(tmp_path, value, kind):
    source = fabricated(tmp_path, [{"target": value, "private": PRIVATE}])
    before = snapshots(source)
    with pytest.raises(ReplacementError, match="incompatible replacement target type") as error:
        apply_replacements(source, plan(rule([key("target")], kind)))
    assert PRIVATE not in str(error.value)
    assert snapshots(source) == before


def test_conflicting_kinds_for_same_field_and_equal_cross_field_values(tmp_path):
    source = fabricated(tmp_path, [{"a": PRIVATE, "b": PRIVATE}])
    before = snapshots(source)
    for second_path in ([key("a")], [key("b")]):
        with pytest.raises(ReplacementError, match="conflicting") as error:
            apply_replacements(source, plan(
                rule([key("a")], "name"), rule(second_path, "text"),
            ))
        assert PRIVATE not in str(error.value)
        assert snapshots(source) == before


def test_duplicate_identical_instructions_apply_once(tmp_path):
    source = fabricated(tmp_path, [{"id": 1}])
    result = apply_replacements(source, plan(rule([key("id")]), rule([key("id")])))
    assert sum(item.count for item in result.replacements) == 1


@pytest.mark.parametrize("target, path", [
    ("response_json", [key("missing")]), ("query", [key("missing")]),
    ("response_json", [key("items"), {"each": "*"}, key("missing")]),
])
def test_selectors_with_no_fields_fail(tmp_path, target, path):
    source = fabricated(tmp_path, [{"id": 1, "items": []}])
    with pytest.raises(ReplacementError, match="matched no fields"):
        apply_replacements(source, plan(rule(path, target=target)))


@pytest.mark.parametrize("path", [
    [key("id"), {"each": "*"}], [key("id"), key(PRIVATE)],
])
def test_wrong_selector_shapes_fail_without_values(tmp_path, path):
    source = fabricated(tmp_path, [{"id": 1}])
    with pytest.raises(ReplacementError) as error:
        apply_replacements(source, plan(rule(path)))
    assert PRIVATE not in str(error.value)


def test_ambiguous_query_and_missing_request_body_are_rejected(tmp_path):
    source = fabricated(tmp_path, [{"id": 1}], [{
        "url": "https://api.example.test/v1/customers?id=a&id=b",
        "queryString": [{"name": "id", "value": "a"}, {"name": "id", "value": "b"}],
    }])
    for target, message in (("query", "ambiguous query"), ("request_json", "JSON request body")):
        with pytest.raises(ReplacementError, match=message):
            apply_replacements(source, plan(rule([key("id")], target=target)))


def test_unknown_entry_ids_rejected(tmp_path):
    source = fabricated(tmp_path, [{"id": 1}])
    with pytest.raises(ReplacementError, match="unknown selected entry ID"):
        apply_replacements(source, plan(rule([key("id")], ids=[1])))


def test_conflicting_retained_representations_are_not_silently_overwritten(tmp_path):
    source = fabricated(tmp_path, [{"id": 1}])
    source[0].response_json["id"] = 2
    with pytest.raises(ReplacementError, match="conflicting parsed response"):
        apply_replacements(source, plan(rule([key("id")])))


def test_reports_are_counts_only_and_replacement_maps_are_not_returned(tmp_path):
    source = fabricated(tmp_path, [{"id": PRIVATE, "again": PRIVATE}])
    result = apply_replacements(source, plan(rule([key("id")]), rule([key("again")])))
    assert result.transactions[0].response_json["id"] == result.transactions[0].response_json["again"]
    assert [asdict(item) for item in result.replacements] == [
        {"entry_id": 0, "target": "response_json", "kind": "id", "count": 2}
    ]
    assert PRIVATE not in repr(result.replacements)
    assert set(vars(result)) == {"transactions", "replacements"}


def test_mutated_plan_is_revalidated_without_serialization_warnings(tmp_path, capsys):
    source = fabricated(tmp_path, [{"id": PRIVATE}])
    unsafe_plan = plan(rule([key("id")]))
    # Frozen Pydantic fields can still contain mutable lists supplied by a caller.
    unsafe_plan.rules[0].path.append(PRIVATE)
    with pytest.raises(ReplacementError, match="Invalid replacement plan") as error:
        apply_replacements(source, unsafe_plan)
    assert PRIVATE not in str(error.value)
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""
