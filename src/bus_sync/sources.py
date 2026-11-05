"""Acquire and normalize the public Seoul and GBIS bulk files.

Only a successful, fully parsed acquisition replaces the local manifest.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import re
import tempfile
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from bs4 import BeautifulSoup
from openpyxl import load_workbook

from .http import EmptyDownload


SEOUL_PAGES = {
    "stops": ("OA-15067", "A", "서울시버스정류소위치정보"),
    "routes": ("OA-15066", "F", "서울시버스노선기본정보"),
    "route_stations": ("OA-1095", "S", "서울시버스노선별정류소정보"),
}
SEOUL_DOWNLOAD = "https://datafile.seoul.go.kr/bigfile/iot/inf/nio_download.do?useCache=false"
GBIS_TEST_PAGE = "https://www.gbis.go.kr/gbis2014/publicService.action?cmd=tBaseInfo"
GBIS_TEST_API = "https://www.gbis.go.kr/gbis2014/openAPI.action"
GBIS_KINDS = {"stops": "station", "routes": "route", "route_stations": "routeStation"}
# GBIS announces each day's files at midnight and fills them later, and now and then
# lists new routes' stop sequences a day before the routes themselves. When a day's files
# are still empty or disagree, the whole GBIS set is taken from the latest complete day.
GBIS_EARLIER_DAYS = 3
HOSTS = {"data.seoul.go.kr", "datafile.seoul.go.kr", "www.gbis.go.kr", "gbis.go.kr", "openapi.gbis.go.kr"}


class SourceError(ValueError):
    """An upstream snapshot cannot safely be interpreted as a complete file."""


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_bytes(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def _atomic_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _fetch(url: str, destination: Path, *, method: str = "GET", data: dict | None = None) -> dict:
    from .http import fetch
    return fetch(url, destination, method=method, data=data, allowed_hosts=HOSTS)


def _date(value: str) -> str:
    try:
        return datetime.strptime(value, "%Y%m%d").date().isoformat()
    except ValueError as exc:
        raise SourceError(f"Invalid upstream snapshot date: {value!r}") from exc


def _earlier_gbis(item: dict, days: int) -> dict:
    """The same GBIS file as announced, from an earlier day; GBIS keeps them under the same name pattern."""
    if not days:
        return item
    day = datetime.strptime(item["snapshot_date"], "%Y-%m-%d").date() - timedelta(days=days)
    filename = item["upstream_filename"].replace(item["snapshot_date"].replace("-", ""), day.strftime("%Y%m%d"), 1)
    return {**item, "snapshot_date": day.isoformat(), "upstream_filename": filename,
            "url": f"http://openapi.gbis.go.kr/ws/download?{filename}",
            "announced_filename": item["upstream_filename"]}


def _discover_seoul(html: str, kind: str) -> dict:
    expected_id, _, filename_prefix = SEOUL_PAGES[kind]
    soup = BeautifulSoup(html, "html.parser")
    form = soup.find("form", attrs={"name": "frmFile"})
    if form is None:
        raise SourceError(f"Seoul {kind}: download form missing")
    values = {x.get("name"): x.get("value", "") for x in form.find_all("input")}
    if values.get("infId") != expected_id or not str(values.get("infSeq", "")).isdigit():
        raise SourceError(f"Seoul {kind}: unexpected download form")
    candidates = []
    for element in soup.find_all(["span", "a"]):
        title = element.get("title", "")
        match = re.fullmatch(re.escape(filename_prefix) + r"\((\d{8})\)\.xlsx", title)
        action = element.get("onclick", "") or element.get("href", "")
        sequence = re.search(r"downloadFile\(\s*['\"](\d+)['\"]\s*\)", action)
        if match and sequence:
            candidates.append((_date(match[1]), title, sequence[1]))
    if not candidates:
        raise SourceError(f"Seoul {kind}: no dated XLSX download found")
    date, filename, sequence = max(candidates)
    return {"provider": "seoul", "kind": kind, "snapshot_date": date,
            "upstream_filename": filename, "url": SEOUL_DOWNLOAD, "method": "POST",
            "data": {"infId": expected_id, "infSeq": values["infSeq"], "seq": sequence},
            "license": "KOGL Type 1 (attribution; commercial use and modification allowed)"}


def _discover_gbis_test(html: str) -> str:
    # Read the value from the official page, never substitute a private key or
    # silently invent today's version/file name when discovery stops working.
    match = re.search(r"input\[name=serviceKey\].*?\.val\(\s*['\"]([^'\"]+)['\"]\s*\)", html, re.S)
    if not match or "openAPI.action?cmd=getBaseInfo&serviceKey=" not in html:
        raise SourceError("GBIS public test page changed: test request/value missing")
    key = match[1]
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", key):
        raise SourceError("GBIS public test page has an unexpected test value")
    return GBIS_TEST_API + "?" + urlencode({"cmd": "getBaseInfo", "serviceKey": key, "format": "json"})


def _discover_gbis(body: str) -> tuple[list[dict], dict]:
    try:
        response = json.loads(body)["response"]
        if int(response["msgHeader"]["resultCode"]) != 0:
            raise SourceError("GBIS discovery returned an upstream error")
        item = response["msgBody"]["baseInfoItem"]
        result = []
        for kind, prefix in GBIS_KINDS.items():
            url, version = str(item[prefix + "DownloadUrl"]), str(item[prefix + "Version"])
            parsed = urlsplit(url)
            if parsed.scheme not in {"http", "https"} or parsed.hostname != "openapi.gbis.go.kr" or parsed.path != "/ws/download":
                raise SourceError("GBIS returned an unexpected download host/path")
            filename = parsed.query
            expected_prefix = {"stops": "station", "routes": "route", "route_stations": "routestation"}[kind]
            if filename != f"{expected_prefix}{version}V2.txt":
                raise SourceError(f"GBIS {kind}: download name/version mismatch")
            result.append({"provider": "gbis", "kind": kind, "snapshot_date": _date(version),
                           "upstream_filename": filename, "url": url, "method": "GET", "data": None,
                           "license": "Unrestricted use (data.go.kr dataset 15080658)"})
        shape = {"url": item.get("routeLineDownloadUrl"), "version": item.get("routeLineVersion"),
                 "downloaded": False, "reason": "Route geometry is not compared."}
        return result, shape
    except (KeyError, TypeError, json.JSONDecodeError, ValueError) as exc:
        if isinstance(exc, SourceError):
            raise
        raise SourceError("GBIS discovery response has an invalid schema") from exc


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _ref(value) -> str:
    value = _text(value)
    if not value or re.fullmatch(r"0+", value):
        return ""
    return value.zfill(5) if value.isdigit() and len(value) <= 5 else value


def _xlsx(path: Path, required: set[str]) -> list[dict]:
    try:
        workbook = load_workbook(path, read_only=True, data_only=True)
        try:
            rows = iter(workbook.active.values)
            headers = [_text(x) for x in next(rows)]
            if not required.issubset(headers) or len(headers) != len(set(headers)):
                raise SourceError(f"{path.name}: required/unique XLSX columns missing")
            result = []
            for row in rows:
                if any(x is not None for x in row):
                    result.append(dict(zip(headers, (_text(x) for x in row))))
        finally:
            workbook.close()
    except SourceError:
        raise
    except Exception as exc:
        raise SourceError(f"{path.name}: cannot read XLSX") from exc
    if not result:
        raise SourceError(f"{path.name}: empty data file")
    return result


def _gbis_rows(path: Path, required: set[str]) -> list[dict]:
    records = path.read_text(encoding="utf-8-sig").strip().split("^")
    rows = csv.reader(records, delimiter="|", quoting=csv.QUOTE_NONE)
    headers = next(rows, [])
    if not required.issubset(headers) or len(headers) != len(set(headers)):
        raise SourceError(f"{path.name}: required/unique GBIS columns missing")
    result = []
    for row in rows:
        if not row or row == [""]:
            continue
        if len(row) != len(headers):
            raise SourceError(f"{path.name}: truncated or malformed GBIS row")
        result.append(dict(zip(headers, (x.strip() for x in row))))
    if not result:
        raise SourceError(f"{path.name}: empty data file")
    return result


def _gbis_disagreement(paths: dict[str, Path]) -> str | None:
    """Why one day's GBIS files cannot be used together, if they cannot."""
    master = {row["routeId"] for row in _gbis_rows(paths["routes"], {"routeId"})}
    missing = {row["routeId"] for row in _gbis_rows(paths["route_stations"], {"routeId"})} - master
    if not missing:
        return None
    return f"{len(missing)} {'route' if len(missing) == 1 else 'routes'} in stop sequences missing from the route master"


def _coordinates(lon: str, lat: str, label: str) -> tuple[float, float]:
    try:
        x, y = float(lon), float(lat)
    except (ValueError, TypeError) as exc:
        raise SourceError(f"{label}: invalid coordinates") from exc
    if not math.isfinite(x) or not math.isfinite(y) or not (120 <= x <= 135 and 30 <= y <= 44):
        raise SourceError(f"{label}: expected Korean WGS84 longitude/latitude")
    return x, y


def _unique(rows: list[dict], field: str, label: str) -> None:
    ids = [_text(row.get(field)) for row in rows]
    if not all(ids) or len(ids) != len(set(ids)):
        raise SourceError(f"{label}: blank or duplicate {field}")


def _ordered(rows: list[dict], order: str, stop: str, known: set[str], label: str, warnings: list[str]) -> tuple[list[str], bool]:
    try:
        positions = [int(row[order]) for row in rows]
    except (ValueError, KeyError) as exc:
        raise SourceError(f"{label}: invalid stop sequence") from exc
    if len(positions) != len(set(positions)):
        raise SourceError(f"{label}: duplicate stop sequence positions")
    ordered = [row for _, row in sorted(zip(positions, rows), key=lambda pair: pair[0])]
    ids = [row[stop] for row in ordered]
    complete = bool(ids) and sorted(positions) == list(range(1, len(ids) + 1)) and set(ids).issubset(known)
    if not complete:
        warnings.append(f"{label}: incomplete stop sequence or missing station reference")
    return ids, complete


def _seoul_mode(route_type: str, name: str) -> str:
    if route_type == "한강":
        return "ferry"
    if route_type in {"지선", "마을", "간선", "공항", "광역", "관광", "순환"}:
        return "bus"
    if not route_type and (re.fullmatch(r"N\d+", name) or name.startswith("심야A")):
        return "bus"
    return "unknown"


def _name_equivalent(value: str) -> str:
    return re.sub(r"[\s.·,]+", "", unicodedata.normalize("NFKC", value)).casefold()


def _supplement_seoul_stops(stops: list[dict], sequences: list[dict], warnings: list[str]) -> None:
    """Keep official cross-boundary stops present only in route membership files.

    A repeated source ID is consolidated only when the populated rows agree.
    Empty cells are not disagreement; incompatible coordinates/names are.
    """
    known = {stop["id"] for stop in stops}
    missing = defaultdict(list)
    for row in sequences:
        if row["NODE_ID"] and row["NODE_ID"] not in known:
            missing[row["NODE_ID"]].append(row)
    for ident, rows in sorted(missing.items()):
        names = [row.get("정류소명", "") for row in rows if row.get("정류소명")]
        refs = {_ref(row.get("ARS_ID")) for row in rows} - {""}
        coordinates = []
        invalid = False
        for row in rows:
            if not row.get("X좌표") or not row.get("Y좌표"):
                continue
            try:
                coordinates.append(_coordinates(row["X좌표"], row["Y좌표"], f"seoul:stop:{ident}"))
            except SourceError:
                invalid = True
        unique_coordinates = {(round(lon, 7), round(lat, 7)) for lon, lat in coordinates}
        if invalid or len(unique_coordinates) > 1 or len({_name_equivalent(name) for name in names}) > 1 or len(refs) > 1:
            warnings.append(f"seoul:stop:{ident}: conflicting route-station rows; supplemental stop withheld")
            continue
        if not coordinates or not names:
            warnings.append(f"seoul:stop:{ident}: route-station rows lack name/coordinates; supplemental stop withheld")
            continue
        lon, lat = coordinates[0]
        route_names = {row["노선명"] for row in rows}
        mode = "ferry" if all(name.startswith("한강버스(") for name in route_names) else "bus"
        stops.append({"key": f"seoul:stop:{ident}", "provider": "seoul", "id": ident,
                      "ref": next(iter(refs)) if refs else "", "name": names[0], "lon": lon, "lat": lat,
                      "region": "", "mode": mode, "origin": "route_stations"})


def _parse_seoul(paths: dict[str, Path], aligned: bool, warnings: list[str]) -> tuple[list[dict], list[dict]]:
    stations = _xlsx(paths["stops"], {"NODE_ID", "ARS_ID", "정류소명", "X좌표", "Y좌표", "정류소타입"})
    master = _xlsx(paths["routes"], {"업체명", "노선번호", "유형"})
    sequences = _xlsx(paths["route_stations"], {"ROUTE_ID", "노선명", "순번", "NODE_ID"})
    _unique(stations, "NODE_ID", "Seoul stations")
    stops = []
    known_types = {"중앙차로", "일반차로", "가로변시간", "가로변전일", "마을버스"}
    for row in stations:
        ident = row["NODE_ID"]
        lon, lat = _coordinates(row["X좌표"], row["Y좌표"], f"seoul:stop:{ident}")
        mode = "ferry" if row["정류소타입"] == "한강선착장" else "bus" if row["정류소타입"] in known_types else "unknown"
        stops.append({"key": f"seoul:stop:{ident}", "provider": "seoul", "id": ident,
                      "ref": _ref(row["ARS_ID"]), "name": row["정류소명"], "lon": lon, "lat": lat,
                      "region": "", "mode": mode, "origin": "stops"})
    _supplement_seoul_stops(stops, sequences, warnings)
    by_name, by_id = defaultdict(list), defaultdict(list)
    for row in master:
        if not row["노선번호"]:
            raise SourceError("Seoul route master has a blank route name")
        by_name[row["노선번호"]].append(row)
    for row in sequences:
        if not row["ROUTE_ID"]:
            raise SourceError("Seoul route sequence has a blank ROUTE_ID")
        by_id[row["ROUTE_ID"]].append(row)
    routes, seen_names = [], set()
    known = {stop["id"] for stop in stops}
    for ident, sequence in sorted(by_id.items()):
        names = {row["노선명"] for row in sequence}
        if len(names) != 1 or "" in names:
            raise SourceError(f"Seoul route {ident}: conflicting/blank route names")
        name = next(iter(names))
        seen_names.add(name)
        sources = by_name.get(name, [])
        types = sorted({row["유형"] for row in sources})
        route_type = ";".join(types)
        mode = _seoul_mode(route_type, name)
        ids, complete = _ordered(sequence, "순번", "NODE_ID", known, f"seoul:route:{ident}", warnings)
        if not sources:
            warnings.append(f"seoul:route:{ident}: route missing from Seoul master")
        routes.append({"key": f"seoul:route:{ident}", "provider": "seoul", "id": ident, "name": name,
                       "operator": ";".join(sorted({row["업체명"] for row in sources if row["업체명"]})),
                       "from": ";".join(sorted({row.get("기점명", "") for row in sources if row.get("기점명")})),
                       "to": ";".join(sorted({row.get("종점명", "") for row in sources if row.get("종점명")})),
                       "route_type": route_type, "mode": mode, "stop_ids": ids,
                       "sequence_complete": complete and aligned and bool(sources)})
    for name in sorted(set(by_name) - seen_names):
        sources = by_name[name]
        route_type = ";".join(sorted({row["유형"] for row in sources}))
        ident = f"name:{name}"
        routes.append({"key": f"seoul:route:{ident}", "provider": "seoul", "id": ident, "name": name,
                       "operator": ";".join(sorted({row["업체명"] for row in sources if row["업체명"]})),
                       "from": ";".join(sorted({row.get("기점명", "") for row in sources if row.get("기점명")})),
                       "to": ";".join(sorted({row.get("종점명", "") for row in sources if row.get("종점명")})),
                       "route_type": route_type, "mode": _seoul_mode(route_type, name),
                       "stop_ids": [], "sequence_complete": False})
        warnings.append(f"seoul:route:{ident}: master-only route; no ROUTE_ID or stop sequence available")
    duplicates = [name for name, count in Counter(r["name"] for r in routes).items() if count > 1]
    if duplicates:
        warnings.append(f"Seoul: {len(duplicates)} route names map to multiple route IDs")
    return stops, routes


def _parse_gbis(paths: dict[str, Path], aligned: bool, warnings: list[str]) -> tuple[list[dict], list[dict]]:
    stations = _gbis_rows(paths["stops"], {"stationId", "mobileNo", "stationName", "regionName", "x", "y"})
    master = _gbis_rows(paths["routes"], {"routeId", "routeName", "routeTypeCd", "companyName"})
    sequences = _gbis_rows(paths["route_stations"], {"routeId", "staOrder", "stationId"})
    _unique(stations, "stationId", "GBIS stations")
    _unique(master, "routeId", "GBIS routes")
    stops = []
    for row in stations:
        ident = row["stationId"]
        lon, lat = _coordinates(row["x"], row["y"], f"gbis:stop:{ident}")
        stops.append({"key": f"gbis:stop:{ident}", "provider": "gbis", "id": ident,
                      "ref": _ref(row["mobileNo"]), "name": row["stationName"], "lon": lon, "lat": lat,
                      "region": row["regionName"], "mode": "bus", "origin": "stops"})
    by_id = defaultdict(list)
    for row in sequences:
        by_id[row["routeId"]].append(row)
    extra = set(by_id) - {row["routeId"] for row in master}
    if extra:
        raise SourceError("GBIS stop sequence references routes missing from master")
    bus_types = {"11", "12", "13", "14", "15", "16", "21", "22", "23", "30", "41", "42", "43", "51", "52", "53"}
    known = {row["stationId"] for row in stations}
    routes = []
    for row in master:
        ident, kind = row["routeId"], row["routeTypeCd"]
        ids, complete = _ordered(by_id.get(ident, []), "staOrder", "stationId", known, f"gbis:route:{ident}", warnings)
        routes.append({"key": f"gbis:route:{ident}", "provider": "gbis", "id": ident, "name": row["routeName"],
                       "operator": row["companyName"], "route_type": kind,
                       "from": row.get("startStationName", ""), "to": row.get("endStationName", ""),
                       "mode": "drt" if kind == "50" else "bus" if kind in bus_types else "unknown",
                       "stop_ids": ids, "sequence_complete": complete and aligned})
    duplicates = [name for name, count in Counter(r["name"] for r in routes).items() if count > 1]
    if duplicates:
        warnings.append(f"GBIS: {len(duplicates)} route names map to multiple route IDs")
    return stops, routes


def _normalize(root: Path, files: list[dict], observed: str, shape: dict) -> dict:
    result = {"schema_version": 1, "snapshots": {}, "stops": [], "routes": [], "warnings": [], "files": files}
    for provider, parser in (("seoul", _parse_seoul), ("gbis", _parse_gbis)):
        entries = [entry for entry in files if entry["provider"] == provider]
        dates = {entry["kind"]: entry["snapshot_date"] for entry in entries}
        if set(dates) != set(SEOUL_PAGES):
            raise SourceError(f"{provider}: expected three bulk files")
        aligned = len(set(dates.values())) == 1
        if not aligned:
            result["warnings"].append(f"{provider}: mixed snapshot dates; comparison completeness downgraded: {dates}")
        stops, routes = parser({entry["kind"]: root / entry["path"] for entry in entries}, aligned, result["warnings"])
        result["stops"].extend(stops)
        result["routes"].extend(routes)
        result["snapshots"][provider] = {"dates": dates, "snapshot_date": next(iter(dates.values())) if aligned else None,
                                            "observed_at": observed, "aligned": aligned,
                                            "comparison_complete": aligned,
                                            "route_sequence_complete": aligned and all(route["sequence_complete"] for route in routes),
                                            "stop_count": len(stops), "route_count": len(routes)}
        result["snapshots"][provider]["primary_stop_count"] = sum(stop["origin"] == "stops" for stop in stops)
        result["snapshots"][provider]["supplemental_stop_count"] = sum(stop["origin"] == "route_stations" for stop in stops)
        duplicate_refs = sum(count > 1 for count in Counter(stop["ref"] for stop in stops if stop["ref"]).values())
        blank_refs = sum(not stop["ref"] for stop in stops)
        if duplicate_refs:
            result["warnings"].append(f"{provider}: {duplicate_refs} duplicated public stop numbers")
        if blank_refs:
            result["warnings"].append(f"{provider}: {blank_refs} blank/zero public stop numbers retained without a ref")
        unknown = sum(item["mode"] == "unknown" for item in [*stops, *routes])
        if unknown:
            result["warnings"].append(f"{provider}: {unknown} records have unknown transport modes")
            result["snapshots"][provider]["comparison_complete"] = False
    result["snapshots"]["gbis"]["route_geometry"] = shape
    result["snapshots"]["gbis"]["discovery"] = "public_test_api_no_personal_key"
    result["stops"].sort(key=lambda item: item["key"])
    result["routes"].sort(key=lambda item: item["key"])
    return result


def _safe_path(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()) or path == root.resolve():
        raise SourceError("Source manifest contains an unsafe path")
    return path


def load_sources(data_dir: Path) -> dict:
    """Load the last complete acquisition, verifying both raw and normalized data."""
    root = Path(data_dir) / "sources"
    try:
        manifest = json.loads((root / "manifest.json").read_text())
        if manifest["schema_version"] != 1:
            raise SourceError("Unsupported source manifest version")
        for entry in manifest["files"]:
            path = _safe_path(root, entry["path"])
            if path.stat().st_size != entry["bytes"] or _sha(path) != entry["sha256"]:
                raise SourceError(f"Cached source checksum mismatch: {entry['path']}")
        normalized = _safe_path(root, manifest["normalized_path"])
        if _sha(normalized) != manifest["normalized_sha256"]:
            raise SourceError("Cached normalized source checksum mismatch")
        result = json.loads(normalized.read_text())
        if result["schema_version"] != 1 or result["files"] != manifest["files"]:
            raise SourceError("Cached normalized source manifest mismatch")
        return result
    except SourceError:
        raise
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise SourceError("No valid cached source snapshot; run fetch first") from exc


def fetch_sources(data_dir: Path, *, refresh: bool = True) -> dict:
    """Discover current bulk versions and publish a verified local snapshot.

    refresh=False explicitly loads a verified local snapshot. Discovery failure
    during refresh never falls back to stale data or guessed date-based URLs.
    """
    if not refresh:
        return load_sources(data_dir)
    root = Path(data_dir) / "sources"
    root.mkdir(parents=True, exist_ok=True)
    manifest_path = root / "manifest.json"
    observed = datetime.now(timezone.utc).isoformat()
    with tempfile.TemporaryDirectory(prefix=".acquire-", dir=root) as temporary:
        stage = Path(temporary)
        discovered = []
        for kind, (inf_id, view, _) in SEOUL_PAGES.items():
            page = stage / f"seoul-{kind}.html"
            url = f"https://data.seoul.go.kr/dataList/{inf_id}/{view}/1/datasetView.do"
            _fetch(url, page)
            item = _discover_seoul(page.read_text(encoding="utf-8"), kind)
            item["landing_url"] = url
            discovered.append(item)
        page = stage / "gbis-test.html"
        _fetch(GBIS_TEST_PAGE, page)
        discovery_url = _discover_gbis_test(page.read_text(encoding="utf-8"))
        response = stage / "gbis-discovery.json"
        _fetch(discovery_url, response)
        items, shape = _discover_gbis(response.read_text(encoding="utf-8"))
        files = []
        notes = []

        def download(item):
            # A provider may correct bytes without changing its date or URL.
            # Refresh therefore validates all six files; offline reuse is explicit.
            downloaded = stage / f"{item['provider']}-{item['kind']}.raw"
            metadata = _fetch(item["url"], downloaded, method=item["method"], data=item["data"])
            checksum = _sha(downloaded)
            size = downloaded.stat().st_size
            if metadata.get("sha256") != checksum or metadata.get("bytes") != size:
                raise SourceError("HTTP downloader returned inconsistent source metadata")
            suffix = ".xlsx" if item["provider"] == "seoul" else ".txt"
            relative = f"raw/{item['provider']}-{item['kind']}-{item['snapshot_date']}-{checksum[:16]}{suffix}"
            destination = root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(downloaded, destination)
            return {**metadata, **item, "path": relative, "sha256": checksum, "bytes": size}

        for item in discovered:
            files.append(download(item))
        skipped = []
        for days in range(GBIS_EARLIER_DAYS + 1):
            chosen = [_earlier_gbis(item, days) for item in items]
            day = chosen[0]["snapshot_date"]
            try:
                gbis = [download(item) for item in chosen]
            except EmptyDownload:
                skipped.append(f"{day} files were still empty")
                continue
            problem = _gbis_disagreement({f["kind"]: root / f["path"] for f in gbis})
            if problem:
                skipped.append(f"{day} files disagree ({problem})")
                continue
            files.extend(gbis)
            if skipped:
                notes.append(f"GBIS {'; '.join(skipped)}; used the complete set of {day} instead")
            break
        else:
            raise SourceError(f"GBIS published no complete file set in the {GBIS_EARLIER_DAYS} days "
                              f"before {items[0]['snapshot_date']}: {'; '.join(skipped)}")
        # Parsing precedes publication. A bad new file never replaces the prior
        # manifest; any unreferenced content-addressed raw file is harmless.
        result = _normalize(root, files, observed, shape)
        result["warnings"].extend(notes)
        normalized = _json_bytes(result)
        checksum = hashlib.sha256(normalized).hexdigest()
        relative = f"normalized/{checksum}.json"
        _atomic_bytes(root / relative, normalized)
        manifest = {"schema_version": 1, "observed_at": observed, "files": files,
                    "snapshots": result["snapshots"], "warnings": result["warnings"],
                    "normalized_path": relative, "normalized_sha256": checksum}
        _atomic_bytes(manifest_path, _json_bytes(manifest))
    return result
