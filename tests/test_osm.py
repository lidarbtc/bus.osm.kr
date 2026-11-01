import json
from pathlib import Path
import xml.etree.ElementTree as ET

import osmium
import pytest
from shapely.geometry import shape

from bus_sync.osm import read_osm

FIXTURE = Path(__file__).parent / "fixtures" / "osm_regions.osm"


def indexed(rows):
    return {row["key"]: row for row in rows}


def changed_file(tmp_path, change):
    tree = ET.parse(FIXTURE)
    change(tree.getroot())
    path = tmp_path / "modified.osm"
    tree.write(path, encoding="utf-8", xml_declaration=True)
    return path


def test_filters_by_real_boundaries_and_keeps_uncategorized_platforms():
    result = read_osm(FIXTURE)
    stops = indexed(result["stops"])
    assert result["scope"]["boundary_complete"] is True
    assert result["scope"]["reference_complete"] is True
    assert stops["node/101"]["version"] == 3
    assert stops["node/101"]["in_scope"] is True
    assert stops["node/110"]["in_scope"] is True  # Boundary points are covered.
    assert stops["node/103"]["bus_evidence"] == "unclassified"
    assert "node/104" not in stops  # Rail-only member in a mixed stop_area.
    assert "node/106" not in stops
    assert stops["way/201"]["geometry"]["type"] == "Polygon"
    assert stops["way/201"]["in_scope"] is True  # Intersects, centroid outside.
    assert shape(stops["relation/400"]["geometry"]).area == pytest.approx(0.01)
    assert len(result["boundaries"]["features"]) == 2
    assert result["snapshot_at"] is None
    json.dumps(result)


def test_cross_border_members_and_route_master_siblings_are_retained():
    result = read_osm(FIXTURE)
    stops, routes = indexed(result["stops"]), indexed(result["routes"])
    assert stops["node/102"]["in_scope"] is False
    assert stops["node/102"]["scope_state"] == "outside"
    assert routes["relation/500"]["scope_state"] == "inside"
    assert routes["relation/501"]["scope_state"] == "outside"
    assert routes["relation/501"]["geometry_complete"] is True
    assert routes["relation/500"]["stop_keys"] == ["node/101", "node/107", "node/102", "node/101"]
    assert routes["relation/500"]["members"][-1] == {"type": "way", "ref": 203, "role": ""}
    assert routes["relation/502"]["tags"]["route_master"] == "bus"
    assert routes["relation/501"]["in_scope"] is False
    assert stops["node/109"]["in_scope"] is False
    assert "relation/503" not in routes
    assert routes["relation/504"]["in_scope"] is True  # Road crosses with no stop in region.


def test_bus_evidence_flows_from_routes_and_stop_areas():
    result = read_osm(FIXTURE)
    stops = indexed(result["stops"])
    assert stops["node/107"]["bus_evidence"] == "relation"
    assert stops["node/105"]["bus_evidence"] == "relation"
    assert stops["node/108"]["bus_evidence"] == "relation"
    assert stops["node/101"]["bus_evidence"] == "explicit"
    assert stops["node/105"]["stop_area_ids"] == ["relation/450"]
    assert indexed(result["stop_areas"])["relation/450"]["bus_evidence"] == "relation"


@pytest.mark.parametrize("target", ["relation[@id='301']", "way[@id='11']", "node[@id='1']"])
def test_missing_boundary_or_ring_references_fail_closed(tmp_path, target):
    def remove(root):
        root.remove(root.find(target))
    with pytest.raises(ValueError, match="boundary"):
        read_osm(changed_file(tmp_path, remove))


def test_unclosed_boundary_fails_closed(tmp_path):
    def unclose(root):
        root.find("way[@id='11']").remove(root.find("way[@id='11']/nd[@ref='1']"))
    with pytest.raises(ValueError, match="boundary"):
        read_osm(changed_file(tmp_path, unclose))


def test_missing_route_member_is_preserved_and_makes_snapshot_incomplete(tmp_path):
    def missing(root):
        root.find("relation[@id='500']").insert(0, ET.Element("member", type="node", ref="999", role="platform"))
    result = read_osm(changed_file(tmp_path, missing))
    assert result["scope"]["reference_complete"] is False
    route = indexed(result["routes"])["relation/500"]
    assert route["stop_keys"][0] == "node/999"
    assert route["members"][0]["ref"] == 999
    assert any("node/999" in w for w in result["warnings"])


def test_incomplete_platform_has_no_invented_coordinates(tmp_path):
    def incomplete(root):
        way = ET.Element("way", id="205", version="1")
        ET.SubElement(way, "nd", ref="101")
        ET.SubElement(way, "nd", ref="999")
        ET.SubElement(way, "tag", k="public_transport", v="platform")
        root.insert(list(root).index(root.find("relation")), way)
    result = read_osm(changed_file(tmp_path, incomplete))
    row = indexed(result["stops"])["way/205"]
    assert row["geometry"] is row["lon"] is row["lat"] is None
    assert row["in_scope"] is False
    assert row["scope_state"] == "unknown"
    assert row["geometry_complete"] is False
    assert result["scope"]["reference_complete"] is False
    assert result["scope"]["unknown_geometry_count"] == 1


def test_boundary_hole_excludes_stop(tmp_path):
    def hole(root):
        seoul = root.find("relation[@id='300']")
        seoul.insert(0, ET.Element("member", type="way", ref="202", role="inner"))
        node = ET.Element("node", id="99", version="1", lat="0.35", lon="0.35")
        ET.SubElement(node, "tag", k="highway", v="bus_stop")
        root.insert(6, node)
    result = read_osm(changed_file(tmp_path, hole))
    assert "node/99" not in indexed(result["stops"])
    seoul = shape(result["boundaries"]["features"][0]["geometry"])
    assert seoul.area == pytest.approx(3.99)


def test_pbf_and_xml_have_identical_inventory_and_read_snapshot_header(tmp_path):
    path = tmp_path / "snapshot.osm.pbf"
    header = osmium.io.Header()
    header.set("osmosis_replication_timestamp", "2026-10-09T20:00:00Z")
    with osmium.SimpleWriter(path, header=header) as writer:
        for obj in osmium.FileProcessor(FIXTURE):
            writer.add(obj)
    result = read_osm(path)
    assert result["snapshot_at"] == "2026-10-09T20:00:00Z"
    expected = read_osm(FIXTURE)
    for key in ("stops", "routes", "stop_areas", "boundaries", "scope"):
        assert result[key] == expected[key]


def test_overlapping_boundary_rings_are_not_silently_repaired(tmp_path):
    def overlap(root):
        coords = [(0.5, -1), (1.5, -1), (1.5, 1), (0.5, 1)]
        for i, (lon, lat) in enumerate(coords, 70):
            root.insert(6, ET.Element("node", id=str(i), version="1", lon=str(lon), lat=str(lat)))
        way = ET.Element("way", id="15", version="1")
        for ref in [70, 71, 72, 73, 70]:
            ET.SubElement(way, "nd", ref=str(ref))
        root.insert(list(root).index(root.find("relation")), way)
        root.find("relation[@id='300']").insert(0, ET.Element("member", type="way", ref="15", role="outer"))
    with pytest.raises(ValueError, match="overlapping"):
        read_osm(changed_file(tmp_path, overlap))


def test_stop_area_infers_relation_platform_and_reports_invalid_geometry(tmp_path):
    def relation_platform(root):
        platform = root.find("relation[@id='400']")
        for tag in list(platform.findall("tag")):
            if tag.attrib["k"] != "type":
                platform.remove(tag)
        root.find("relation[@id='450']").insert(0, ET.Element("member", type="relation", ref="400", role="platform"))
    result = read_osm(changed_file(tmp_path, relation_platform))
    assert indexed(result["stops"])["relation/400"]["bus_evidence"] == "relation"
    assert result["scope"]["reference_complete"] is True

    def invalid(root):
        relation_platform(root)
        way = root.find("way[@id='202']")
        way.remove(list(way.findall("nd"))[-1])
    result = read_osm(changed_file(tmp_path, invalid))
    assert indexed(result["stops"])["relation/400"]["geometry"] is None
    assert result["scope"]["reference_complete"] is False
    assert any("relation/400" in warning for warning in result["warnings"])


def test_polygon_preserves_outer_island_inside_inner_hole():
    from shapely.geometry import LineString
    from bus_sync.osm import _polygon
    outer = LineString([(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)])
    hole = LineString([(2, 2), (8, 2), (8, 8), (2, 8), (2, 2)])
    island = LineString([(3, 3), (4, 3), (4, 4), (3, 4), (3, 3)])
    result = _polygon({"outer": [outer, island], "inner": [hole]}, "test")
    assert result.area == 65
    with pytest.raises(ValueError, match="same role"):
        _polygon({"outer": [outer, island], "inner": []}, "test")


def test_unknown_route_scope_is_not_silently_dropped(tmp_path):
    def missing(root):
        route = ET.SubElement(root, "relation", id="505", version="1")
        ET.SubElement(route, "member", type="way", ref="999", role="")
        ET.SubElement(route, "tag", k="type", v="route")
        ET.SubElement(route, "tag", k="route", v="bus")
    result = read_osm(changed_file(tmp_path, missing))
    route = indexed(result["routes"])["relation/505"]
    assert route["scope_state"] == "unknown"
    assert route["geometry_complete"] is False
    assert result["scope"]["reference_complete"] is False
    assert result["scope"]["unknown_route_geometry_count"] == 1
    assert any("way/999" in warning for warning in result["warnings"])


def test_cycle_through_network_relation_is_reported_without_recursing_forever(tmp_path):
    def cycle(root):
        root.find("relation[@id='503']").insert(0, ET.Element("member", type="relation", ref="601", role=""))
        network = ET.SubElement(root, "relation", id="601", version="1")
        ET.SubElement(network, "member", type="relation", ref="503", role="")
        ET.SubElement(network, "tag", k="type", v="network")
    result = read_osm(changed_file(tmp_path, cycle))
    route = indexed(result["routes"])["relation/503"]
    assert route["scope_state"] == "unknown"
    assert route["geometry_complete"] is False
    assert result["scope"]["reference_complete"] is False
    assert any("cyclic" in warning for warning in result["warnings"])


def test_districts_group_the_regions_and_incomplete_ones_are_reported():
    result = read_osm(FIXTURE)
    districts = {f["properties"]["name"]: f["properties"] for f in result["districts"]["features"]}
    assert districts == {
        "종로구": {"osm_key": "relation/310", "version": 1, "name": "종로구", "parent": "KR-11", "code": "1111000000"},
        "수원시": {"osm_key": "relation/311", "version": 1, "name": "수원시", "parent": "KR-41", "code": "4111000000"},
    }
    # An open ring is reported instead of failing the whole inventory; other provinces are ignored.
    assert any("relation/312" in w and "incomplete" in w for w in result["warnings"])
    assert not any("relation/313" in w for w in result["warnings"])
