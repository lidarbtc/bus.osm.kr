"""Conservative, explainable correspondence candidates. Never emits edits."""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter, defaultdict
from difflib import SequenceMatcher

from shapely.geometry import Point, shape
from shapely.ops import unary_union
from shapely.prepared import prep


def normalize(value: str) -> str:
    return re.sub(r"[^\w]", "", unicodedata.normalize("NFKC", str(value or "")).casefold())


def route_ref(value: str) -> str:
    # A hyphen is part of a service identity: 10-1 and 101 are different buses.
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(value or "")).casefold())


def stop_ref(value: str) -> str:
    value = re.sub(r"[\s-]", "", str(value or ""))
    if not value or (value.isdigit() and int(value) == 0):
        return ""
    return value.zfill(5) if value.isdigit() and len(value) <= 5 else value


def distance(a: dict, b: dict) -> float:
    la, lb = math.radians(a["lat"]), math.radians(b["lat"])
    dn = math.radians(b["lon"] - a["lon"])
    q = math.sin((lb - la) / 2)**2 + math.cos(la) * math.cos(lb) * math.sin(dn / 2)**2
    return 12742000 * math.asin(min(1, math.sqrt(q)))


def object_refs(tags: dict, provider: str) -> set[str]:
    # local_ref numbers individual bays, not a provider stop identity.
    values = set()
    for key, value in tags.items():
        # A namespaced reference from another system is not interchangeable.
        recognized = {"seoul": {"ref:seoul", "ref:KR:seoul"},
                      "gbis": {"ref:gbis", "ref:KR:gbis"}}
        if key == "ref" or key in recognized.get(provider, set()):
            for part in re.split(r"[;,]", value):
                ref = stop_ref(part)
                if ref:
                    values.add(ref)
    return values


class Grid:
    def __init__(self, rows, cell=0.002):
        self.cell = cell
        self.rows = defaultdict(list)
        for row in rows:
            if row.get("lon") is not None and row.get("lat") is not None:
                self.rows[self.key(row)].append(row)

    def key(self, row):
        return math.floor(row["lon"] / self.cell), math.floor(row["lat"] / self.cell)

    def nearby(self, row):
        x, y = self.key(row)
        for dx in range(-2, 3):
            for dy in range(-2, 3):
                yield from self.rows[(x + dx, y + dy)]


def _name_matches(a, b):
    return bool(normalize(a)) and normalize(a) == normalize(b)


def _stop_candidates(osm, grid):
    if osm.get("lat") is None or osm.get("lon") is None:
        return []
    tags = osm["tags"]
    refs_by_provider = {p: object_refs(tags, p) for p in ("seoul", "gbis")}
    names = [tags.get("name", ""), tags.get("name:ko", "")]
    candidates = []
    for row in grid.nearby(osm):
        d = distance(osm, row)
        if d > 250:
            continue
        refs = refs_by_provider.get(row["provider"], set())
        ref_hit = bool(stop_ref(row.get("ref")) and stop_ref(row["ref"]) in refs)
        id_hit = str(row["id"]) in refs
        name_hit = any(_name_matches(n, row["name"]) for n in names)
        # An id at a remote location is not silently accepted. Proximity alone
        # is only evidence for review, including platforms across a road.
        if not (id_hit or ref_hit or name_hit or d <= 35):
            continue
        basis = [name for name, yes in (("source_id", id_hit), ("public_ref", ref_hit),
                                        ("name", name_hit)) if yes]
        confidence = ("strong" if (id_hit or ref_hit) and d <= 100
                      and (name_hit or d <= 30) else "review")
        candidates.append({"source_key": row["key"], "provider": row["provider"],
                           "source_id": row["id"], "source_name": row["name"],
                           "distance_m": round(d, 1), "basis": basis or ["proximity"],
                           "confidence": confidence,
                           "score": (100 if id_hit else 60 if ref_hit else 30 if name_hit else 0) - d / 100})
    candidates.sort(key=lambda r: (-r["score"], r["source_key"]))
    # Multiple source entities in one provider with credible references require
    # review. Matching a platform and stop_position to one source is permitted.
    strong = [c for c in candidates if c["confidence"] == "strong"]
    ambiguous = any(n > 1 for n in Counter(c["provider"] for c in strong).values())
    if ambiguous:
        for c in candidates:
            c["confidence"] = "review"
            c["ambiguity"] = "multiple_source_stops_in_provider"
    return candidates


def _route_ref(tags):
    ref = str(tags.get("ref", "")).strip()
    if ref:
        return route_ref(ref)
    match = re.fullmatch(r"(?:서울|경기)?\s*(?:시내)?버스\s+([\w-]+)", tags.get("name", ""))
    return route_ref(match.group(1)) if match else ""


def _operators(value):
    return {normalize(re.sub(r"\(주\)|주식회사", "", part))
            for part in re.split(r"[,;/]", value or "") if normalize(part)}


def compare(sources: dict, inventory: dict, exceptions: dict) -> dict:
    """Compare every supplied in-scope object; unmatched always means preserve.

    Result counts are source records and OSM objects, never physical stop counts.
    route=bus and route_master=bus are distinguished in each record.
    """
    boundary = unary_union([shape(f["geometry"]) for f in inventory["boundaries"]["features"]])
    boundary_index = prep(boundary)
    exception_index = {key: entry for entry in exceptions["exceptions"] for key in entry["osm_objects"]}
    bus_sources = [s for s in sources["stops"] if s.get("mode", "bus") == "bus"]
    grid = Grid(bus_sources)
    source_stop_lookup = {r["key"]: r for r in sources["stops"]}
    referenced_stops = defaultdict(list)
    strong_links = defaultdict(set)
    stop_rows = []
    for osm in sorted(inventory["stops"], key=lambda r: r["key"]):
        candidates = _stop_candidates(osm, grid)
        eligible = osm.get("bus_evidence") != "unclassified"
        status = ("matched" if any(c["confidence"] == "strong" for c in candidates)
                  else "candidate_match" if candidates else "osm_unmatched")
        if not eligible:
            status = "needs_classification"
        scope_state = osm.get("scope_state", "inside" if osm.get("in_scope", True) else
                              "unknown" if osm.get("lon") is None else "outside")
        if scope_state == "unknown":
            status = "needs_scope_review"
        exception = exception_index.get(osm["key"])
        if exception:
            status = "preserved_exception"
        for c in candidates:
            if eligible:
                referenced_stops[c["source_key"]].append({"osm_key": osm["key"], **c})
                if c["confidence"] == "strong":
                    strong_links[osm["key"]].add(c["source_key"])
        tags = osm["tags"]
        row = {"side": "osm", "key": osm["key"], "kind": "stop", "status": status,
               "name": tags.get("name", tags.get("name:ko", "")), "ref": tags.get("ref", ""),
               "provider": "osm", "lon": osm.get("lon"), "lat": osm.get("lat"),
               "in_scope": osm.get("in_scope", True), "bus_evidence": osm.get("bus_evidence"),
               "scope_state": scope_state,
               "representation": tags.get("public_transport", tags.get("highway", "")),
               "stop_area_ids": osm.get("stop_area_ids", []), "version": osm.get("version"),
               "candidates": candidates, "action": "preserve", "exception": exception}
        stop_rows.append(row)
    for src in sorted(sources["stops"], key=lambda r: r["key"]):
        if not boundary_index.covers(Point(src["lon"], src["lat"])):
            continue
        candidates = referenced_stops[src["key"]]
        status = ("needs_classification" if src.get("mode") == "unknown" else
                  "out_of_mode_scope" if src.get("mode", "bus") != "bus" else
                  "matched" if any(c["confidence"] == "strong" for c in candidates) else
                  "candidate_match" if candidates else "source_unmatched")
        stop_rows.append({"side": "source", "key": src["key"], "kind": "stop", "status": status,
                          "name": src["name"], "ref": src.get("ref", ""), "provider": src["provider"],
                          "lon": src["lon"], "lat": src["lat"], "in_scope": True,
                          "mode": src.get("mode", "bus"), "candidates": candidates,
                          "action": "review_only", "exception": None})
    # Index all provider route identities; duplicate public numbers remain separate.
    route_index = defaultdict(list)
    for r in sources["routes"]:
        route_index[route_ref(r["name"])].append(r)
    source_route_links = defaultdict(list)
    route_rows = []
    for osm in sorted(inventory["routes"], key=lambda r: r["key"]):
        tags = osm["tags"]
        in_scope = osm.get("in_scope", True)
        scope_state = osm.get("scope_state", "inside" if in_scope else "unknown")
        master = tags.get("route_master") == "bus"
        candidates = []
        for src in route_index.get(_route_ref(tags), []):
            if src.get("mode", "bus") == "ferry":
                continue
            operator_hit = bool(_operators(tags.get("operator", "")) & _operators(src.get("operator", "")))
            mapped = []
            for key in osm.get("stop_keys", []):
                ids = [source_stop_lookup[s]["id"] for s in strong_links[key]
                       if source_stop_lookup[s]["provider"] == src["provider"]]
                # platform/stop_position pairs can represent a single visit.
                if len(ids) == 1 and (not mapped or mapped[-1] != ids[0]):
                    mapped.append(ids[0])
            public_sequence = src.get("stop_ids", [])
            blocks = SequenceMatcher(None, mapped, public_sequence, autojunk=False).get_matching_blocks()
            ordered = sum(b.size for b in blocks)
            overlap = len(set(mapped) & set(public_sequence))
            strong = (src.get("mode", "bus") in {"bus", "drt"}
                      and (operator_hit or (overlap >= 3 and ordered >= 3)))
            candidates.append({"source_key": src["key"], "provider": src["provider"],
                               "source_name": src["name"], "source_operator": src.get("operator", ""),
                               "confidence": "metadata" if strong else "review",
                               "basis": ["route_ref"] + (["operator"] if operator_hit else [])
                                        + (["stop_sequence"] if ordered >= 3 else []),
                               "mapped_osm_stop_count": len(mapped), "shared_stop_count": overlap,
                               "ordered_overlap_count": ordered,
                               "source_sequence_complete": src.get("sequence_complete", False),
                               "geometry_verified": False})
        metadata = [c for c in candidates if c["confidence"] == "metadata"]
        if len(metadata) > 1:
            for c in candidates:
                c["confidence"] = "review"
        status = ("matched_metadata" if any(c["confidence"] == "metadata" for c in candidates) else
                  "candidate_match" if candidates else "osm_unmatched")
        if scope_state == "unknown":
            status = "needs_scope_review"
        elif not in_scope:
            status = "outside_scope_support"
        exception = exception_index.get(osm["key"])
        if exception:
            status = "preserved_exception"
        for c in candidates:
            source_route_links[c["source_key"]].append({"osm_key": osm["key"], "osm_kind": "route_master" if master else "route", "in_scope": in_scope, **c})
        route_rows.append({"side": "osm", "key": osm["key"], "kind": "route_master" if master else "route",
                           "status": status, "provider": "osm", "name": tags.get("name", ""),
                           "ref": tags.get("ref", ""), "operator": tags.get("operator", ""),
                           "candidates": candidates, "action": "preserve", "exception": exception,
                           "member_count": len(osm.get("members", [])), "version": osm.get("version"),
                           "geometry_verified": False, "in_scope": in_scope, "scope_state": scope_state})
    for src in sorted(sources["routes"], key=lambda r: r["key"]):
        candidates = source_route_links[src["key"]]
        status = ("out_of_mode_scope" if src.get("mode", "bus") == "ferry" else
                  "matched_metadata" if any(c["confidence"] == "metadata" and c["osm_kind"] == "route" and c["in_scope"] for c in candidates) else
                  "candidate_match" if candidates else "source_unmatched")
        if src.get("mode") == "unknown":
            status = "needs_classification"
        route_rows.append({"side": "source", "key": src["key"], "kind": "route", "status": status,
                           "provider": src["provider"], "name": src["name"], "ref": src["name"],
                           "operator": src.get("operator", ""), "mode": src.get("mode", "bus"),
                           "candidates": candidates, "sequence_complete": src.get("sequence_complete", False),
                           "action": "review_only", "exception": None, "in_scope": True,
                           "geometry_verified": False})
    actual_keys = {r["key"] for r in inventory["stops"] + inventory["routes"]}
    exception_status = [{"id": e["id"], "review_due": e["review_due"], "action": "preserve",
                         "observed": sorted(set(e["osm_objects"]) & actual_keys),
                         "not_observed": sorted(set(e["osm_objects"]) - actual_keys)}
                        for e in exceptions["exceptions"]]
    return {"schema_version": 1, "read_only": True,
            "scope": inventory["scope"], "osm_snapshot_at": inventory.get("snapshot_at"),
            "source_snapshots": sources["snapshots"],
            "counts": {"stops": dict(Counter(f"{r['side']}:{r['status']}" for r in stop_rows if r["in_scope"])),
                       "routes": dict(Counter(f"{r['side']}:{r['kind']}:{r['status']}" for r in route_rows if r["in_scope"]))},
            "cross_boundary_support_stop_objects": sum(r.get("scope_state") == "outside" for r in stop_rows),
            "cross_boundary_support_route_objects": sum(r.get("scope_state") == "outside" for r in route_rows),
            "unknown_scope_stop_objects": sum(r.get("scope_state") == "unknown" for r in stop_rows),
            "unknown_scope_route_objects": sum(r.get("scope_state") == "unknown" for r in route_rows),
            "count_units": {"source": "provider records, not unique physical stops/services",
                            "osm": "OSM objects; platforms, stop positions and route directions may be separate"},
            "limitations": ["Unmatched is not proof of absence, closure or a needed addition.",
                            "Spatial/name matches are review candidates, not verified identities.",
                            "Route matching verifies metadata/ordered stop overlap, not road geometry or timetables.",
                            "Public feeds may have different dates and include modes or regions outside this audit."],
            "warnings": list(sources.get("warnings", [])) + list(inventory.get("warnings", [])),
            "exceptions": exception_status, "stops": stop_rows, "routes": route_rows}
