"""Regression cases for conservative identities and honest coverage counts."""

from copy import deepcopy

import pytest

from bus_sync.compare import compare


def source_stop(identifier="100000001", *, provider="seoul", name="시청", ref="01001", lon=127.0, lat=37.5, mode="bus"):
    return {
        "key": f"{provider}:stop:{identifier}", "id": identifier,
        "provider": provider, "name": name, "ref": ref,
        "lon": lon, "lat": lat, "mode": mode,
    }


def osm_stop(identifier=1, *, name="시청", ref="01001", lon=127.0, lat=37.5, bus_evidence="explicit", representation="platform", tags=None):
    values = {"name": name, "public_transport": representation}
    if ref is not None:
        values["ref"] = ref
    if tags:
        values.update(tags)
    return {
        "key": f"node/{identifier}", "type": "node", "id": identifier,
        "tags": values, "lon": lon, "lat": lat, "version": 1,
        "bus_evidence": bus_evidence, "in_scope": True, "stop_area_ids": [],
    }


def source_route(identifier="100100001", *, provider="seoul", name="10", operator="서울운수", stop_ids=None, mode="bus", sequence_complete=True):
    return {
        "key": f"{provider}:route:{identifier}", "id": identifier,
        "provider": provider, "name": name, "operator": operator,
        "stop_ids": stop_ids or [], "mode": mode,
        "sequence_complete": sequence_complete,
    }


def osm_route(identifier=101, *, ref="10", operator="서울운수", stop_keys=None, master=False):
    tags = {"type": "route_master" if master else "route", "ref": ref, "operator": operator}
    tags["route_master" if master else "route"] = "bus"
    return {
        "key": f"relation/{identifier}", "type": "relation", "id": identifier,
        "tags": tags, "version": 1, "members": [],
        "stop_keys": stop_keys or [], "in_scope": True,
    }


def datasets(*, public_stops=(), osm_stops=(), public_routes=(), osm_routes=()):
    sources = {
        "stops": list(public_stops), "routes": list(public_routes),
        "snapshots": {"test": "2026-10-09"}, "warnings": [],
    }
    inventory = {
        "stops": list(osm_stops), "routes": list(osm_routes), "stop_areas": [],
        "scope": {"iso3166_2": ["KR-11", "KR-41"], "reference_complete": True},
        "boundaries": {
            "type": "FeatureCollection",
            "features": [{
                "type": "Feature", "properties": {},
                "geometry": {"type": "Polygon", "coordinates": [[
                    [126, 37], [128, 37], [128, 38], [126, 38], [126, 37],
                ]]},
            }],
        },
        "snapshot_at": "2026-10-09T00:00:00Z", "warnings": [],
    }
    return sources, inventory


def run_comparison(**kwargs):
    sources, inventory = datasets(**kwargs)
    return compare(sources, inventory, {"exceptions": []})


def row(result, key, kind="stops"):
    return next(item for item in result[kind] if item["key"] == key)


def test_unique_reference_with_name_and_location_is_a_match():
    public = source_stop()
    result = run_comparison(public_stops=[public], osm_stops=[osm_stop(lat=37.5005)])
    osm = row(result, "node/1")
    assert osm["status"] == "matched"
    assert osm["candidates"][0]["basis"] == ["public_ref", "name"]
    assert row(result, public["key"])["status"] == "matched"
    assert osm["action"] == "preserve"
    assert result["read_only"] is True


def test_same_names_on_opposite_sides_remain_review_candidates():
    north = source_stop(lat=37.5002)
    south = source_stop("100000002", ref="01002", lat=37.4998)
    result = run_comparison(public_stops=[north, south], osm_stops=[osm_stop(ref=None)])
    osm = row(result, "node/1")
    assert osm["status"] == "candidate_match"
    assert len(osm["candidates"]) == 2
    assert all(candidate["confidence"] == "review" for candidate in osm["candidates"])
    assert all(row(result, stop["key"])["status"] == "candidate_match" for stop in [north, south])


def test_duplicate_reference_within_provider_is_not_a_definitive_match():
    stops = [source_stop(), source_stop("100000002", lat=37.5001)]
    result = run_comparison(public_stops=stops, osm_stops=[osm_stop()])
    osm = row(result, "node/1")
    assert osm["status"] == "candidate_match"
    assert {c["source_key"] for c in osm["candidates"]} == {s["key"] for s in stops}
    assert all(c["confidence"] == "review" for c in osm["candidates"])


def test_provider_reference_cannot_identify_other_provider_record():
    seoul = source_stop()
    gbis = source_stop(provider="gbis", ref="09009")
    osm = osm_stop(ref=None, tags={"ref:seoul": seoul["id"]})
    result = run_comparison(public_stops=[seoul, gbis], osm_stops=[osm])
    assert row(result, gbis["key"])["status"] != "matched"
    assert not any(
        c["provider"] == "gbis" and c["confidence"] == "strong"
        for c in row(result, "node/1")["candidates"]
    )


def test_unrelated_namespaced_reference_is_not_a_stop_identity():
    public = source_stop()
    osm = osm_stop(ref=None, tags={"ref:unrelated_asset": public["ref"]})
    result = run_comparison(public_stops=[public], osm_stops=[osm])
    assert row(result, "node/1")["status"] == "candidate_match"
    assert row(result, public["key"])["status"] == "candidate_match"
    assert all(c["confidence"] == "review" for c in row(result, "node/1")["candidates"])


@pytest.mark.parametrize("tags", [{"local_ref": "01001"}, {"ref": "00000"}])
def test_bay_number_and_zero_reference_are_not_stop_identity(tags):
    result = run_comparison(public_stops=[source_stop()], osm_stops=[osm_stop(ref=None, tags=tags)])
    assert row(result, "node/1")["status"] == "candidate_match"
    assert row(result, "node/1")["candidates"][0]["basis"] == ["name"]


def test_remote_reference_collision_does_not_match():
    public = source_stop()
    result = run_comparison(public_stops=[public], osm_stops=[osm_stop(lat=37.51)])
    assert row(result, "node/1")["status"] == "osm_unmatched"
    assert row(result, public["key"])["status"] == "source_unmatched"


def test_unclassified_platform_does_not_claim_a_public_bus_stop():
    public = source_stop()
    result = run_comparison(public_stops=[public], osm_stops=[osm_stop(bus_evidence="unclassified")])
    assert row(result, "node/1")["status"] == "needs_classification"
    assert row(result, public["key"])["status"] == "source_unmatched"
    assert row(result, public["key"])["candidates"] == []


def test_unknown_geometry_is_preserved_without_inventing_a_match():
    public = source_stop()
    result = run_comparison(public_stops=[public], osm_stops=[osm_stop(lon=None, lat=None)])
    osm = row(result, "node/1")
    assert osm["candidates"] == []
    assert osm["lon"] is None and osm["lat"] is None
    assert osm["action"] == "preserve"
    assert row(result, public["key"])["status"] == "source_unmatched"


def test_platform_and_stop_position_are_two_osm_objects_not_two_physical_stops():
    public = source_stop()
    platform = osm_stop()
    position = osm_stop(2, representation="stop_position", lat=37.50005)
    platform["stop_area_ids"] = position["stop_area_ids"] = ["relation/999"]
    result = run_comparison(public_stops=[public], osm_stops=[platform, position])
    assert result["counts"]["stops"]["osm:matched"] == 2
    assert result["counts"]["stops"]["source:matched"] == 1
    assert len(row(result, public["key"])["candidates"]) == 2
    assert row(result, "node/1")["representation"] == "platform"
    assert row(result, "node/2")["representation"] == "stop_position"


def test_ferry_stop_is_not_matched_to_bus_platform():
    public = source_stop(mode="ferry")
    result = run_comparison(public_stops=[public], osm_stops=[osm_stop()])
    assert row(result, public["key"])["status"] == "out_of_mode_scope"
    assert row(result, "node/1")["candidates"] == []


def test_unknown_public_stop_mode_requires_classification():
    public = source_stop(mode="unknown")
    result = run_comparison(public_stops=[public], osm_stops=[osm_stop()])
    assert row(result, public["key"])["status"] == "needs_classification"
    assert row(result, "node/1")["candidates"] == []


def test_expired_exception_remains_preserved_and_reports_missing_objects():
    sources, inventory = datasets(osm_stops=[osm_stop()], osm_routes=[osm_route()])
    exception = {
        "id": "expired", "osm_objects": ["node/1", "node/2", "relation/101"],
        "classification": "needs_review", "action": "preserve", "review_due": True,
    }
    result = compare(sources, inventory, {"exceptions": [exception]})
    assert row(result, "node/1")["status"] == "preserved_exception"
    assert row(result, "relation/101", "routes")["status"] == "preserved_exception"
    assert row(result, "node/1")["action"] == "preserve"
    assert result["exceptions"] == [{
        "id": "expired", "review_due": True, "action": "preserve",
        "observed": ["node/1", "relation/101"], "not_observed": ["node/2"],
    }]


def test_same_route_number_different_companies_selects_matching_operator():
    expected = source_route()
    other = source_route("100100002", operator="다른운수")
    result = run_comparison(public_routes=[expected, other], osm_routes=[osm_route()])
    assert row(result, "relation/101", "routes")["status"] == "matched_metadata"
    assert row(result, expected["key"], "routes")["status"] == "matched_metadata"
    assert row(result, other["key"], "routes")["status"] == "candidate_match"
    assert len(row(result, "relation/101", "routes")["candidates"]) == 2


def test_duplicate_route_number_and_operator_remain_ambiguous():
    public = [source_route(), source_route("100100002")]
    result = run_comparison(public_routes=public, osm_routes=[osm_route()])
    assert row(result, "relation/101", "routes")["status"] == "candidate_match"
    assert all(c["confidence"] == "review" for c in row(result, "relation/101", "routes")["candidates"])
    assert all(row(result, r["key"], "routes")["status"] == "candidate_match" for r in public)


def test_route_number_alone_is_only_a_candidate():
    public = source_route()
    result = run_comparison(public_routes=[public], osm_routes=[osm_route(operator="")])
    assert row(result, "relation/101", "routes")["status"] == "candidate_match"
    assert row(result, public["key"], "routes")["status"] == "candidate_match"


@pytest.mark.parametrize(("public_number", "osm_number"), [("10-1", "101"), ("1-1", "11")])
def test_route_number_hyphen_distinguishes_different_services(public_number, osm_number):
    public = source_route(name=public_number)
    result = run_comparison(public_routes=[public], osm_routes=[osm_route(ref=osm_number)])
    assert row(result, "relation/101", "routes")["status"] == "osm_unmatched"
    assert row(result, public["key"], "routes")["status"] == "source_unmatched"


def test_route_master_alone_does_not_claim_a_mapped_route_direction():
    public = source_route()
    result = run_comparison(public_routes=[public], osm_routes=[osm_route(master=True)])
    assert row(result, "relation/101", "routes")["kind"] == "route_master"
    assert row(result, public["key"], "routes")["status"] == "candidate_match"
    assert row(result, public["key"], "routes")["candidates"][0]["osm_kind"] == "route_master"
    assert result["counts"]["routes"].get("source:route:matched_metadata", 0) == 0


def test_unknown_public_route_mode_cannot_become_a_confirmed_bus_match():
    public = source_route(mode="unknown")
    result = run_comparison(public_routes=[public], osm_routes=[osm_route()])
    assert row(result, public["key"], "routes")["status"] == "needs_classification"
    assert row(result, "relation/101", "routes")["status"] != "matched_metadata"
    assert all(c["confidence"] != "metadata" for c in row(result, "relation/101", "routes")["candidates"])


def test_ferry_route_is_excluded_even_with_same_number_and_operator():
    public = source_route(mode="ferry")
    result = run_comparison(public_routes=[public], osm_routes=[osm_route()])
    assert row(result, public["key"], "routes")["status"] == "out_of_mode_scope"
    assert row(result, "relation/101", "routes")["candidates"] == []


def sequence_case(*, reverse=False, sequence_complete=True, paired=False):
    public_stops = [
        source_stop(str(100000001 + i), name=f"정류소{i}", ref=f"{1001 + i:05}", lat=37.5 + i * 0.002)
        for i in range(3)
    ]
    osm_stops = [
        osm_stop(i + 1, name=p["name"], ref=p["ref"], lat=p["lat"])
        for i, p in enumerate(public_stops)
    ]
    keys = [s["key"] for s in osm_stops]
    if paired:
        for i, p in enumerate(public_stops):
            osm_stops.append(osm_stop(i + 11, name=p["name"], ref=p["ref"], lat=p["lat"], representation="stop_position"))
        keys = [key for pair in zip(keys, [s["key"] for s in osm_stops[3:]]) for key in pair]
    if reverse:
        keys.reverse()
    public = source_route(operator="", stop_ids=[p["id"] for p in public_stops], sequence_complete=sequence_complete)
    result = run_comparison(
        public_stops=public_stops, osm_stops=osm_stops,
        public_routes=[public], osm_routes=[osm_route(operator="", stop_keys=keys)],
    )
    return public, result


def test_ordered_stop_overlap_is_metadata_evidence_never_geometry_verification():
    public, result = sequence_case(sequence_complete=False)
    osm = row(result, "relation/101", "routes")
    candidate = osm["candidates"][0]
    assert osm["status"] == "matched_metadata"
    assert candidate["ordered_overlap_count"] == 3
    assert "stop_sequence" in candidate["basis"]
    assert candidate["source_sequence_complete"] is False
    assert candidate["geometry_verified"] is False
    assert osm["geometry_verified"] is False
    assert row(result, public["key"], "routes")["geometry_verified"] is False


def test_same_stops_in_reverse_order_do_not_verify_the_direction():
    _, result = sequence_case(reverse=True)
    osm = row(result, "relation/101", "routes")
    assert osm["status"] == "candidate_match"
    assert osm["candidates"][0]["shared_stop_count"] == 3
    assert osm["candidates"][0]["ordered_overlap_count"] < 3


def test_platform_stop_position_pairs_do_not_double_route_stop_overlap():
    _, result = sequence_case(paired=True)
    candidate = row(result, "relation/101", "routes")["candidates"][0]
    assert candidate["mapped_osm_stop_count"] == 3
    assert candidate["shared_stop_count"] == 3
    assert candidate["ordered_overlap_count"] == 3
    assert result["counts"]["stops"]["osm:matched"] == 6


def test_outside_region_support_stops_are_retained_but_not_counted_as_local_stops():
    local = osm_stop()
    outside = osm_stop(2, lon=128.1)
    outside["in_scope"] = False
    result = run_comparison(osm_stops=[local, outside])
    assert row(result, "node/2")["in_scope"] is False
    assert row(result, "node/2")["action"] == "preserve"
    assert result["counts"]["stops"]["osm:osm_unmatched"] == 1
    assert result["cross_boundary_support_stop_objects"] == 1


@pytest.mark.parametrize(
    ("scope_state", "expected_status"),
    [("unknown", "needs_scope_review"), ("outside", "outside_scope_support")],
)
@pytest.mark.parametrize("master", [False, True])
@pytest.mark.parametrize("has_exception", [False, True])
def test_unknown_and_outside_routes_remain_visible_without_claiming_local_match(
    scope_state, expected_status, master, has_exception
):
    public = source_route()
    osm = osm_route(master=master)
    osm.update(in_scope=False, scope_state=scope_state)
    sources, inventory = datasets(public_routes=[public], osm_routes=[osm])
    entries = [{
        "id": "retained-route", "osm_objects": [osm["key"]],
        "classification": "needs_review", "action": "preserve", "review_due": True,
    }] if has_exception else []

    result = compare(sources, inventory, {"exceptions": entries})
    retained = row(result, osm["key"], "routes")
    public_row = row(result, public["key"], "routes")

    assert retained["status"] == ("preserved_exception" if has_exception else expected_status)
    assert retained["in_scope"] is False
    assert retained["scope_state"] == scope_state
    assert retained["action"] == "preserve"
    assert retained["kind"] == ("route_master" if master else "route")
    assert retained["candidates"]
    assert public_row["candidates"]
    assert public_row["status"] != "matched_metadata"
    assert result["counts"]["routes"].get("source:route:matched_metadata", 0) == 0
    if has_exception:
        assert result["exceptions"][0]["observed"] == [osm["key"]]
        assert result["exceptions"][0]["not_observed"] == []


@pytest.mark.parametrize("has_exception", [False, True])
def test_unknown_scope_stops_have_separate_count_from_boundary_support(has_exception):
    unknown = osm_stop(2, lon=None, lat=None)
    unknown.update(in_scope=False, scope_state="unknown")
    outside = osm_stop(3, lon=128.1)
    outside.update(in_scope=False, scope_state="outside")
    sources, inventory = datasets(osm_stops=[osm_stop(), unknown, outside])
    entries = [{
        "id": "unknown-location", "osm_objects": [unknown["key"]],
        "classification": "needs_review", "action": "preserve", "review_due": False,
    }] if has_exception else []

    result = compare(sources, inventory, {"exceptions": entries})
    retained = row(result, unknown["key"])

    assert retained["status"] == ("preserved_exception" if has_exception else "needs_scope_review")
    assert retained["scope_state"] == "unknown"
    assert retained["in_scope"] is False
    assert retained["lon"] is None and retained["lat"] is None
    assert retained["action"] == "preserve"
    assert retained["candidates"] == []
    assert result["unknown_scope_stop_objects"] == 1
    assert result["cross_boundary_support_stop_objects"] == 1
    assert sum(result["counts"]["stops"].values()) == 1


@pytest.mark.parametrize("scope_state", ["unknown", "outside"])
def test_support_route_does_not_hide_an_independent_in_scope_match(scope_state):
    public = source_route()
    support = osm_route(102)
    support.update(in_scope=False, scope_state=scope_state)
    result = run_comparison(public_routes=[public], osm_routes=[osm_route(), support])

    public_row = row(result, public["key"], "routes")
    assert public_row["status"] == "matched_metadata"
    assert {candidate["osm_key"] for candidate in public_row["candidates"]} == {
        "relation/101", "relation/102",
    }
    assert row(result, "relation/102", "routes")["action"] == "preserve"


def test_comparison_is_deterministic_and_does_not_mutate_inputs():
    sources, inventory = datasets(
        public_stops=[source_stop("100000002", ref="01002"), source_stop()],
        osm_stops=[osm_stop(2, ref="01002"), osm_stop()],
        public_routes=[source_route()], osm_routes=[osm_route()],
    )
    saved_sources, saved_inventory = deepcopy(sources), deepcopy(inventory)
    result = compare(sources, inventory, {"exceptions": []})
    sources["stops"].reverse()
    inventory["stops"].reverse()
    assert result == compare(sources, inventory, {"exceptions": []})
    sources["stops"].reverse()
    inventory["stops"].reverse()
    assert sources == saved_sources
    assert inventory == saved_inventory
