"""Restricted payload tests: fabricated captures only, never network/model calls."""

import base64
from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
import socket
from urllib.parse import parse_qsl, urlsplit
import urllib.request

import pytest

from app import privacy
from app.engine.har import parse_har, select_transactions
from app.engine.replace import apply_replacements, validate_plan
from app.engine.scrub import scrub_transactions
from app.privacy import PrivacyError, prepare_model_payload


FIXTURE_PATH = Path(__file__).resolve().parents[2] / "fixtures" / "duplicate-pagination.har"
PRIVATE = "FABRICATED_PRIVATE_PAYLOAD_MARKER_92741"


def raw_entries():
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))["log"]["entries"]


def fabricated(tmp_path, bodies, requests=None):
    template = raw_entries()[0]
    entries = []
    for index, body in enumerate(bodies):
        entry = deepcopy(template)
        entry["response"]["content"]["text"] = json.dumps(body)
        entry["comment"] = PRIVATE
        entry["_filename"] = f"{PRIVATE}.har"
        if requests:
            entry["request"].update(deepcopy(requests[index]))
        entries.append(entry)
    path = tmp_path / f"{PRIVATE}.har"
    path.write_text(json.dumps({"log": {"entries": entries}}), encoding="utf-8")
    return parse_har(path).transactions


def snapshots(transactions):
    return [(deepcopy(tx.model_dump()), deepcopy(tx.original_entry)) for tx in transactions]


def local_field(prepared, node, key):
    for field in node.get("fields", []):
        binding = prepared.bindings.resolve(field["node"]["ref"])
        if getattr(binding.path[-1], "key", None) == key:
            return field
    raise AssertionError("Expected local field was not found.")


def nodes(node):
    yield node
    for field in node.get("fields", []):
        yield from nodes(field["node"])
    if "items" in node:
        yield from nodes(node["items"])


def all_nodes(payload):
    for entry in payload["transactions"]:
        for target in ("query", "request_json", "response_json"):
            if target in entry:
                yield from nodes(entry[target])


def example_tokens(prepared, node, key):
    return [example["token"] for example in local_field(prepared, node, key)["node"]["examples"]]


def test_actual_fixture_is_restricted_and_duplicate_equality_is_visible():
    source = select_transactions(parse_har(FIXTURE_PATH), [0, 1])
    before = snapshots(source)
    file_before = FIXTURE_PATH.read_bytes()
    prepared = prepare_model_payload(source, "duplicate_pagination")
    payload = prepared.payload
    assert json.loads(prepared.preview_json) == payload
    assert set(payload) == {"failure_pattern", "transactions", "metadata"}
    assert payload["failure_pattern"] == "duplicate_pagination"
    assert [entry["entry_id"] for entry in payload["transactions"]] == [0, 1]
    for entry in payload["transactions"]:
        assert set(entry) == {"entry_id", "method", "status", "query", "response_json"}
        assert entry["method"] == "GET" and entry["status"] == 200
    text = prepared.preview_json
    banned = ["BUGCAPSULE_FAKE_TOKEN_DO_NOT_USE", "BUGCAPSULE_FAKE_SESSION",
              "api.example.test", "app.example.test", "https://", "Authorization",
              "Cookie", "startedDateTime", "original_entry", "timings", "mimeType",
              '"customers"', '"pagination"', '"email"', '"name"', '"id"']
    for tx in source:
        banned.append(tx.url)
        for customer in tx.response_json["customers"]:
            banned.extend(str(value) for value in customer.values())
    assert all(value not in text for value in banned)
    rows = [local_field(prepared, entry["response_json"], "customers")["node"]
            for entry in payload["transactions"]]
    assert all(row["array_lengths"] == [{"items": 3, "arrays": 1}] for row in rows)
    first = example_tokens(prepared, rows[0]["items"], "id")
    second = example_tokens(prepared, rows[1]["items"], "id")
    assert first[2] == second[0]
    assert len(set(first + second)) == 5
    assert snapshots(source) == before
    assert FIXTURE_PATH.read_bytes() == file_before


def test_internal_scrubbing_is_mandatory_and_precedes_extraction(tmp_path, monkeypatch):
    source = fabricated(tmp_path, [{"password": PRIVATE, "id": 927414}])
    calls = []
    real_scrubber = privacy.scrub_transactions

    def scrub_first(transactions):
        calls.append(transactions)
        return real_scrubber(transactions)

    monkeypatch.setattr(privacy, "scrub_transactions", scrub_first)
    prepared = prepare_model_payload(source, "missing_null_fields")
    assert calls == [source]
    response = prepared.payload["transactions"][0]["response_json"]
    assert len(response["fields"]) == 1
    assert prepared.bindings.resolve(response["fields"][0]["node"]["ref"]).path[-1].key == "id"
    assert PRIVATE not in prepared.preview_json and "927414" not in prepared.preview_json


def test_adversarial_keys_hosts_urls_comments_and_untargeted_values_do_not_pass(tmp_path):
    private_key = f"customer_{PRIVATE}@example.test"
    url = f"https://{PRIVATE.lower()}.example.test/{PRIVATE}?{private_key}={PRIVATE}&page=1"
    source = fabricated(tmp_path, [{
        private_key: {PRIVATE: PRIVATE, "free_description": f"Private text {PRIVATE}"},
        "customer_id": 927414,
    }], [{
        "url": url,
        "queryString": [{"name": name, "value": value}
                        for name, value in parse_qsl(urlsplit(url).query)],
        "headers": [{"name": f"X-{PRIVATE}", "value": PRIVATE},
                    {"name": "Authorization", "value": PRIVATE}],
    }])
    prepared = prepare_model_payload(source, "inconsistent_identifier_references")
    text = prepared.preview_json
    assert PRIVATE not in text and PRIVATE.lower() not in text
    assert "example.test" not in text and "927414" not in text
    assert "free_description" not in text and "customer_id" not in text
    assert '"url"' not in text and '"headers"' not in text and '"comment"' not in text


def test_typed_tokens_are_shared_across_requests_responses_and_entries(tmp_path):
    source = fabricated(tmp_path, [
        {"a": 1, "b": "1", "c": True, "d": 1.0, "e": False, "f": None},
        {"other_int": 1, "other_string": "1", "other_bool": True},
    ], [{"postData": {"mimeType": "application/json", "text": json.dumps({"owner": 1})}}, {}])
    prepared = prepare_model_payload(source, "inconsistent_identifier_references")
    first, second = prepared.payload["transactions"]
    scalar_nodes = [local_field(prepared, first["response_json"], key)["node"]
                    for key in ("a", "b", "c", "d", "e", "f")]
    assert [node["types"][0]["type"] for node in scalar_nodes] == [
        "integer", "string", "boolean", "number", "boolean", "null"
    ]
    tokens = [node["examples"][0]["token"] for node in scalar_nodes]
    assert len(set(tokens[:5])) == 5 and tokens[5] is None
    assert example_tokens(prepared, second["response_json"], "other_int") == [tokens[0]]
    assert example_tokens(prepared, second["response_json"], "other_string") == [tokens[1]]
    assert example_tokens(prepared, second["response_json"], "other_bool") == [tokens[2]]
    assert example_tokens(prepared, first["request_json"], "owner") == [tokens[0]]
    # The query's string "1" shares the string token, never the integer token.
    assert example_tokens(prepared, first["query"], "page") == [tokens[1]]


def test_missing_null_and_duplicate_structure_is_preserved(tmp_path):
    source = fabricated(tmp_path, [{"rows": [{"id": 7}, {"id": 7}, {"id": None}, {}]}])
    prepared = prepare_model_payload(source, "missing_null_fields")
    rows = local_field(prepared, prepared.payload["transactions"][0]["response_json"], "rows")["node"]
    assert rows["array_lengths"] == [{"items": 4, "arrays": 1}]
    field = local_field(prepared, rows["items"], "id")
    assert field["present"] == 3 and field["missing"] == 1
    assert field["node"]["types"] == [{"type": "integer", "count": 2}, {"type": "null", "count": 1}]
    assert [example["count"] for example in field["node"]["examples"]] == [2, 1]
    assert field["node"]["examples"][1] == {"type": "null", "token": None, "count": 1}


def test_labels_require_explicit_exact_approval_and_aliases_do_not_collide(tmp_path):
    source = fabricated(tmp_path, [{
        "approved": {PRIVATE: PRIVATE}, "approved_suffix": 927414,
        "field_0001": "value_0001", "node_0001": "field_0002",
    }])
    prepared = prepare_model_payload(source, "duplicate_pagination", approved_labels=frozenset({"approved"}))
    root = prepared.payload["transactions"][0]["response_json"]
    labels = [field["label"] for field in root["fields"]]
    assert labels[0] == "approved" and len(set(labels)) == len(labels)
    assert "approved_suffix" not in labels and "field_0001" not in labels
    assert PRIVATE not in prepared.preview_json
    assert '"value_0001"' not in prepared.preview_json
    assert '"node_0001"' not in prepared.preview_json
    without_approval = prepare_model_payload(source, "duplicate_pagination")
    assert '"approved"' not in without_approval.preview_json


def test_private_bindings_cannot_be_serialized_or_leak_through_repr(tmp_path):
    source = fabricated(tmp_path, [{PRIVATE: PRIVATE}])
    prepared = prepare_model_payload(source, "duplicate_pagination")
    root = prepared.payload["transactions"][0]["response_json"]
    ref = root["fields"][0]["node"]["ref"]
    binding = prepared.bindings.resolve(ref)
    assert binding.path[-1].key == PRIVATE
    for obj in (prepared, prepared.bindings, binding):
        assert PRIVATE not in repr(obj) and PRIVATE not in str(obj)
        with pytest.raises(TypeError):
            json.dumps(obj)
    assert "bindings" not in prepared.payload
    with pytest.raises(TypeError):
        asdict(prepared)
    with pytest.raises(PrivacyError, match="Unknown local binding") as error:
        prepared.bindings.resolve(PRIVATE)
    assert PRIVATE not in str(error.value)


def test_binding_resolves_to_an_existing_replacement_selector():
    source = select_transactions(parse_har(FIXTURE_PATH), [0, 1])
    prepared = prepare_model_payload(source, "duplicate_pagination")
    rows = local_field(prepared, prepared.payload["transactions"][0]["response_json"], "customers")["node"]
    ref = local_field(prepared, rows["items"], "id")["node"]["ref"]
    local = prepared.bindings.resolve(ref)
    rule = {"entry_ids": [local.entry_id], "target": local.target, "kind": "id",
            "path": [segment.model_dump() for segment in local.path]}
    assert rule["path"] == [{"key": "customers"}, {"each": "*"}, {"key": "id"}]
    # Resolution is local only; it does not itself approve or execute a plan.
    result = apply_replacements(scrub_transactions(source).transactions, validate_plan({"rules": [rule]}))
    assert result.transactions[0].response_json["customers"][0]["id"] != 101


def test_payload_copy_cannot_make_the_exact_preview_stale(tmp_path):
    prepared = prepare_model_payload(fabricated(tmp_path, [{"id": 7}]), "duplicate_pagination")
    preview = prepared.preview_json
    mutable_copy = prepared.payload
    mutable_copy["transactions"][0]["method"] = PRIVATE
    assert prepared.preview_json == preview
    assert prepared.payload == json.loads(preview)
    assert PRIVATE not in prepared.preview_json
    with pytest.raises(AttributeError):
        prepared.preview_json = PRIVATE


def test_example_limits_keep_complete_structural_summaries(tmp_path, monkeypatch):
    source = fabricated(tmp_path, [{"rows": [
        {"id": index, "optional": None if index % 2 else PRIVATE} for index in range(10)
    ] + [{}]}])
    prepared = prepare_model_payload(source, "missing_null_fields")
    root = prepared.payload["transactions"][0]["response_json"]
    rows = local_field(prepared, root, "rows")["node"]
    id_field = local_field(prepared, rows["items"], "id")
    assert len(id_field["node"]["examples"]) == privacy.MAX_EXAMPLES_PER_NODE
    assert id_field["node"]["examples_omitted"] == 7
    assert id_field["present"] == 10 and id_field["missing"] == 1
    assert rows["array_lengths"] == [{"items": 11, "arrays": 1}]
    assert prepared.payload["metadata"]["examples_truncated"] is True
    monkeypatch.setattr(privacy, "MAX_EXAMPLES_TOTAL", 1)
    limited = prepare_model_payload(source, "missing_null_fields")
    assert sum(len(node.get("examples", [])) for node in all_nodes(limited.payload)) == 1
    limited_rows = local_field(limited, limited.payload["transactions"][0]["response_json"], "rows")["node"]
    optional = local_field(limited, limited_rows["items"], "optional")
    assert optional["node"]["types"] == [{"type": "null", "count": 5}, {"type": "string", "count": 5}]
    assert optional["missing"] == 1
    assert limited.payload["metadata"]["examples_omitted"] > prepared.payload["metadata"]["examples_omitted"]


def test_nesting_at_limit_and_over_limit(tmp_path):
    body = PRIVATE
    for _ in range(privacy.MAX_DEPTH):
        body = {"child": body}
    prepare_model_payload(fabricated(tmp_path, [body]), "duplicate_pagination")
    with pytest.raises(PrivacyError, match="nesting limit") as error:
        prepare_model_payload(fabricated(tmp_path, [{"child": body}]), "duplicate_pagination")
    assert PRIVATE not in str(error.value)


@pytest.mark.parametrize("constant, value", [
    ("MAX_INPUT_NODES", 2), ("MAX_INPUT_BYTES", 10),
    ("MAX_STRUCTURE_NODES", 1), ("MAX_FIELDS_PER_OBJECT", 1),
])
def test_hard_resource_limits_have_safe_errors(tmp_path, monkeypatch, constant, value):
    source = fabricated(tmp_path, [{PRIVATE: PRIVATE, "other": None}])
    monkeypatch.setattr(privacy, constant, value)
    with pytest.raises(PrivacyError, match="limit exceeded") as error:
        prepare_model_payload(source, "duplicate_pagination")
    assert PRIVATE not in str(error.value)


def test_payload_size_limit_is_exact_utf8_preview_size(tmp_path, monkeypatch):
    source = fabricated(tmp_path, [{PRIVATE: PRIVATE}])
    prepared = prepare_model_payload(source, "duplicate_pagination")
    size = len(prepared.preview_json.encode("utf-8"))
    monkeypatch.setattr(privacy, "MAX_PAYLOAD_BYTES", size)
    assert prepare_model_payload(source, "duplicate_pagination").preview_json == prepared.preview_json
    monkeypatch.setattr(privacy, "MAX_PAYLOAD_BYTES", size - 1)
    with pytest.raises(PrivacyError, match="payload byte limit"):
        prepare_model_payload(source, "duplicate_pagination")


@pytest.mark.parametrize("pattern", [PRIVATE, "", None, 1, []])
def test_invalid_patterns_have_safe_errors(tmp_path, pattern):
    with pytest.raises(PrivacyError, match="Unsupported failure pattern") as error:
        prepare_model_payload(fabricated(tmp_path, [{"id": 1}]), pattern)
    assert PRIVATE not in str(error.value)


@pytest.mark.parametrize("labels", [
    PRIVATE, [PRIVATE], {1}, {PRIVATE * 3}, {str(index) for index in range(65)},
])
def test_invalid_label_approvals_have_safe_errors(tmp_path, labels):
    with pytest.raises(PrivacyError, match="schema-label approval") as error:
        prepare_model_payload(fabricated(tmp_path, [{"id": 1}]), "duplicate_pagination", approved_labels=labels)
    assert PRIVATE not in str(error.value)


@pytest.mark.parametrize("selection", [None, [], PRIVATE, [None]])
def test_malformed_selections_have_safe_errors(selection):
    with pytest.raises(PrivacyError) as error:
        prepare_model_payload(selection, "duplicate_pagination")
    assert PRIVATE not in str(error.value)


def test_selection_bounds_and_unique_ids(tmp_path):
    source = fabricated(tmp_path, [{"id": index} for index in range(15)])
    assert len(prepare_model_payload(source[:10], "duplicate_pagination").payload["transactions"]) == 10
    for selection in (source[:11], [source[0], source[0]]):
        with pytest.raises(PrivacyError):
            prepare_model_payload(selection, "duplicate_pagination")


def test_cross_origin_and_forged_origin_are_rejected(tmp_path):
    source = fabricated(tmp_path, [{"id": 1}, {"id": 2}], [{}, {
        "url": f"https://{PRIVATE.lower()}.example.test/v1/customers?page=1&page_size=3"
    }])
    with pytest.raises(PrivacyError, match="one API origin") as error:
        prepare_model_payload(source, "duplicate_pagination")
    assert PRIVATE.lower() not in str(error.value)
    forged = source[0].model_copy(update={"origin": PRIVATE})
    with pytest.raises(PrivacyError, match="origin") as error:
        prepare_model_payload([forged], "duplicate_pagination")
    assert PRIVATE not in str(error.value)


@pytest.mark.parametrize("change", ["method", "status", "body", "query", "malformed", "conflict"])
def test_unsupported_or_inconsistent_input_is_rejected_without_values(tmp_path, change):
    source = fabricated(tmp_path, [{"id": PRIVATE}])
    tx = source[0]
    if change == "method":
        tx = tx.model_copy(update={"method": PRIVATE})
    elif change == "status":
        tx = tx.model_copy(update={"status": 499})
    elif change == "body":
        tx.original_entry["request"]["postData"] = {
            "mimeType": "application/x-www-form-urlencoded", "params": [{"name": "value", "value": PRIVATE}]
        }
    elif change == "query":
        tx.original_entry["request"]["queryString"].append({"name": "page", "value": "1"})
        tx = tx.model_copy(update={"url": tx.url + "&page=1"})
        tx.original_entry["request"]["url"] = tx.url
    elif change == "malformed":
        tx.original_entry["request"] = None
    else:
        tx.response_json["id"] = PRIVATE + "changed"
    with pytest.raises(PrivacyError) as error:
        prepare_model_payload([tx], "duplicate_pagination")
    assert PRIVATE not in str(error.value)
    assert "input_value" not in str(error.value)
    assert "https://" not in str(error.value)


def test_no_network_or_output_logging(tmp_path, monkeypatch, capsys):
    source = fabricated(tmp_path, [{PRIVATE: PRIVATE}])

    def forbidden(*args, **kwargs):
        raise AssertionError("Network access is forbidden in the local boundary.")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    prepare_model_payload(source, "duplicate_pagination")
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


def test_tokens_reset_per_payload_and_do_not_encode_source_text(tmp_path):
    first = fabricated(tmp_path, [{"private_first": PRIVATE, "private_second": PRIVATE}])
    second = fabricated(tmp_path, [{"different_key": "OTHER_FABRICATED_VALUE", "another_key": "OTHER_FABRICATED_VALUE"}])
    first_preview = prepare_model_payload(first, "duplicate_pagination").preview_json
    second_preview = prepare_model_payload(second, "duplicate_pagination").preview_json
    # Same shape/equality pattern, different private labels and values: the
    # per-call counter output is identical, rather than source-derived.
    assert first_preview == second_preview
    assert PRIVATE not in first_preview and "OTHER_FABRICATED_VALUE" not in second_preview


def test_base64_json_is_decoded_and_only_pseudonymized_examples_leave(tmp_path):
    source = fabricated(tmp_path, [{"id": PRIVATE}])
    content = source[0].original_entry["response"]["content"]
    encoded = base64.b64encode(content["text"].encode()).decode()
    content.update(encoding="base64", text=encoded)
    source[0].original_entry["request"]["postData"] = {
        "mimeType": "application/json", "encoding": "base64", "text": encoded,
    }
    prepared = prepare_model_payload(source, "inconsistent_identifier_references")
    entry = prepared.payload["transactions"][0]
    assert example_tokens(prepared, entry["response_json"], "id") == example_tokens(prepared, entry["request_json"], "id")
    assert PRIVATE not in prepared.preview_json and encoded not in prepared.preview_json
    allowed = {"ref", "types", "fields", "array_lengths", "items", "examples", "examples_omitted"}
    for node in all_nodes(prepared.payload):
        assert set(node) <= allowed
        for example in node.get("examples", []):
            assert set(example) == {"type", "token", "count"}
        for field in node.get("fields", []):
            assert set(field) == {"label", "present", "missing", "node"}
