"""Full-data preservation checks over the fixture and fabricated inline captures."""

from copy import deepcopy
import json
from pathlib import Path
import socket
import urllib.request

import pytest

from app.agent.schemas import CheckProposal
from app.engine.har import parse_har, select_transactions
from app.engine.replace import apply_replacements, validate_plan
from app.engine.scrub import scrub_transactions
from app.privacy import prepare_model_payload
from app.verify.checks import (
    CheckConfigurationError, check_from_proposal, validate_check, verify_failure,
    verify_duplicate_pagination, verify_missing_null_fields, verify_inconsistent_references,
)


FIXTURE_PATH = Path(__file__).resolve().parents[2] / "fixtures" / "duplicate-pagination.har"
PRIVATE = "FABRICATED_PRIVATE_SELECTOR_AND_VALUE_927414"
# Recorded check example supplied by the user from the earlier live run.
# These constants do not represent a new model call or a newly verified proposal.
RECORDED_DUPLICATE_REFS = ["node_0007", "node_0024"]


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("No network calls are allowed in verifier tests.")
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)


def path(*keys):
    return [{"each": "*"} if key is None else {"key": key} for key in keys]


def selector(entry_id, *keys, target="response_json"):
    return {"entry_id": entry_id, "target": target, "path": path(*keys)}


def duplicate_config(ids=(0, 1), fields=("records", None, "identity")):
    return validate_check({"type": "duplicate_pagination", "pages": [
        {"fields": [selector(entry_id, *fields)]} for entry_id in ids
    ]})


def missing_config(*selectors):
    return validate_check({"type": "missing_null_fields", "fields": list(selectors)})


def reference_config(definitions=None, references=None):
    return validate_check({
        "type": "inconsistent_references",
        "definitions": definitions or [selector(0, "definitions", None, "code")],
        "references": references or [selector(0, "links", None, "target")],
    })


def fabricated(tmp_path, bodies, requests=None):
    template = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))["log"]["entries"][0]
    entries = []
    for index, body in enumerate(bodies):
        entry = deepcopy(template)
        entry["response"]["content"]["text"] = json.dumps(body)
        if requests:
            entry["request"].update(deepcopy(requests[index]))
        entries.append(entry)
    file = tmp_path / "fabricated.har"
    file.write_text(json.dumps({"log": {"entries": entries}}), encoding="utf-8")
    return scrub_transactions(parse_har(file).transactions).transactions


def snapshots(transactions):
    return [(deepcopy(tx.model_dump()), deepcopy(tx.original_entry)) for tx in transactions]


def transformed_bodies(tmp_path, bodies, requests=None):
    # Construct coherent retained HAR/parsed representations through the real parser.
    return fabricated(tmp_path, bodies, requests)


def test_recorded_fixture_check_after_real_scrubbing_and_replacement():
    baseline = scrub_transactions(select_transactions(parse_har(FIXTURE_PATH), [0, 1])).transactions
    prepared = prepare_model_payload(baseline, "duplicate_pagination")
    recorded = CheckProposal(type="duplicate_pagination", refs=RECORDED_DUPLICATE_REFS,
                             note="Recorded example supplied by user; no new model call.")
    check = check_from_proposal(recorded, prepared.bindings)
    rules = [{"entry_ids": [0, 1], "target": "response_json", "kind": kind,
              "path": path("customers", None, field)}
             for field, kind in (("id", "id"), ("name", "name"), ("email", "email"), ("company", "text"))]
    transformed = apply_replacements(baseline, validate_plan({"rules": rules})).transactions
    before, after = snapshots(baseline), snapshots(transformed)
    result = verify_duplicate_pagination(baseline, transformed, check)
    assert result.outcome == "preserved" and result.reason == "failure_preserved"
    assert result.baseline.observations == result.transformed.observations == 6
    assert result.baseline.failures == result.transformed.failures == 1
    assert result.baseline.distinct_identities == result.transformed.distinct_identities == 5
    locations = result.baseline.equality_groups[0]
    assert [(item.entry_id, item.record, item.refs) for item in locations] == [
        (0, (2,), ("node_0007",)), (1, (0,), ("node_0024",))
    ]
    assert snapshots(baseline) == before and snapshots(transformed) == after
    text = result.model_dump_json() + repr(result)
    for forbidden in ("Maya Chen", "Sofia", "api.example.test", "BUGCAPSULE_FAKE", '"id"', '"customers"', "101", "103"):
        assert forbidden not in text


@pytest.mark.parametrize("change, reason", [
    ("destroy", "failure_removed"), ("different_duplicate", "equality_relationships_changed"),
    ("collapse", "equality_relationships_changed"), ("count", "record_counts_changed"),
    ("within_page_collapse", "equality_relationships_changed"),
])
def test_fixture_duplicate_negative_cases(tmp_path, change, reason):
    baseline = scrub_transactions(select_transactions(parse_har(FIXTURE_PATH), [0, 1])).transactions
    bodies = [deepcopy(tx.response_json) for tx in baseline]
    if change == "destroy":
        bodies[1]["customers"][0]["id"] = 106
    elif change == "different_duplicate":
        bodies[1]["customers"][0]["id"] = 102
    elif change == "collapse":
        for body in bodies:
            for record in body["customers"]:
                record["id"] = 7
    elif change == "within_page_collapse":
        bodies[0]["customers"][1]["id"] = 101
    else:
        bodies[1]["customers"].append(deepcopy(bodies[1]["customers"][-1]))
    check = duplicate_config(fields=("customers", None, "id"))
    result = verify_failure(baseline, transformed_bodies(tmp_path, bodies), check)
    assert result.outcome == "changed" and result.reason == reason
    if change in {"different_duplicate", "within_page_collapse"}:
        assert result.baseline.failures == result.transformed.failures == 1


def test_duplicate_typed_composite_identity_and_healthy_control(tmp_path):
    bodies = [
        {"records": [{"identity": 1, "scope": "north"}, {"identity": 1, "scope": "south"}]},
        {"records": [{"identity": 1, "scope": "north"}, {"identity": "1", "scope": "north"}]},
    ]
    baseline = fabricated(tmp_path, bodies)
    check = validate_check({"type": "duplicate_pagination", "pages": [
        {"fields": [selector(entry_id, "records", None, field) for field in ("identity", "scope")]}
        for entry_id in (0, 1)
    ]})
    result = verify_failure(baseline, baseline, check)
    assert result.outcome == "preserved" and result.baseline.failures == 1
    assert result.baseline.distinct_identities == 3
    bodies[1]["records"][0]["scope"] = "east"
    healthy = fabricated(tmp_path, bodies)
    result = verify_failure(healthy, baseline, check)
    assert result.outcome == "not_present" and result.reason == "baseline_failure_absent"


def test_duplicate_strict_types_cannot_turn_integer_into_string(tmp_path):
    baseline = fabricated(tmp_path, [{"records": [{"identity": 1}]}, {"records": [{"identity": 1}]}])
    transformed = fabricated(tmp_path, [{"records": [{"identity": "x"}]}, {"records": [{"identity": "x"}]}])
    result = verify_failure(baseline, transformed, duplicate_config())
    assert result.outcome == "changed" and result.reason == "value_types_changed"
    healthy = fabricated(tmp_path, [{"records": [{"identity": 1}]}, {"records": [{"identity": "1"}]}])
    assert verify_failure(healthy, healthy, duplicate_config()).outcome == "not_present"


@pytest.mark.parametrize("value", [None, True, False, 1.5, {}, []])
def test_unsupported_duplicate_identity_types_are_inconclusive(tmp_path, value):
    baseline = fabricated(tmp_path, [{"records": [{"identity": value}]}, {"records": [{"identity": value}]}])
    assert verify_failure(baseline, baseline, duplicate_config()).outcome == "inconclusive"


def test_full_data_check_is_not_limited_by_model_examples(tmp_path):
    baseline = fabricated(tmp_path, [
        {"records": [{"identity": index} for index in range(7)]},
        {"records": [{"identity": index} for index in (8, 9, 10, 11, 12, 13, 6)]},
    ])
    prepared = prepare_model_payload(baseline, "duplicate_pagination")
    assert prepared.payload["metadata"]["examples_truncated"] is True
    refs = [entry["response_json"]["fields"][0]["node"]["items"]["fields"][0]["node"]["ref"]
            for entry in prepared.payload["transactions"]]
    check = check_from_proposal(CheckProposal(type="duplicate_pagination", refs=refs), prepared.bindings)
    result = verify_failure(baseline, baseline, check)
    assert result.outcome == "preserved" and result.baseline.observations == 14
    assert [item.record for item in result.baseline.equality_groups[0]] == [(6,), (6,)]


def test_missing_null_states_preserve_positions_and_leave_present_values_private(tmp_path):
    bodies = [{"records": [{"optional": None}, {}, {"optional": PRIVATE}]}]
    baseline = fabricated(tmp_path, bodies)
    bodies[0]["records"][2]["optional"] = "Synthetic text"
    transformed = fabricated(tmp_path, bodies)
    check = missing_config(selector(0, "records", None, "optional"))
    before, after = snapshots(baseline), snapshots(transformed)
    result = verify_missing_null_fields(baseline, transformed, check)
    assert result.outcome == "preserved" and result.baseline.failures == 2
    assert [item.state for item in result.baseline.fields] == ["null", "missing", "present"]
    assert [item.location.record for item in result.baseline.fields] == [(0,), (1,), (2,)]
    assert PRIVATE not in result.model_dump_json() and "optional" not in repr(result)
    assert snapshots(baseline) == before and snapshots(transformed) == after


@pytest.mark.parametrize("records", [
    [{}, {"optional": None}, {"optional": PRIVATE}],
    [{"optional": "fixed"}, {"optional": "fixed"}, {"optional": PRIVATE}],
    [{"optional": None}, {}, {"optional": PRIVATE}, {}],
])
def test_missing_null_negative_cases(tmp_path, records):
    baseline = fabricated(tmp_path, [{"records": [{"optional": None}, {}, {"optional": PRIVATE}]}])
    transformed = fabricated(tmp_path, [{"records": records}])
    result = verify_failure(baseline, transformed, missing_config(selector(0, "records", None, "optional")))
    assert result.outcome == "changed"


def test_explicit_all_missing_key_and_healthy_present_control(tmp_path):
    missing = fabricated(tmp_path, [{"records": [{}, {}]}])
    check = missing_config(selector(0, "records", None, PRIVATE))
    assert verify_failure(missing, missing, check).outcome == "preserved"
    healthy = fabricated(tmp_path, [{"records": [{PRIVATE: 1}, {PRIVATE: 2}]}])
    result = verify_failure(healthy, missing, check)
    assert result.outcome == "not_present"
    assert PRIVATE not in result.model_dump_json()


def test_missing_nested_record_shape_changes_even_when_flat_count_matches(tmp_path):
    baseline = fabricated(tmp_path, [{"groups": [{"rows": [{}, {"x": None}]}, {"rows": [{"x": "ok"}]}]}])
    transformed = fabricated(tmp_path, [{"groups": [{"rows": [{}]}, {"rows": [{"x": None}, {"x": "ok"}]}]}])
    check = missing_config(selector(0, "groups", None, "rows", None, "x"))
    result = verify_failure(baseline, transformed, check)
    assert result.outcome == "changed" and result.reason == "record_locations_changed"


def test_present_boolean_and_integer_are_not_conflated_by_state_checks(tmp_path):
    baseline = fabricated(tmp_path, [{"records": [{"x": None}, {"x": True}]}])
    transformed = fabricated(tmp_path, [{"records": [{"x": None}, {"x": 1}]}])
    result = verify_failure(baseline, transformed, missing_config(selector(0, "records", None, "x")))
    assert result.outcome == "changed" and result.reason == "value_types_changed"


def test_inconsistent_reference_graph_preserves_exact_definition_targets(tmp_path):
    baseline = fabricated(tmp_path, [{"definitions": [{"code": 1}, {"code": 2}], "links": [{"target": 1}, {"target": 9}]}])
    transformed = fabricated(tmp_path, [{"definitions": [{"code": 101}, {"code": 102}], "links": [{"target": 101}, {"target": 109}]}])
    before, after = snapshots(baseline), snapshots(transformed)
    result = verify_inconsistent_references(baseline, transformed, reference_config())
    assert result.outcome == "preserved" and result.baseline.failures == 1
    assert [item.state for item in result.baseline.resolutions] == ["resolved", "dangling"]
    assert result.baseline.resolutions[0].definitions[0].record == (0,)
    assert result.baseline.resolutions[1].definitions == ()
    assert snapshots(baseline) == before and snapshots(transformed) == after


@pytest.mark.parametrize("definitions, links", [
    ([1, 2], [1, 2]),  # Repair the dangling reference.
    ([1, 2], [2, 9]),  # Keep one dangling, but resolve the other to a different definition.
    ([1, 2], [9, 1]),  # Keep one dangling, at a different occurrence.
    ([1, 1], [1, 9]),  # Collapse distinct definitions without changing dangling count.
    ([1, 2], [1, 9, 9]),
])
def test_reference_graph_negative_cases(tmp_path, definitions, links):
    baseline = fabricated(tmp_path, [{"definitions": [{"code": 1}, {"code": 2}], "links": [{"target": 1}, {"target": 9}]}])
    transformed = fabricated(tmp_path, [{"definitions": [{"code": value} for value in definitions],
                                       "links": [{"target": value} for value in links]}])
    result = verify_failure(baseline, transformed, reference_config())
    assert result.outcome == "changed"


def test_reference_strict_types_empty_definitions_and_healthy_controls(tmp_path):
    baseline = fabricated(tmp_path, [{"definitions": [{"code": 1}], "links": [{"target": "1"}]}])
    assert verify_failure(baseline, baseline, reference_config()).outcome == "preserved"
    healthy = fabricated(tmp_path, [{"definitions": [{"code": 1}], "links": [{"target": 1}]}])
    assert verify_failure(healthy, healthy, reference_config()).outcome == "not_present"
    empty = fabricated(tmp_path, [{"definitions": [], "links": [{"target": 1}]}])
    assert verify_failure(empty, empty, reference_config()).outcome == "preserved"
    incompatible = fabricated(tmp_path, [{"definitions": [{"code": 1}], "links": [{"target": True}]}])
    assert verify_failure(incompatible, incompatible, reference_config()).outcome == "inconclusive"


def test_distinct_dangling_references_cannot_collapse_with_same_failure_count(tmp_path):
    baseline = fabricated(tmp_path, [{"definitions": [{"code": 1}], "links": [{"target": 8}, {"target": 9}]}])
    transformed = fabricated(tmp_path, [{"definitions": [{"code": 1}], "links": [{"target": 99}, {"target": 99}]}])
    result = verify_failure(baseline, transformed, reference_config())
    assert result.baseline.failures == result.transformed.failures == 2
    assert result.outcome == "changed" and result.reason == "reference_resolution_changed"


def test_reference_request_body_and_query_targets(tmp_path):
    requests = [{"postData": {"mimeType": "application/json", "text": json.dumps({"link": "good"})}}]
    baseline = fabricated(tmp_path, [{"definitions": [{"code": "good"}]}], requests)
    check = reference_config(
        definitions=[selector(0, "definitions", None, "code")],
        references=[selector(0, "link", target="request_json"), selector(0, "page", target="query")],
    )
    result = verify_failure(baseline, baseline, check)
    assert result.outcome == "preserved"
    assert [item.state for item in result.baseline.resolutions] == ["resolved", "dangling"]
    assert result.baseline.resolutions[1].location.target == "query"


def test_reference_proposal_requires_explicit_roles_and_keeps_agent_identifier(tmp_path):
    baseline = fabricated(tmp_path, [{"definitions": [{"code": 1}], "links": [{"target": 9}]}])
    prepared = prepare_model_payload(baseline, "inconsistent_identifier_references")
    root = prepared.payload["transactions"][0]["response_json"]
    refs = [field["node"]["items"]["fields"][0]["node"]["ref"] for field in root["fields"]]
    proposal = CheckProposal(type="inconsistent_identifier_references", refs=refs, note=PRIVATE)
    with pytest.raises(CheckConfigurationError):
        check_from_proposal(proposal, prepared.bindings)
    check = check_from_proposal(proposal, prepared.bindings, definition_refs=[refs[0]])
    assert check.type == "inconsistent_references"
    result = verify_failure(baseline, baseline, check)
    assert result.outcome == "preserved" and PRIVATE not in result.model_dump_json()
    assert proposal.type == "inconsistent_identifier_references"


@pytest.mark.parametrize("data", [
    {}, None, PRIVATE, {"type": "extra_failure"},
    {"type": "missing_null_fields", "fields": []},
    {"type": "missing_null_fields", "fields": [selector(True, "x")]},
    {"type": "missing_null_fields", "fields": [{"entry_id": 0, "path": [PRIVATE]}]},
    {"type": "missing_null_fields", "fields": [{"entry_id": 0, "path": [{"index": 0}]}]},
    {"type": "missing_null_fields", "fields": [{**selector(0, "x"), "ref": PRIVATE}]},
    {"type": "missing_null_fields", "fields": [selector(0, "x"), selector(0, "x")]},
    {"type": "duplicate_pagination", "pages": [{"fields": [selector(0, "x")]}]},
    {"type": "duplicate_pagination", "pages": [{"fields": [selector(0, "x")]}, {"fields": [selector(0, "y")]}]},
    {"type": "inconsistent_references", "definitions": [selector(0, "x")], "references": [selector(0, "x")]},
])
def test_invalid_configurations_never_echo_private_selectors(data):
    with pytest.raises(CheckConfigurationError, match="Invalid check configuration") as caught:
        validate_check(data)
    assert PRIVATE not in str(caught.value) and "input_value" not in str(caught.value)


@pytest.mark.parametrize("body, check_type", [
    ({"records": []}, "missing"),
    ({"definitions": [{"code": 1}], "links": []}, "reference"),
    ({"records": None}, "missing"),
    ({"records": [True]}, "missing"),
])
def test_insufficient_or_incompatible_evidence_is_inconclusive(tmp_path, body, check_type):
    baseline = fabricated(tmp_path, [body])
    check = missing_config(selector(0, "records", None, PRIVATE)) if check_type == "missing" else reference_config()
    result = verify_failure(baseline, baseline, check)
    assert result.outcome == "inconclusive" and PRIVATE not in result.model_dump_json()


def test_unknown_entries_invalid_inputs_and_changed_entry_sets(tmp_path):
    baseline = fabricated(tmp_path, [{"x": None}, {"x": None}])
    check = missing_config(selector(0, "x"))
    assert verify_failure([], baseline, check).outcome == "inconclusive"
    assert verify_failure(baseline, baseline[:1], check).reason == "entry_set_changed"
    with pytest.raises(CheckConfigurationError, match="Unknown selected entry ID"):
        verify_failure(baseline, baseline, missing_config(selector(9, PRIVATE)))
    malformed = deepcopy(baseline)
    malformed[0].response_json["x"] = PRIVATE
    assert verify_failure(baseline, malformed, check).outcome == "inconclusive"


def test_private_reference_reports_and_wrapper_type_safety(tmp_path):
    bodies = [{"definitions": [{PRIVATE: 927414}], "links": [{PRIVATE: PRIVATE}]}]
    baseline = fabricated(tmp_path, bodies)
    check = reference_config([selector(0, "definitions", None, PRIVATE)], [selector(0, "links", None, PRIVATE)])
    result = verify_failure(baseline, baseline, check)
    text = result.model_dump_json() + repr(result)
    for secret in (PRIVATE, "927414", "api.example.test", "BUGCAPSULE_FAKE", "https://"):
        assert secret not in text
    with pytest.raises(CheckConfigurationError):
        verify_duplicate_pagination(baseline, baseline, check)
