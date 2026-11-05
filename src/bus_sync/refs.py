"""Plan which mapped OSM bus stops can receive their official stop number.

The plan only ever adds "ref" to a platform that has no stop number yet. It never
creates, moves, merges, renames or deletes anything. Every decision carries its
evidence, and everything that needs a mapper (stops mapped twice, conflicting
numbers, unclear sides of a road) is returned as a task list.

Evidence used for each pairing of an OSM stop with an official stop:
* name and distance;
* the side of the road: buses in Korea keep right, so a stop lies to the right of
  its travel direction, which comes from the order of stops in official routes;
* the other stops of the same name, which are paired together so that one
  number is never given to two stops;
* official stops with other names and numbers nearby, which must be clearly farther away.
"""

from __future__ import annotations

import itertools
import json
import math
import re
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from shapely.geometry import Point, shape
from shapely.prepared import prep

from .compare import Grid, distance, normalize, stop_ref

MATCH_M = 30.0           # an OSM stop and its official stop lie at most this far apart
SIDE_MATCH_M = 60.0      # ... or this far when the side of the road confirms the pairing
SIMILAR_MATCH_M = 20.0   # ... or this far when the names are only similar
MARGIN_M = 10.0          # the chosen pairing must beat every alternative by this much
CLEAR_MARGIN_M = 20.0    # ... or this much where the stop could be either side of the road
UNPAIRED_COST_M = 40.0   # cost of leaving an OSM stop without an official partner
CLUSTER_M = 100.0        # same-name stops closer than this are decided together
DUPLICATE_M = 20.0       # same-name OSM platforms closer than this are one stop mapped twice
RECORD_MERGE_M = 60.0    # official records with the same number are one stop
DUAL_MERGE_M = 30.0      # ... and so are Seoul and GBIS records of one name and direction
SIDE_M = 2.5             # minimum offset from the road centre to decide a side
# Travel directions come from neighbouring stops hundreds of metres away, so the road
# here may turn by this much; a side only counts if it survives that error.
HEADING_TOLERANCE = math.tan(math.radians(25))
NUMBER_RADIUS_M = 3000.0
MAX_SIDE = 7             # larger clusters are listed for mappers instead
LOCAL_PROVIDER = {"KR-11": "seoul", "KR-41": "gbis"}
OPPOSITE_WORDS = ("건너", "맞은편", "반대편", "방면")

# What a mapper is asked to do for each kind of task; a task file can be loaded into MapRoulette.
TASK_INSTRUCTIONS = {
    "mapped_twice": "같은 정류장이 두 번 그려져 있습니다. 하나로 합쳐 주세요.",
    "no_official_partner": "같은 이름의 공식 정류장이 60m 안에 없거나 이미 다른 OSM 정류장과 짝지어졌습니다. 위치와 이름을 확인해 주세요.",
    "unclear_pairing": "같은 이름의 정류장이 길 양쪽에 있어 번호를 확정하지 못했습니다. 표지판의 정류장 번호를 ref에 넣어 주세요.",
    "side_of_road_disagrees": "공식 좌표와 버스 진행 방향이 서로 다른 쪽을 가리킵니다. 정류장 위치와 표지판 번호를 확인해 주세요.",
    "other_official_stop_as_close": "이름이 다른 공식 정류장도 비슷한 거리에 있습니다. 표지판의 정류장 이름과 번호를 확인해 주세요.",
    "same_stop_as_numbered_neighbour": "가장 가까운 공식 정류장의 번호가 이미 옆 OSM 정류장에 있습니다. 같은 정류장이면 합쳐 주세요.",
    "neighbour_number_not_in_official_data": "근처 같은 이름 정류장의 번호가 공식 데이터에 없습니다. 번호가 바뀌었는지 확인해 주세요.",
    "too_many_same_name_stops": "같은 이름의 정류장이 한곳에 많아 자동으로 짝짓지 않았습니다. 표지판 번호를 확인해 주세요.",
    "local_stop_number_unknown": "이 지역 기관이 매긴 정류장 번호가 공식 데이터에 없습니다. 표지판 번호를 확인해 주세요.",
    "no_official_stop_with_this_name": "같거나 비슷한 이름의 공식 정류장이 근처에 없습니다. 이름이 바뀌었거나 없어진 정류장인지 확인해 주세요.",
    "similar_name_unclear": "이름이 비슷한 공식 정류장이 여러 곳이라 번호를 확정하지 못했습니다. 표지판을 확인해 주세요.",
    "number_already_on_nearby_stop": "같은 번호가 3km 안의 다른 OSM 객체에 이미 있습니다. 어느 쪽이 맞는지 확인해 주세요.",
    "number_would_be_used_twice": "공식 데이터에서 같은 번호가 가까운 두 정류장에 쓰입니다. 표지판을 확인해 주세요.",
    "platform_is_not_a_node": "정류장이 선이나 관계로만 그려져 있어 자동으로 번호를 붙이지 않았습니다.",
}

# Short names of the same tasks for summaries.
TASK_LABELS = {
    "mapped_twice": "두 번 그려진 정류장",
    "no_official_partner": "짝이 되는 공식 정류장 없음",
    "unclear_pairing": "길 양쪽 구분 불가",
    "side_of_road_disagrees": "좌표와 진행 방향 불일치",
    "other_official_stop_as_close": "다른 공식 정류장도 가까움",
    "same_stop_as_numbered_neighbour": "번호 있는 옆 정류장과 같은 곳",
    "neighbour_number_not_in_official_data": "옆 정류장 번호가 공식 데이터에 없음",
    "too_many_same_name_stops": "같은 이름 정류장이 많음",
    "local_stop_number_unknown": "지역 번호가 공식 데이터에 없음",
    "no_official_stop_with_this_name": "같은 이름의 공식 정류장 없음",
    "similar_name_unclear": "비슷한 이름 후보 여러 곳",
    "number_already_on_nearby_stop": "근처에 같은 번호 있음",
    "number_would_be_used_twice": "공식 번호 중복",
    "platform_is_not_a_node": "선이나 관계로 그려진 정류장",
}

NUMBER = re.compile(r"(?<!\d)(\d{2})-?(\d{3})(?!\d)")


def numbers_in(tags: dict) -> set[str]:
    """Stop numbers already on an OSM object, in any notation (08-047, 22334;22434, ref:*)."""
    found = set()
    for key, value in tags.items():
        if key == "ref" or key.startswith("ref:"):
            found.update(a + b for a, b in NUMBER.findall(str(value)))
    return found


def similar_names(a: str, b: str) -> bool:
    """Normalised names that differ only in wording, never in numbers or direction words."""
    if not a or not b:
        return False
    if a == b:
        return True
    if any((w in a) != (w in b) for w in OPPOSITE_WORDS) or re.findall(r"\d+", a) != re.findall(r"\d+", b):
        return False
    short, long = sorted((a, b), key=len)
    return (len(short) >= 3 and short in long) or SequenceMatcher(None, a, b).ratio() >= 0.8


def _is_platform(stop: dict) -> bool:
    tags = stop["tags"]
    return ((tags.get("highway") == "bus_stop" or tags.get("public_transport") == "platform")
            and stop.get("bus_evidence") != "unclassified" and "railway" not in tags)


def _vector(a, b):
    """Metres east and north from a to b."""
    k = math.cos(math.radians((a["lat"] + b["lat"]) / 2))
    return (b["lon"] - a["lon"]) * 111320 * k, (b["lat"] - a["lat"]) * 110540


def _unit(sum_x, sum_y, count, agreement=0.7):
    length = math.hypot(sum_x, sum_y)
    return (sum_x / length, sum_y / length) if count and length / count >= agreement else None


def _same_direction(a, b) -> bool:
    return a is not None and b is not None and a[0] * b[0] + a[1] * b[1] > 0.7


def travel_directions(sources: dict) -> dict:
    """Unit travel direction of each official stop record, from the order of stops in routes."""
    records = {(r["provider"], str(r["id"])): r for r in sources["stops"] if r.get("lon") is not None}
    sums = defaultdict(lambda: [0.0, 0.0, 0])
    for route in sources.get("routes", []):
        seq = [records.get((route["provider"], str(i))) for i in route.get("stop_ids", [])]
        for i, r in enumerate(seq):
            if r is None:
                continue
            before = next((seq[j] for j in range(i - 1, -1, -1) if seq[j] is not None and seq[j]["key"] != r["key"]), r)
            after = next((seq[j] for j in range(i + 1, len(seq)) if seq[j] is not None and seq[j]["key"] != r["key"]), r)
            dx, dy = _vector(before, after)
            length = math.hypot(dx, dy)
            if length >= 20:
                s = sums[r["key"]]
                s[0] += dx / length
                s[1] += dy / length
                s[2] += 1
    return {k: v for k, (x, y, n) in sums.items() if (v := _unit(x, y, n))}


class _Sets:
    def __init__(self, items):
        self.parent = {i: i for i in items}

    def find(self, i):
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def union(self, a, b):
        self.parent[self.find(a)] = self.find(b)

    def groups(self):
        out = defaultdict(list)
        for i in self.parent:
            out[self.find(i)].append(i)
        return list(out.values())


@dataclass(eq=False)
class Official:
    key: str
    numbers: dict           # provider -> stop number
    names: set              # normalised names used by either provider
    name: str
    lon: float
    lat: float
    heading: tuple | None
    records: list = field(default_factory=list)

    @property
    def all_numbers(self) -> set:
        return {stop_ref(r.get("ref")) for r in self.records} - {""}

    def number_for(self, region: str) -> str:
        """The number on the stop sign is the one of the area the stop is in."""
        local = LOCAL_PROVIDER.get(region)
        if local in self.numbers:
            return self.numbers[local]
        values = set(self.numbers.values())
        # GBIS mostly reuses Seoul's numbers for Seoul stops; Seoul's own numbers for
        # stops in Gyeonggi-do are not the ones that mappers find on the sign.
        return values.pop() if len(values) == 1 and local == "seoul" else ""


@dataclass(eq=False)
class Mapped:
    key: str
    members: list
    name: str
    norm: str
    lon: float
    lat: float
    numbers: set
    target: dict
    in_scope: bool
    region: str


def official_stops(sources: dict) -> list[Official]:
    """One entry per physical stop. Seoul and GBIS list many stops twice, under two names or numbers."""
    directions = travel_directions(sources)
    rows = [r for r in sources["stops"] if r.get("mode", "bus") == "bus" and r.get("lon") is not None
            and normalize(r.get("name"))]
    for i, r in enumerate(rows):
        r["_i"] = i
    grid = Grid(rows)
    sets = _Sets(range(len(rows)))
    for a in rows:
        na, ha = stop_ref(a.get("ref")), directions.get(a["key"])
        for b in grid.nearby(a):
            if b["_i"] <= a["_i"]:
                continue
            d = distance(a, b)
            if d > RECORD_MERGE_M:
                continue
            nb = stop_ref(b.get("ref"))
            if (na and na == nb) or (
                    d <= DUAL_MERGE_M and normalize(a["name"]) == normalize(b["name"])
                    and _same_direction(ha, directions.get(b["key"]))
                    and (a["provider"] != b["provider"] or not na or not nb)):
                sets.union(a["_i"], b["_i"])
    result = []
    for part in sets.groups():
        members = sorted((rows[i] for i in part), key=lambda r: r["key"])
        numbers = {}
        for provider in ("seoul", "gbis"):
            values = Counter(stop_ref(r.get("ref")) for r in members if r["provider"] == provider and stop_ref(r.get("ref")))
            if values:
                numbers[provider] = values.most_common(1)[0][0]
        vectors = [directions[r["key"]] for r in members if r["key"] in directions]
        heading = _unit(sum(v[0] for v in vectors), sum(v[1] for v in vectors), len(vectors)) if vectors else None
        result.append(Official(
            key=f"official:{members[0]['key']}", numbers=numbers, names={normalize(r["name"]) for r in members},
            name=Counter(r["name"] for r in members).most_common(1)[0][0],
            lon=sum(r["lon"] for r in members) / len(members), lat=sum(r["lat"] for r in members) / len(members),
            heading=heading, records=[{k: v for k, v in r.items() if k != "_i"} for r in members]))
    for r in rows:
        r.pop("_i", None)
    return result


def _regions(inventory):
    shapes = [(f["properties"]["iso3166_2"], prep(shape(f["geometry"])))
              for f in inventory["boundaries"]["features"]]

    def region(item) -> str:
        point = Point(item["lon"], item["lat"])
        return next((code for code, geom in shapes if geom.covers(point)), "")
    return region


def mapped_stops(stops: list[dict], officials: list[Official], region) -> list[Mapped]:
    """Group OSM platforms that are one stop mapped twice (node and area, or two nodes)."""
    platforms = [s for s in stops if s.get("lat") is not None and _is_platform(s)]
    official_grid = Grid([{"lon": o.lon, "lat": o.lat, "o": o} for o in officials])
    grid = Grid(platforms)
    index = {s["key"]: i for i, s in enumerate(platforms)}
    sets = _Sets(range(len(platforms)))
    areas = {s["key"]: prep(shape(s["geometry"])) for s in platforms
             if s.get("geometry") and s["geometry"]["type"] in {"Polygon", "MultiPolygon"}}

    def separate_officially(a, b, norm):
        nearby = {x["o"] for x in official_grid.nearby(a) if norm in x["o"].names
                  and distance(a, x["o"].__dict__) <= RECORD_MERGE_M and distance(b, x["o"].__dict__) <= RECORD_MERGE_M}
        return len(nearby) >= 2

    def same_stop(a, b):
        name_a, name_b = normalize(a["tags"].get("name")), normalize(b["tags"].get("name"))
        if name_a and name_b and name_a != name_b:
            return False
        inside = any(k in areas and areas[k].covers(Point(o["lon"], o["lat"]))
                     for k, o in ((a["key"], b), (b["key"], a)))
        if not inside and distance(a, b) > DUPLICATE_M:
            return False
        # Two same-name platforms are separate stops when official data has two stops there.
        return not (name_a and name_b and separate_officially(a, b, name_a))

    for a in platforms:
        for b in grid.nearby(a):
            if index[b["key"]] > index[a["key"]] and same_stop(a, b):
                sets.union(index[a["key"]], index[b["key"]])
    result = []
    for part in sets.groups():
        members = sorted((platforms[i] for i in part), key=lambda s: s["key"])
        named = [m for m in members if normalize(m["tags"].get("name"))]
        name = named[0]["tags"].get("name", "") if named else ""
        # Prefer the conventional highway=bus_stop node when a stop is mapped twice.
        target = sorted(members, key=lambda m: (m["tags"].get("highway") != "bus_stop", m["type"] != "node", m["key"]))[0]
        lon = sum(m["lon"] for m in members) / len(members)
        lat = sum(m["lat"] for m in members) / len(members)
        result.append(Mapped(
            key=members[0]["key"], members=members, name=name, norm=normalize(name), lon=lon, lat=lat,
            numbers=set().union(*(numbers_in(m["tags"]) for m in members)), target=target,
            in_scope=all(m.get("in_scope", True) for m in members), region=region({"lon": lon, "lat": lat})))
    return result


def _side(stop, official, centre) -> int:
    """+1 if the stop is clearly right of the official stop's travel direction, -1 if clearly left, else 0."""
    if official.heading is None or centre is None:
        return 0
    dx, dy = _vector(centre, stop.__dict__)
    hx, hy = official.heading
    right = dx * hy - dy * hx
    along = abs(dx * hx + dy * hy)
    needed = SIDE_M + along * HEADING_TOLERANCE
    return 1 if right >= needed else -1 if right <= -needed else 0


def _across_road(stop, official) -> bool:
    """True when the official stop clearly serves the other direction, across the road."""
    centre = {"lon": (stop.lon + official.lon) / 2, "lat": (stop.lat + official.lat) / 2}
    return _side(stop, official, centre) == -1


def _two_sided(officials) -> tuple[bool, bool]:
    """(opposite directions known, either side of a road possible)."""
    opposite = any(a.heading and b.heading and a.heading[0] * b.heading[0] + a.heading[1] * b.heading[1] < -0.5
                   for a, b in itertools.combinations(officials, 2))
    possible = opposite or (len(officials) >= 2 and not all(
        _same_direction(a.heading, b.heading) for a, b in itertools.combinations(officials, 2)))
    return opposite, possible


def _assignments(count, officials, allowed):
    def walk(i, used, chosen, cost):
        if i == count:
            yield cost, tuple(chosen)
            return
        yield from walk(i + 1, used, chosen + [None], cost + UNPAIRED_COST_M)
        for j in range(len(officials)):
            if j not in used and (i, j) in allowed:
                yield from walk(i + 1, used | {j}, chosen + [j], cost + allowed[(i, j)])
    yield from walk(0, frozenset(), [], 0.0)


def _decide(open_m, free, cluster_m, cluster_o):
    """Pair OSM stops with official stops of the same name by distance, then check the side of the road.

    Returns {index: (official index, distance, margin, side, two_sided, others_ruled_out)};
    side is +1 when the side of the road confirms the pairing, -1 when it contradicts it, 0 if undecided.
    """
    opposite, two_sided = _two_sided(cluster_o)
    centre = None
    if opposite:
        anchor = cluster_m if len(cluster_m) >= 2 else cluster_o
        centre = {"lon": sum(x.lon for x in anchor) / len(anchor), "lat": sum(x.lat for x in anchor) / len(anchor)}
    allowed = {}
    for i, m in enumerate(open_m):
        for j, o in enumerate(free):
            d = distance(m.__dict__, o.__dict__)
            if m.norm in o.names and d <= SIDE_MATCH_M:
                allowed[(i, j)] = d
    options = sorted(_assignments(len(open_m), free, allowed), key=lambda x: x[0])
    best_cost, best = options[0]
    decided = {}
    for i, j in enumerate(best):
        if j is None:
            continue
        gap = min((cost for cost, other in options if other[i] != j), default=math.inf) - best_cost
        side = _side(open_m[i], free[j], centre) if opposite else 0
        # The side only settles a close call if it also rules out every other candidate.
        others_ruled_out = all(_side(open_m[i], free[k], centre) == -1 for k in range(len(free))
                               if k != j and (i, k) in allowed)
        decided[i] = (j, allowed[(i, j)], gap, side, two_sided, others_ruled_out)
    return decided


def plan_refs(sources: dict, inventory: dict) -> dict:
    region = _regions(inventory)
    districts = [(f["properties"], prep(shape(f["geometry"])))
                 for f in inventory.get("districts", {}).get("features", [])]

    def district(item) -> dict:
        point = Point(item["lon"], item["lat"])
        return next(({"name": p["name"], "code": p["code"]} for p, geom in districts if geom.covers(point)),
                    {"name": "", "code": ""})

    officials = official_stops(sources)
    official_grid = Grid([{"lon": o.lon, "lat": o.lat, "o": o} for o in officials])
    mapped = mapped_stops(inventory["stops"], officials, region)
    changes, tasks = [], []
    by_number = defaultdict(list)
    for m in mapped:
        for n in m.numbers:
            by_number[n].append(m)

    def task(kind, m, **detail):
        tasks.append({"kind": kind, "osm_keys": [x["key"] for x in m.members], "name": m.name,
                      "lon": m.lon, "lat": m.lat, **detail})

    def claimed(o):
        """An official stop already represented by a numbered OSM stop nearby."""
        return any(distance(o.__dict__, x.__dict__) <= RECORD_MERGE_M
                   for n in o.all_numbers for x in by_number.get(n, []))

    def rivals(m, o, d, slack):
        """Official stops with other names and numbers that are about as close as the chosen one.

        Stops of the same name were already weighed against each other when pairing."""
        return [x["o"] for x in official_grid.nearby(m.__dict__) if x["o"] is not o
                and m.norm not in x["o"].names and not (x["o"].all_numbers & o.all_numbers)
                and distance(m.__dict__, x["o"].__dict__) < d + slack
                and not claimed(x["o"]) and not _across_road(m, x["o"])]

    def propose(m, o, d, **evidence):
        number = o.number_for(m.region)
        if not number:
            task("local_stop_number_unknown", m, numbers=o.numbers)
            return
        if m.target["type"] != "node":
            task("platform_is_not_a_node", m, number=number)
            return
        changes.append({"osm_key": m.target["key"], "type": m.target["type"], "id": m.target["id"],
                        "version": m.target["version"], "name": m.name, "ref": number, "region": m.region,
                        "district": district(m.target)["name"], "district_code": district(m.target)["code"],
                        "lon": m.target["lon"], "lat": m.target["lat"], "tags": m.target["tags"],
                        "official": [{"provider": r["provider"], "id": r["id"], "name": r["name"],
                                      "ref": stop_ref(r.get("ref"))} for r in o.records],
                        "distance_m": round(d, 1), **evidence, "group": m})

    for m in mapped:
        if len(m.members) > 1 and m.in_scope:
            task("mapped_twice", m, numbers=sorted(m.numbers))

    # Clusters: official and OSM stops that share a name and lie within CLUSTER_M of each other.
    nodes = list(officials) + [m for m in mapped if m.norm]
    position = {id(x): i for i, x in enumerate(nodes)}
    by_name = defaultdict(list)
    for x in nodes:
        for n in (x.names if isinstance(x, Official) else {x.norm}):
            by_name[n].append(x)
    sets = _Sets(range(len(nodes)))
    for n, items in by_name.items():
        if len(items) < 2:
            continue
        grid = Grid([{"lon": x.lon, "lat": x.lat, "x": x} for x in items])
        for x in items:
            for y in grid.nearby({"lon": x.lon, "lat": x.lat}):
                if y["x"] is not x and distance(x.__dict__, y) <= CLUSTER_M:
                    sets.union(position[id(x)], position[id(y["x"])])
    unmatched_names = []
    for part in sets.groups():
        cluster_o = [nodes[i] for i in part if isinstance(nodes[i], Official)]
        cluster_m = [nodes[i] for i in part if isinstance(nodes[i], Mapped)]
        open_m = [m for m in cluster_m if not m.numbers and m.in_scope]
        if not open_m:
            continue
        if not cluster_o:
            unmatched_names.extend(open_m)
            continue
        numbered = [m for m in cluster_m if m.numbers]
        taken = {o.key for o in cluster_o if any(o.all_numbers & m.numbers for m in numbered)}
        unknown = [m for m in numbered if not any(o.all_numbers & m.numbers for o in cluster_o)]
        if unknown:
            for m in open_m:
                task("neighbour_number_not_in_official_data", m,
                     neighbour_numbers=sorted(set().union(*(u.numbers for u in unknown))))
            continue
        # If the nearest official stop already has its number on another OSM stop, this is most
        # likely the same stop mapped twice; giving it the next number would be wrong.
        remaining = []
        for m in open_m:
            named = [o for o in cluster_o if m.norm in o.names]
            nearest = min(named, key=lambda o: distance(m.__dict__, o.__dict__)) if named else None
            if nearest is not None and nearest.key in taken:
                task("same_stop_as_numbered_neighbour", m, number=sorted(nearest.all_numbers),
                     distance_m=round(distance(m.__dict__, nearest.__dict__), 1))
            else:
                remaining.append(m)
        open_m = remaining
        free = [o for o in cluster_o if o.key not in taken]
        if not open_m:
            continue
        if not free:
            for m in open_m:
                task("no_official_partner", m)
            continue
        if len(open_m) > MAX_SIDE or len(free) > MAX_SIDE:
            for m in open_m:
                task("too_many_same_name_stops", m, official_count=len(cluster_o))
            continue
        decided = _decide(open_m, free, cluster_m, cluster_o)
        for i, m in enumerate(open_m):
            if i not in decided:
                task("no_official_partner", m)
                continue
            j, d, gap, side, two_sided, others_ruled_out = decided[i]
            o = free[j]
            if side == -1:
                task("side_of_road_disagrees", m, official=sorted(o.all_numbers), distance_m=round(d, 1))
                continue
            confirmed = side == 1 and others_ruled_out
            if confirmed:
                clear = d <= SIDE_MATCH_M
            elif two_sided:
                clear = d <= MATCH_M and gap >= CLEAR_MARGIN_M
            else:
                clear = d <= MATCH_M and gap >= MARGIN_M
            if not clear:
                task("unclear_pairing", m, official=sorted(o.all_numbers), distance_m=round(d, 1),
                     margin_m=None if gap == math.inf else round(gap, 1), side=side)
                continue
            if rival := rivals(m, o, d, MARGIN_M):
                task("other_official_stop_as_close", m, official=sorted(o.all_numbers),
                     rivals=[{"name": r.name, "numbers": sorted(r.all_numbers)} for r in rival])
                continue
            propose(m, o, d, method="same_name",
                    side_of_road="confirmed" if confirmed else "undecided" if two_sided else "not needed",
                    margin_m=None if gap == math.inf else round(gap, 1), same_name_official_stops=len(cluster_o))

    # OSM names that official data spells differently, such as 서대문우체국앞 for 서대문우체국.
    # Only used where a single stop of that name exists nearby, so no side of the road is in question.
    for m in unmatched_names:
        near = [(distance(m.__dict__, x["o"].__dict__), x["o"]) for x in official_grid.nearby(m.__dict__)]
        similar = sorted(((d, o) for d, o in near if d <= CLUSTER_M and any(similar_names(m.norm, n) for n in o.names)),
                         key=lambda x: x[0])
        if not similar or similar[0][0] > SIMILAR_MATCH_M or claimed(similar[0][1]):
            task("no_official_stop_with_this_name", m)
            continue
        d, o = similar[0]
        others = [x for dx, x in near if x is not o and not (x.all_numbers & o.all_numbers)
                  and dx < d + CLEAR_MARGIN_M and not claimed(x) and not _across_road(m, x)]
        same_name_elsewhere = [x for _, x in similar[1:] if not (x.all_numbers & o.all_numbers)]
        if others or same_name_elsewhere:
            task("similar_name_unclear", m, official=sorted(o.all_numbers), official_name=o.name, distance_m=round(d, 1))
            continue
        propose(m, o, d, method="similar_name", official_name=o.name, side_of_road="not needed",
                margin_m=None, same_name_official_stops=0)

    # A number must not end up twice nearby, whether already mapped or assigned now.
    final = []
    assigned = defaultdict(list)
    for c in changes:
        assigned[c["ref"]].append(c)
    for c in changes:
        m = c.pop("group")
        clash = [x for x in by_number.get(c["ref"], []) if distance(c, x.__dict__) <= NUMBER_RADIUS_M]
        twice = [x for x in assigned[c["ref"]] if x is not c and distance(c, x) <= NUMBER_RADIUS_M]
        if clash:
            task("number_already_on_nearby_stop", m, number=c["ref"], other=[x.key for x in clash])
        elif twice:
            task("number_would_be_used_twice", m, number=c["ref"])
        else:
            final.append(c)
    for t in tasks:
        t["district"] = district(t)["name"]
    return {"changes": sorted(final, key=lambda c: (c["district_code"], c["osm_key"])), "tasks": tasks,
            "summary": {"changes": len(final),
                        "by_district": dict(sorted(Counter(c["district"] for c in final).items())),
                        "by_evidence": dict(Counter(f"{c['method']}/{c['side_of_road']}" for c in final)),
                        "tasks": dict(Counter(t["kind"] for t in tasks))}}


def osm_change(changes: list[dict]) -> bytes:
    """An osmChange document that adds ref to each planned node, for review in JOSM.

    It is built from the extract the plan used; an upload must fetch each node again first."""
    root = ET.Element("osmChange", version="0.6", generator="osm-korea-bus-sync")
    modify = ET.SubElement(root, "modify")
    for c in sorted(changes, key=lambda c: c["id"]):
        node = ET.SubElement(modify, "node", id=str(c["id"]), version=str(c["version"]),
                             lat=f"{c['lat']:.7f}", lon=f"{c['lon']:.7f}")
        for key, value in sorted({**c["tags"], "ref": c["ref"]}.items()):
            ET.SubElement(node, "tag", k=key, v=str(value))
    ET.indent(root)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def tasks_geojson(tasks: list[dict]) -> dict:
    """Mapper tasks as GeoJSON points (one per OSM stop), ready for a MapRoulette challenge."""
    features = []
    for t in tasks:
        detail = {k: v for k, v in t.items() if k not in {"lon", "lat", "kind", "osm_keys", "name", "district"}}
        features.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [t["lon"], t["lat"]]},
                         "properties": {"kind": t["kind"], "instruction": TASK_INSTRUCTIONS[t["kind"]],
                                        "name": t["name"], "district": t.get("district", ""),
                                        "osm": ";".join(t["osm_keys"]),
                                        "detail": json.dumps(detail, ensure_ascii=False) if detail else ""}})
    return {"type": "FeatureCollection", "features": features}


def self_check(sources: dict, inventory: dict, fraction: float = 0.5, seed: int = 0) -> dict:
    """Hide the numbers of some mapped stops, plan again and compare with what mappers entered.

    A disagreement where the mapped number belongs to no official stop within 60 m usually means
    the mapped number is outdated; those are counted apart from real disagreements."""
    import random
    rnd = random.Random(seed)
    stops, truth = [], {}
    for s in inventory["stops"]:
        numbers = numbers_in(s["tags"])
        if len(numbers) == 1 and _is_platform(s) and s.get("in_scope", True) and rnd.random() < fraction:
            truth[s["key"]] = next(iter(numbers))
            s = {**s, "tags": {k: v for k, v in s["tags"].items() if not (k == "ref" or k.startswith("ref:"))}}
        stops.append(s)
    plan = plan_refs(sources, {**inventory, "stops": stops})
    records = Grid([r for r in sources["stops"] if r.get("lon") is not None])
    result = {"hidden": len(truth), "assigned": 0, "agree": 0, "disagree": 0, "mapped_number_not_official_here": 0}
    examples = []
    for c in plan["changes"]:
        if c["osm_key"] not in truth:
            continue
        result["assigned"] += 1
        if c["ref"] == truth[c["osm_key"]]:
            result["agree"] += 1
        elif any(stop_ref(r.get("ref")) == truth[c["osm_key"]] and distance(c, r) <= RECORD_MERGE_M for r in records.nearby(c)):
            result["disagree"] += 1
            if len(examples) < 20:
                examples.append({"osm_key": c["osm_key"], "name": c["name"], "mapped": truth[c["osm_key"]], "planned": c["ref"]})
        else:
            result["mapped_number_not_official_here"] += 1
    decided = result["agree"] + result["disagree"]
    return {**result, "agreement": round(result["agree"] / decided, 4) if decided else None, "disagreements": examples}
