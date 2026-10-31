"""Protect preservation decisions when exception records age or are malformed."""

from copy import deepcopy
from datetime import date
import json
from pathlib import Path

import pytest

from bus_sync.exceptions import load_exceptions


@pytest.fixture
def exception_document():
    return {
        "schema_version": 1,
        "defaults": {
            "unmatched_osm_action": "preserve",
            "expired_exception_action": "preserve_and_review",
        },
        "exceptions": [
            {
                "id": "shuttle-service",
                "osm_objects": ["relation/20399451"],
                "classification": "valid_outside_source_scope",
                "action": "preserve",
                "reason": "The operator confirms a service outside the selected feeds.",
                "evidence_urls": ["https://www.yonsei.ac.kr/sc/363/subview.do"],
                "verified_at": "2026-10-09",
                "review_after": "2026-12-22",
                "reviewer": "test-reviewer",
                "scope": "Service existence only; route geometry has not been checked.",
            }
        ],
    }


def write_document(tmp_path, document):
    path = tmp_path / "exceptions.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("today", "review_due"),
    [
        (date(2026, 12, 21), False),
        (date(2026, 12, 22), True),
        (date(2030, 1, 1), True),
    ],
)
def test_review_date_never_changes_preservation(
    tmp_path, exception_document, today, review_due
):
    path = write_document(tmp_path, exception_document)
    loaded = load_exceptions(path, today=today)

    assert loaded["defaults"]["unmatched_osm_action"] == "preserve"
    assert loaded["defaults"]["expired_exception_action"] == "preserve_and_review"
    assert loaded["exceptions"][0]["action"] == "preserve"
    assert loaded["exceptions"][0]["review_due"] is review_due
    # Derived review state must not silently rewrite the reviewed source record.
    assert json.loads(path.read_text(encoding="utf-8")) == exception_document


@pytest.mark.parametrize("action", ["delete", "ignore", "replace", ""])
def test_rejects_nonpreserving_exception_actions(
    tmp_path, exception_document, action
):
    exception_document["exceptions"][0]["action"] = action
    with pytest.raises(ValueError):
        load_exceptions(write_document(tmp_path, exception_document))


@pytest.mark.parametrize(
    "default_key", ["unmatched_osm_action", "expired_exception_action"]
)
def test_rejects_destructive_defaults(tmp_path, exception_document, default_key):
    exception_document["defaults"][default_key] = "delete"
    with pytest.raises(ValueError):
        load_exceptions(write_document(tmp_path, exception_document))


def test_rejects_duplicate_exception_ids(tmp_path, exception_document):
    duplicate = deepcopy(exception_document["exceptions"][0])
    duplicate["osm_objects"] = ["relation/20399452"]
    exception_document["exceptions"].append(duplicate)
    with pytest.raises(ValueError):
        load_exceptions(write_document(tmp_path, exception_document))


def test_rejects_object_assigned_to_multiple_exceptions(tmp_path, exception_document):
    duplicate = deepcopy(exception_document["exceptions"][0])
    duplicate["id"] = "another-service"
    exception_document["exceptions"].append(duplicate)
    with pytest.raises(ValueError):
        load_exceptions(write_document(tmp_path, exception_document))


@pytest.mark.parametrize(
    "object_ref",
    ["20399451", "node/-1", "node/0", "node/abc", "node/1/2", "area/1"],
)
def test_rejects_invalid_osm_object_references(
    tmp_path, exception_document, object_ref
):
    exception_document["exceptions"][0]["osm_objects"] = [object_ref]
    with pytest.raises(ValueError):
        load_exceptions(write_document(tmp_path, exception_document))


@pytest.mark.parametrize("field", ["verified_at", "review_after"])
def test_rejects_invalid_review_dates(tmp_path, exception_document, field):
    exception_document["exceptions"][0][field] = "2026-02-30"
    with pytest.raises(ValueError):
        load_exceptions(write_document(tmp_path, exception_document))


@pytest.mark.parametrize("schema_version", [0, 2, "1"])
def test_rejects_unknown_schema_version(tmp_path, exception_document, schema_version):
    exception_document["schema_version"] = schema_version
    with pytest.raises(ValueError):
        load_exceptions(write_document(tmp_path, exception_document))


def test_seeded_exceptions_distinguish_services_from_stop_candidates():
    path = Path(__file__).resolve().parents[1] / "config" / "exceptions.json"
    loaded = load_exceptions(path, today=date(2030, 1, 1))
    by_object = {
        osm_object: entry
        for entry in loaded["exceptions"]
        for osm_object in entry["osm_objects"]
    }
    confirmed_routes = {
        "relation/20399451",
        "relation/20399452",
        "relation/20416697",
        "relation/20416719",
        "relation/20597569",
        "relation/20597697",
    }
    review_stops = {
        "node/357899927",
        "node/357900068",
        "node/13392140061",
        "node/13392140066",
        "node/13392140067",
        "way/614535232",
        "way/614535233",
        "way/614535234",
        "node/14103009456",
        "node/14103009457",
        "node/14103009458",
    }

    assert confirmed_routes | review_stops <= by_object.keys()
    for osm_object in confirmed_routes:
        assert by_object[osm_object]["classification"] == "valid_outside_source_scope"
    for osm_object in review_stops:
        assert by_object[osm_object]["classification"] == "needs_review"
    for entry in loaded["exceptions"]:
        assert entry["action"] == "preserve"
        assert entry["review_due"] is True
        assert entry["reason"] and entry["scope"] and entry["evidence_urls"]
