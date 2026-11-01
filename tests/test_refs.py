import math

import pytest

from bus_sync.refs import numbers_in, plan_refs, similar_names

LAT0, LON0 = 37.5, 127.0


def at(x, y):
    """Coordinates x metres east and y metres north of a fixed origin."""
    return {"lon": LON0 + x / (111320 * math.cos(math.radians(LAT0))), "lat": LAT0 + y / 110540}


def box(x0, y0, x1, y1):
    corners = [at(x0, y0), at(x1, y0), at(x1, y1), at(x0, y1), at(x0, y0)]
    return {"type": "Polygon", "coordinates": [[[c["lon"], c["lat"]] for c in corners]]}


def inventory(*stops, region="KR-11"):
    other = "KR-41" if region == "KR-11" else "KR-11"
    return {"boundaries": {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"iso3166_2": region}, "geometry": box(-30000, -30000, 30000, 30000)},
        {"type": "Feature", "properties": {"iso3166_2": other}, "geometry": box(40000, -30000, 90000, 30000)},
    ]}, "stops": list(stops)}


def osm(key, x, y, name, ref=None, **tags):
    kind, ident = key.split("/")
    tags = {"highway": "bus_stop", "name": name, **({"ref": ref} if ref else {}), **tags}
    return {"key": key, "type": kind, "id": int(ident), "version": 3, "tags": tags, **at(x, y),
            "geometry": {"type": "Point", "coordinates": [at(x, y)["lon"], at(x, y)["lat"]]},
            "in_scope": True, "scope_state": "inside", "bus_evidence": "explicit"}


def official(provider, ident, x, y, name, ref):
    return {"key": f"{provider}:stop:{ident}", "provider": provider, "id": ident, "ref": ref, "name": name,
            "mode": "bus", "origin": "stops", **at(x, y)}


def route(provider, ident, *stop_ids):
    return {"key": f"{provider}:route:{ident}", "provider": provider, "id": ident, "name": ident,
            "stop_ids": list(stop_ids), "mode": "bus"}


def sources(stops, routes=()):
    return {"stops": stops, "routes": list(routes)}


def road(provider="seoul"):
    """An east-west road with an eastbound stop south of it and a westbound stop north of it."""
    stops = [official(provider, "w1", -400, -8, "서쪽", "10001"), official(provider, "e1", 400, -8, "동쪽", "10002"),
             official(provider, "e2", 400, 8, "동쪽", "10003"), official(provider, "w2", -400, 8, "서쪽", "10004")]
    routes = [route(provider, "east", "w1", "east-stop", "e1"), route(provider, "west", "e2", "west-stop", "w2")]
    return stops, routes


def refs(plan):
    return {c["osm_key"]: c["ref"] for c in plan["changes"]}


def kinds(plan):
    return sorted(t["kind"] for t in plan["tasks"])


@pytest.mark.parametrize("value, expected", [
    ("08-047", {"08047"}), ("22334;22434", {"22334", "22434"}), ("13044, 22062", {"13044", "22062"}),
    ("35903 천현정미소 방면", {"35903"}), ("329923", set()), ("", set()),
])
def test_existing_numbers_are_read_in_every_notation(value, expected):
    assert numbers_in({"ref": value}) == expected
    assert numbers_in({"ref:KR:gbis": "228000123"}) == set()


def test_similar_names_never_mix_numbers_or_opposite_sides():
    assert similar_names("서대문우체국앞", "서대문우체국")
    assert not similar_names("신원1단지", "신원2단지")
    assert not similar_names("상록수역3번출구", "상록수역3번출구건너편")


def test_a_lone_stop_gets_the_number_of_the_official_stop_beside_it():
    plan = plan_refs(sources([official("seoul", "a", 0, 0, "시장입구", "12345")]),
                     inventory(osm("node/1", 5, 0, "시장입구"), osm("node/2", 3000, 0, "시장입구", ref="12345")))
    assert refs(plan) == {}  # The number already exists on a stop 3 km away.
    # A same-name neighbour carries a number that official data does not know: leave both alone.
    plan = plan_refs(sources([official("seoul", "a", 0, 0, "시장입구", "12345")]),
                     inventory(osm("node/1", 5, 0, "시장입구"), osm("node/9", 40, 0, "시장입구", ref="99999")))
    assert refs(plan) == {} and "neighbour_number_not_in_official_data" in kinds(plan)
    plan = plan_refs(sources([official("seoul", "a", 0, 0, "시장입구", "12345")]), inventory(osm("node/1", 5, 0, "시장입구")))
    assert refs(plan) == {"node/1": "12345"}
    change = plan["changes"][0]
    assert change["version"] == 3 and change["method"] == "same_name" and change["official"][0]["ref"] == "12345"


def test_both_sides_of_a_road_are_told_apart_by_travel_direction():
    stops, routes = road()
    stops += [official("seoul", "east-stop", 0, -8, "중앙시장", "20001"), official("seoul", "west-stop", 6, 8, "중앙시장", "20002")]
    plan = plan_refs(sources(stops, routes), inventory(osm("node/1", -3, -12, "중앙시장"), osm("node/2", 4, 11, "중앙시장")))
    assert refs(plan) == {"node/1": "20001", "node/2": "20002"}
    assert {c["side_of_road"] for c in plan["changes"]} == {"confirmed"}


def test_official_coordinates_on_the_wrong_side_stop_the_edit():
    stops, routes = road()
    # The eastbound stop is recorded north of the road, where westbound buses stop.
    stops += [official("seoul", "east-stop", 0, 9, "중앙시장", "20001"), official("seoul", "west-stop", 0, -9, "중앙시장", "20002")]
    plan = plan_refs(sources(stops, routes), inventory(osm("node/1", 0, -10, "중앙시장"), osm("node/2", 0, 10, "중앙시장")))
    assert refs(plan) == {}
    assert kinds(plan) == ["side_of_road_disagrees", "side_of_road_disagrees"]


def test_same_name_stops_without_direction_need_a_clear_lead():
    two = [official("seoul", "a", 0, 0, "마을회관", "30001"), official("seoul", "b", 20, 0, "마을회관", "30002")]
    close_call = plan_refs(sources(two), inventory(osm("node/1", 6, 0, "마을회관")))
    assert refs(close_call) == {} and kinds(close_call) == ["unclear_pairing"]
    far_apart = [official("seoul", "a", 0, 0, "마을회관", "30001"), official("seoul", "b", 45, 0, "마을회관", "30002")]
    assert refs(plan_refs(sources(far_apart), inventory(osm("node/1", 1, 0, "마을회관")))) == {"node/1": "30001"}


def test_a_stop_mapped_twice_is_numbered_once_and_listed_for_merging():
    area = osm("way/5", 1, 1, "학교앞", public_transport="platform")
    area["tags"].pop("highway")
    area["geometry"] = box(-3, -3, 5, 5)
    plan = plan_refs(sources([official("seoul", "a", 0, 0, "학교앞", "40001")]), inventory(osm("node/7", 0, 0, "학교앞"), area))
    assert refs(plan) == {"node/7": "40001"}
    assert kinds(plan) == ["mapped_twice"]
    assert plan["tasks"][0]["osm_keys"] == ["node/7", "way/5"]


def test_a_number_reused_in_another_town_is_not_a_conflict():
    stops = [official("gbis", "ansan", 0, 0, "안산역", "18115"), official("seoul", "geumcheon", 18000, 0, "금천패션아울렛사거리", "18115")]
    plan = plan_refs(sources(stops), inventory(osm("node/1", 3, 0, "안산역"), osm("node/2", 18003, 0, "금천패션아울렛사거리")))
    assert refs(plan) == {"node/1": "18115", "node/2": "18115"}


def test_a_number_already_mapped_nearby_in_another_notation_is_not_added_again():
    plan = plan_refs(sources([official("seoul", "a", 0, 0, "의정부시의회", "08042")]),
                     inventory(osm("node/1", 2, 0, "의정부시의회"), osm("node/2", 400, 0, "다른정류장", ref="08-042")))
    assert refs(plan) == {} and kinds(plan) == ["number_already_on_nearby_stop"]


@pytest.mark.parametrize("region, expected", [("KR-41", "19480"), ("KR-11", "35316")])
def test_a_stop_with_two_official_numbers_gets_the_local_one(region, expected):
    stops, routes = road("seoul")
    gbis_stops, gbis_routes = road("gbis")
    stops += gbis_stops + [official("seoul", "east-stop", 0, -8, "자이더포레", "35316"),
                           official("gbis", "east-stop", 2, -9, "자이더포레", "19480")]
    plan = plan_refs(sources(stops, routes + gbis_routes), inventory(osm("node/1", 1, -10, "자이더포레"), region=region))
    assert refs(plan) == {"node/1": expected}


def test_one_number_under_two_names_is_one_stop():
    stops = [official("seoul", "a", 0, 0, "마곡역(중)", "16018"), official("gbis", "a", 4, 0, "마곡역", "16018")]
    plan = plan_refs(sources(stops), inventory(osm("node/1", 6, 0, "마곡역")))
    assert refs(plan) == {"node/1": "16018"} and kinds(plan) == []


def test_a_differently_spelt_name_is_used_only_when_no_other_stop_could_be_meant():
    one = [official("seoul", "a", 0, 0, "서대문우체국", "13010")]
    plan = plan_refs(sources(one), inventory(osm("node/1", 2, 0, "서대문우체국앞")))
    assert refs(plan) == {"node/1": "13010"} and plan["changes"][0]["method"] == "similar_name"
    two = one + [official("seoul", "b", 40, 0, "서대문우체국", "13011")]
    plan = plan_refs(sources(two), inventory(osm("node/1", 2, 0, "서대문우체국앞")))
    assert refs(plan) == {} and kinds(plan) == ["similar_name_unclear"]
    across = [official("seoul", "a", 0, 0, "상록수역3번출구건너편", "17224")]
    assert refs(plan_refs(sources(across), inventory(osm("node/1", 2, 0, "상록수역3번출구")))) == {}


def test_when_the_nearest_number_is_already_mapped_the_next_one_is_not_used():
    stops = [official("seoul", "a", 0, 0, "남산서울타워", "03320"), official("seoul", "b", 45, 0, "남산서울타워", "03904")]
    plan = plan_refs(sources(stops), inventory(osm("node/1", 2, 0, "남산서울타워"), osm("node/2", -25, 0, "남산서울타워", ref="서울03320")))
    assert refs(plan) == {} and kinds(plan) == ["same_stop_as_numbered_neighbour"]


def test_one_number_is_never_given_to_two_stops():
    stops = [official("seoul", "a", 0, 0, "가나다", "55555"), official("seoul", "b", 500, 0, "라마바", "55555")]
    plan = plan_refs(sources(stops), inventory(osm("node/1", 2, 0, "가나다"), osm("node/2", 502, 0, "라마바")))
    assert refs(plan) == {} and kinds(plan) == ["number_would_be_used_twice", "number_would_be_used_twice"]


def test_only_platforms_without_a_number_inside_the_region_are_changed():
    stop_position = osm("node/3", 0, 1, "시장입구", public_transport="stop_position")
    stop_position["tags"].pop("highway")
    outside = osm("node/4", 1, 0, "시장입구")
    outside["in_scope"] = False
    plan = plan_refs(sources([official("seoul", "a", 0, 0, "시장입구", "12345")]), inventory(stop_position, outside))
    assert refs(plan) == {}


def test_the_osmchange_adds_ref_and_keeps_every_other_tag():
    import xml.etree.ElementTree as ET
    from bus_sync.refs import osm_change
    stop = osm("node/1", 5, 0, "시장입구", shelter="yes")
    plan = plan_refs(sources([official("seoul", "a", 0, 0, "시장입구", "12345")]), inventory(stop))
    node = ET.fromstring(osm_change(plan["changes"])).find("modify/node")
    assert node.attrib["id"] == "1" and node.attrib["version"] == "3"
    assert float(node.attrib["lat"]) == pytest.approx(stop["lat"]) and float(node.attrib["lon"]) == pytest.approx(stop["lon"])
    assert {t.attrib["k"]: t.attrib["v"] for t in node.findall("tag")} == {
        "highway": "bus_stop", "name": "시장입구", "shelter": "yes", "ref": "12345"}


def test_every_task_kind_tells_mappers_what_to_do():
    import inspect
    import re as regex
    from bus_sync import refs
    kinds_in_code = set(regex.findall(r'task\("([a-z_]+)"', inspect.getsource(refs)))
    assert kinds_in_code and kinds_in_code <= set(refs.TASK_INSTRUCTIONS)
    two = [official("seoul", "a", 0, 0, "마을회관", "30001"), official("seoul", "b", 20, 0, "마을회관", "30002")]
    feature = refs.tasks_geojson(plan_refs(sources(two), inventory(osm("node/1", 6, 0, "마을회관")))["tasks"])["features"][0]
    assert feature["properties"]["kind"] == "unclear_pairing" and feature["properties"]["osm"] == "node/1"
    assert "표지판" in feature["properties"]["instruction"]


def test_export_osc_writes_only_the_chosen_districts(tmp_path):
    import json
    import xml.etree.ElementTree as ET
    from bus_sync import cli
    def change(i, district):
        return {"id": i, "version": 1, "lat": 37.5, "lon": 127.0, "tags": {"highway": "bus_stop"}, "ref": f"0000{i}",
                "district": district}
    plan_file = tmp_path / "ref-plan.json"
    plan_file.write_text(json.dumps({"summary": {"by_district": {"종로구": 1, "수원시": 1}},
                                     "changes": [change(1, "종로구"), change(2, "수원시")]}), encoding="utf-8")
    out = tmp_path / "pilot.osc"
    assert cli.main(["export-osc", "--plan", str(plan_file), "--district", "수원시", "--output", str(out)]) == 0
    assert [n.attrib["id"] for n in ET.parse(out).getroot().iter("node")] == ["2"]
    assert cli.main(["export-osc", "--plan", str(plan_file), "--district", "없는구", "--output", str(out)]) == 2


def test_self_check_hides_mapped_numbers_and_compares_the_plan_with_them():
    from bus_sync.refs import self_check
    stops = [official("seoul", "a", 0, 0, "시장입구", "12345"), official("seoul", "b", 900, 0, "우체국", "22222")]
    mapped = inventory(osm("node/1", 5, 0, "시장입구", ref="12345"), osm("node/2", 903, 0, "우체국", ref="33333"))
    check = self_check(sources(stops), mapped, fraction=1.0)
    assert {k: check[k] for k in ("hidden", "assigned", "agree", "disagree", "mapped_number_not_official_here")} == {
        "hidden": 2, "assigned": 2, "agree": 1, "disagree": 0, "mapped_number_not_official_here": 1}
    assert check["agreement"] == 1.0
