"""Read a reference-checked OSM bus inventory without changing OSM objects.

The input must contain both administrative boundaries and their complete rings.
Boundary completeness does not prove that an arbitrary input is a full regional
extract: the downloader must establish the provenance of the source file.
"""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any

import osmium
from shapely.geometry import GeometryCollection, LineString, Point, Polygon, mapping
from shapely.ops import polygonize_full, unary_union
from shapely.prepared import prep

REGIONS = ("KR-11", "KR-41")
_TYPES = {"n": "node", "w": "way", "r": "relation"}
_RAIL = ("train", "subway", "tram", "light_rail", "monorail", "ferry")


def _explicit(tags: dict[str, str]) -> bool:
    return tags.get("highway") == "bus_stop" or (
        tags.get("bus") == "yes"
        and tags.get("public_transport") in {"platform", "stop_position", "stop_area"}
    )


def _non_bus(tags: dict[str, str]) -> bool:
    return tags.get("railway") in {"platform", "stop", "station", "tram_stop"} or any(
        tags.get(k) == "yes" for k in _RAIL
    )


def _candidate(tags: dict[str, str]) -> bool:
    return _explicit(tags) or (
        tags.get("public_transport") in {"platform", "stop_position"}
        and not _non_bus(tags)
    )


def _stop_role(role: str) -> bool:
    return role in {"stop", "stop_entry_only", "stop_exit_only", "platform",
                    "platform_entry_only", "platform_exit_only"}


def _base(obj: Any) -> dict:
    kind = _TYPES[obj.type_str()]
    return {"key": f"{kind}/{obj.id}", "type": kind, "id": obj.id,
            "version": obj.version, "tags": dict(obj.tags)}


def _members(obj: Any) -> list[dict]:
    return [{"type": _TYPES[m.type], "ref": m.ref, "role": m.role} for m in obj.members]


def _key(member: dict) -> str:
    return f"{member['type']}/{member['ref']}"


def _polygon(rings: dict[str, list], label: str):
    """Assemble complete, non-crossing rings, including islands inside holes."""
    closed: list[tuple[str, Any]] = []
    for role, lines in rings.items():
        if not lines:
            continue
        polygons, cuts, dangles, invalid = polygonize_full(lines)
        if any(not g.is_empty for g in (cuts, dangles, invalid)):
            raise ValueError(f"{label}: incomplete or invalid {role} boundary rings")
        # polygonize partitions nested line rings into faces. The exterior of
        # each face recovers each original ring, before applying OSM roles.
        for face in polygons.geoms:
            ring = Polygon(face.exterior)
            if ring.is_empty or not ring.is_valid:
                raise ValueError(f"{label}: invalid {role} boundary geometry")
            closed.append((role, ring))
    if not any(role == "outer" for role, _ in closed):
        raise ValueError(f"{label}: no complete outer boundary ring")
    parents: dict[int, int | None] = {}
    for i, (role, ring) in enumerate(closed):
        containers = []
        for j, (_, other) in enumerate(closed):
            if i == j:
                continue
            if ring.equals(other) or ring.overlaps(other):
                raise ValueError(f"{label}: overlapping boundary rings")
            if other.covers(ring):
                containers.append(j)
        parent = min(containers, key=lambda j: closed[j][1].area) if containers else None
        if parent is None and role != "outer":
            raise ValueError(f"{label}: inner boundary lies outside outer boundary")
        if parent is not None and closed[parent][0] == role:
            raise ValueError(f"{label}: nested boundary rings have the same role")
        parents[i] = parent
    pieces = []
    for i, (role, ring) in enumerate(closed):
        if role == "outer":
            holes = [child for j, (_, child) in enumerate(closed) if parents[j] == i]
            pieces.append(ring.difference(unary_union(holes)) if holes else ring)
    result = unary_union(pieces)
    if result.is_empty or not result.is_valid or result.geom_type not in {"Polygon", "MultiPolygon"}:
        raise ValueError(f"{label}: invalid polygon geometry")
    return result


def read_osm(path: Path) -> dict:
    """Return a JSON-serializable Seoul/Gyeonggi bus inventory from XML or PBF.

    Missing/invalid administrative rings raise ValueError. Other missing
    references are retained as warnings and make ``scope.reference_complete``
    false. A null geometry is never assigned an invented coordinate.
    """
    path = Path(path)
    warnings: set[str] = set()
    relations: dict[str, dict] = {}
    pool = osmium.io.ThreadPool(num_threads=2)
    source = osmium.FileProcessor(path, osmium.osm.RELATION, thread_pool=pool)
    header = source.header
    if header.has_multiple_object_versions:
        raise ValueError("Expected a current OSM snapshot, not a history or change file")
    snapshot_at = header.get("osmosis_replication_timestamp", "") or header.get("timestamp", "") or None
    for obj in source:
        row = _base(obj)
        if row["key"] in relations:
            raise ValueError(f"Duplicate OSM object in snapshot: {row['key']}")
        row["members"] = _members(obj)
        relations[row["key"]] = row

    boundary_rows: dict[str, dict] = {}
    for code in REGIONS:
        found = [r for r in relations.values() if r["tags"].get("ISO3166-2") == code
                 and r["tags"].get("boundary") == "administrative"]
        if len(found) != 1:
            raise ValueError(f"Expected exactly one complete administrative boundary for {code}; found {len(found)}")
        boundary_rows[code] = found[0]

    routes = {k: r for k, r in relations.items()
              if r["tags"].get("route") == "bus" or r["tags"].get("route_master") == "bus"}
    areas = {k: r for k, r in relations.items() if r["tags"].get("public_transport") == "stop_area"}
    platform_relations = {k: r for k, r in relations.items() if _candidate(r["tags"])
                          and r["tags"].get("public_transport") != "stop_area"}
    needed_relations = set(routes) | set(areas) | set(platform_relations) | {
        r["key"] for r in boundary_rows.values()}
    # Keep recursive relation dependencies, including multipolygon platforms.
    todo = list(needed_relations)
    while todo:
        for m in relations.get(todo.pop(), {}).get("members", []):
            k = _key(m)
            if m["type"] == "relation" and k in relations and k not in needed_relations:
                needed_relations.add(k)
                todo.append(k)
    relations = {k: r for k, r in relations.items() if k in needed_relations}
    needed_objects = {_key(m) for r in relations.values() for m in r["members"]
                      if m["type"] != "relation"}
    route_roles = {_key(m) for r in routes.values() for m in r["members"] if _stop_role(m["role"])}
    area_roles = {_key(m) for r in areas.values() for m in r["members"] if _stop_role(m["role"])}
    records: dict[str, dict] = dict(relations)
    geometries: dict[str, Any] = {}
    geometry_errors: dict[str, str] = {}
    candidates = set(platform_relations) | {
        k for k in area_roles if k in relations and k not in areas and k not in routes
        and not _non_bus(relations[k]["tags"])
    }

    # C++ filters keep millions of unrelated objects out of Python. Both
    # scans cache *all* node coordinates before filtering, so untagged nodes
    # remain available to construct selected way geometries.
    filters = (
        osmium.filter.TagFilter(("highway", "bus_stop"), ("public_transport", "platform"),
                                ("public_transport", "stop_position")),
        osmium.filter.IdFilter({int(k.split("/")[1]) for k in needed_objects}),
    )
    for object_filter in filters:
        pass_seen: set[str] = set()
        processor = osmium.FileProcessor(path, osmium.osm.NODE | osmium.osm.WAY, thread_pool=pool)
        processor.with_locations().with_filter(object_filter)
        for obj in processor:
            kind = "node" if obj.is_node() else "way"
            k = f"{kind}/{obj.id}"
            tags = dict(obj.tags)
            is_stop = _candidate(tags) or k in route_roles or (k in area_roles and not _non_bus(tags))
            if not is_stop and k not in needed_objects:
                continue
            if k in pass_seen:
                raise ValueError(f"Duplicate OSM object in snapshot: {k}")
            pass_seen.add(k)
            if k in records:
                if records[k]["version"] != obj.version or records[k]["tags"] != tags:
                    raise ValueError(f"Inconsistent OSM object in snapshot: {k}")
                continue
            row = _base(obj)
            records[k] = row
            if is_stop:
                candidates.add(k)
            try:
                if obj.is_node():
                    if not obj.location.valid():
                        raise ValueError("missing node coordinates")
                    geometry = Point(obj.location.lon, obj.location.lat)
                else:
                    refs = list(obj.nodes)
                    if len(refs) < 2 or any(not n.location.valid() for n in refs):
                        raise ValueError("missing way nodes or coordinates")
                    coords = [(n.lon, n.lat) for n in refs]
                    if len(set(coords)) < 2:
                        raise ValueError("degenerate way geometry")
                    row["node_refs"] = [n.ref for n in refs]
                    # Only platform/area features, never a closed road, become polygons.
                    if refs[0].ref == refs[-1].ref and len(set(coords)) >= 3 and (
                        tags.get("area") == "yes" or tags.get("public_transport") == "platform"
                    ):
                        geometry = Polygon(coords)
                    else:
                        geometry = LineString(coords)
                    if not geometry.is_valid:
                        raise ValueError("invalid way geometry")
                geometries[k] = geometry
            except (ValueError, osmium.InvalidLocationError) as exc:
                geometry_errors[k] = str(exc)
                geometries[k] = None

    def ring_lines(k: str, visiting: frozenset[str] = frozenset()) -> dict[str, list]:
        if k in visiting:
            raise ValueError(f"{k}: cyclic boundary members")
        r = records.get(k)
        if r is None:
            raise ValueError(f"{k}: missing boundary relation")
        rings: dict[str, list] = {"outer": [], "inner": []}
        for m in r.get("members", []):
            # Labels, admin centres and subareas do not define the polygon.
            if m["role"] not in {"", "outer", "inner"}:
                continue
            mk = _key(m)
            role = m["role"] or "outer"
            if m["type"] == "node":
                continue
            if m["type"] == "relation":
                nested = ring_lines(mk, visiting | {k})
                for nested_role, lines in nested.items():
                    effective = nested_role if role == "outer" else ("inner" if nested_role == "outer" else "outer")
                    rings[effective].extend(lines)
            else:
                geom = geometries.get(mk)
                if geom is None:
                    raise ValueError(f"{k}: missing or incomplete boundary member {mk}")
                rings[role].append(geom.boundary if geom.geom_type == "Polygon" else geom)
        return rings

    features = []
    boundary_geometries = []
    for code, row in boundary_rows.items():
        geom = _polygon(ring_lines(row["key"]), row["key"])
        boundary_geometries.append(geom)
        features.append({"type": "Feature", "id": row["key"], "properties": {
            "iso3166_2": code, "osm_key": row["key"], "version": row["version"],
            "name": row["tags"].get("name"), "tags": row["tags"]}, "geometry": mapping(geom)})
    region = unary_union(boundary_geometries)
    region_index = prep(region)
    if boundary_geometries[0].intersection(boundary_geometries[1]).area > 1e-9:
        raise ValueError("KR-11 and KR-41 administrative boundary interiors overlap")

    def geometry_for(k: str, visiting: frozenset[str] = frozenset()):
        if k in geometries:
            return geometries[k]
        row = records.get(k)
        if row is None:
            geometry_errors[k] = "missing referenced object"
            return None
        if k in visiting:
            geometry_errors[k] = "cyclic relation members"
            return None
        members = row.get("members", [])
        try:
            if row["tags"].get("type") in {"multipolygon", "boundary"}:
                geom = _polygon(ring_lines(k), k)
            else:
                parts = [geometry_for(_key(m), visiting | {k}) for m in members]
                if not parts or any(p is None for p in parts):
                    raise ValueError("missing or incomplete relation member geometry")
                geom = GeometryCollection(parts)
            geometries[k] = geom
            return geom
        except ValueError as exc:
            geometry_errors[k] = str(exc)
            geometries[k] = None
            return None

    # Infer service type through bus routes, then through bus stop areas. Do not
    # turn rail-only platforms at a multimodal interchange into bus platforms.
    evidence = {k: "explicit" if _explicit(records[k]["tags"]) else "unclassified"
                for k in candidates}
    for k in route_roles:
        if k not in records:
            geometry_errors[k] = "missing stop/platform member"
        elif k not in areas and k not in routes:
            candidates.add(k)
            evidence[k] = "explicit" if _explicit(records[k]["tags"]) else "relation"
    for route in routes.values():
        for m in route["members"]:
            k = _key(m)
            if k in candidates and evidence[k] == "unclassified":
                evidence[k] = "relation"
    area_membership: dict[str, list[str]] = defaultdict(list)
    bus_areas: set[str] = set()
    route_members = {_key(m) for r in routes.values() for m in r["members"]}
    for k, area in areas.items():
        for m in area["members"]:
            mk = _key(m)
            if mk in candidates:
                area_membership[mk].append(k)
    changed = True
    while changed:
        changed = False
        for k, area in areas.items():
            if k in bus_areas:
                continue
            keys = [_key(m) for m in area["members"]]
            if (_explicit(area["tags"]) or k in route_members
                    or any(evidence.get(mk) in {"explicit", "relation"} for mk in keys)):
                bus_areas.add(k)
                changed = True
                for mk in keys:
                    if mk in candidates and evidence[mk] == "unclassified":
                        evidence[mk] = "relation"

    def intersects(k: str, visiting: frozenset[str] = frozenset()) -> bool:
        if k in visiting:
            return False
        if k in routes or k in areas:
            return any(intersects(_key(m), visiting | {k}) for m in records[k]["members"])
        geom = geometry_for(k)
        return geom is not None and region_index.intersects(geom)

    stop_scope = {k: intersects(k) for k in candidates}
    route_scope = {k: intersects(k) for k in routes}
    unknown_cache: dict[str, bool] = {}
    def has_unknown_geometry(k: str, visiting: frozenset[str] = frozenset()) -> bool:
        if k in visiting or k not in records:
            return True
        if k in unknown_cache:
            return unknown_cache[k]
        if k in routes or k in areas:
            unknown = not records[k]["members"] or any(
                has_unknown_geometry(_key(m), visiting | {k}) for m in records[k]["members"])
        else:
            unknown = geometry_for(k) is None
        unknown_cache[k] = unknown
        return unknown

    unknown_routes = {k for k in routes if has_unknown_geometry(k)}
    selected_routes = {k for k, value in route_scope.items() if value} | unknown_routes
    # Preserve complete route_master families; out-of-region siblings remain
    # identifiable with in_scope=false, and all relation members stay intact.
    changed = True
    while changed:
        changed = False
        for k, route in routes.items():
            child_routes = {_key(m) for m in route["members"] if _key(m) in routes}
            if k in selected_routes or child_routes & selected_routes:
                additions = ({k} | child_routes) - selected_routes
                if additions:
                    selected_routes.update(additions)
                    changed = True

    def stop_keys(k: str, visiting: frozenset[str] = frozenset()) -> list[str]:
        if k in visiting:
            warnings.add(f"{k}: cyclic transit relation members")
            return []
        found = []
        for m in records[k].get("members", []):
            mk = _key(m)
            if mk in candidates:
                found.append(mk)
            elif mk in routes or mk in areas:
                found.extend(stop_keys(mk, visiting | {k}))
            elif _stop_role(m["role"]):
                # Preserve the unresolved reference in the ordered list as well.
                found.append(mk)
        return found

    route_stops = {k: stop_keys(k) for k in selected_routes}
    selected_stops = {k for k in candidates if stop_scope[k] or geometry_for(k) is None}
    selected_stops.update(mk for keys in route_stops.values() for mk in keys if mk in candidates)
    selected_areas = {k for k in areas if any(_key(m) in selected_stops for m in areas[k]["members"])
                      or (k in bus_areas and (intersects(k) or has_unknown_geometry(k)))}
    # A stop area may group an in-region platform with a stop_position just
    # outside the border; preserve both representations of the physical stop.
    selected_stops.update(_key(m) for k in selected_areas for m in areas[k]["members"] if _key(m) in candidates)

    def positioned(k: str) -> dict:
        row = records[k]
        geom = geometry_for(k)
        p = geom.representative_point() if geom is not None and not geom.is_empty else None
        return {**{name: row[name] for name in ("key", "type", "id", "version", "tags")},
                "lon": p.x if p is not None else None, "lat": p.y if p is not None else None,
                "geometry": mapping(geom) if geom is not None else None, "in_scope": intersects(k),
                "scope_state": "unknown" if geom is None else "inside" if intersects(k) else "outside",
                "geometry_complete": geom is not None}

    stops_out = [{**positioned(k), "bus_evidence": evidence[k],
                  "stop_area_ids": sorted(area_membership[k])} for k in sorted(selected_stops)]
    routes_out = [{**routes[k], "stop_keys": route_stops[k], "in_scope": route_scope[k],
                   "scope_state": "inside" if route_scope[k] else "unknown" if k in unknown_routes else "outside",
                   "geometry_complete": k not in unknown_routes}
                  for k in sorted(selected_routes)]
    areas_out = [{**positioned(k), "members": areas[k]["members"], "stop_keys": stop_keys(k),
                  "bus_evidence": "explicit" if _explicit(areas[k]["tags"]) else "relation" if k in bus_areas else "unclassified"}
                 for k in sorted(selected_areas)]

    # Validate every reference of selected transit relations, not just stops.
    checked: set[str] = set()
    integrity_errors: set[str] = set()
    def check(k: str, visiting: frozenset[str] = frozenset()) -> None:
        if k in visiting:
            integrity_errors.add(f"{k}: cyclic relation members")
            return
        if k in checked:
            return
        checked.add(k)
        if k not in records:
            integrity_errors.add(f"{k}: missing referenced object")
            return
        if records[k]["type"] == "relation":
            if k in routes and not records[k]["members"]:
                integrity_errors.add(f"{k}: empty bus route relation")
            for m in records[k].get("members", []):
                check(_key(m), visiting | {k})
            if k not in routes and geometry_for(k) is None:
                integrity_errors.add(f"{k}: {geometry_errors.get(k, 'missing relation geometry')}")
        elif geometry_for(k) is None:
            integrity_errors.add(f"{k}: {geometry_errors.get(k, 'missing geometry')}")
    for k in selected_routes | selected_areas | selected_stops:
        check(k)
        if k in selected_stops and geometry_for(k) is None:
            integrity_errors.add(f"{k}: {geometry_errors.get(k, 'missing geometry')}")
    warnings.update(integrity_errors)
    if snapshot_at is None:
        warnings.add("OSM snapshot timestamp is absent from the file header; object edit times are not a snapshot timestamp")
    if unknown_routes:
        warnings.add(f"{len(unknown_routes)} bus route relations have incomplete geometry; geographic scope may be unknown")
    unknown_count = sum(s["geometry"] is None for s in stops_out)
    if unknown_count:
        warnings.add(f"{unknown_count} stop objects have unknown geometry and cannot be assigned a geographic scope")
    return {"schema_version": 1, "snapshot_at": snapshot_at,
            "scope": {"iso3166_2": list(REGIONS), "method": "osm_administrative_boundaries",
                      "boundary_relation_ids": [r["key"] for r in boundary_rows.values()],
                      "boundary_complete": True, "reference_complete": not integrity_errors,
                      "completeness": "boundary_and_selected_transit_references_checked",
                      "full_extract_provenance_required": True, "unknown_geometry_count": unknown_count,
                      "unknown_route_geometry_count": len(unknown_routes)},
            "boundaries": {"type": "FeatureCollection", "features": features},
            "stops": stops_out, "routes": routes_out, "stop_areas": areas_out,
            "warnings": sorted(warnings)}
