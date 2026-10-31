import hashlib
import json
from pathlib import Path

import pytest
from openpyxl import Workbook

from bus_sync import sources


def xlsx(path, rows):
    workbook = Workbook()
    for row in rows:
        workbook.active.append(row)
    workbook.save(path)


@pytest.fixture
def bulk_files(tmp_path):
    seoul = {kind: tmp_path / f"seoul-{kind}.xlsx" for kind in sources.SEOUL_PAGES}
    xlsx(seoul["stops"], [
        ["NODE_ID", "ARS_ID", "정류소명", "X좌표", "Y좌표", "정류소타입"],
        ["100", 1234, "First", 127.0, 37.5, "일반차로"],
        ["101", "00000", "Pier", 127.01, 37.51, "한강선착장"],
    ])
    xlsx(seoul["routes"], [
        ["업체명", "노선번호", "유형"], ["Company B", "N13", None],
        ["Company A", "N13", None], ["Tour", "TOUR01", "관광"],
        ["Ferry", "River", "한강"],
    ])
    xlsx(seoul["route_stations"], [
        ["ROUTE_ID", "노선명", "순번", "NODE_ID"],
        ["10", "N13", 2, "100"], ["10", "N13", 1, "100"],
        ["11", "River", 1, "101"], ["12", "N13", 1, "100"],
    ])
    gbis = {kind: tmp_path / f"gbis-{kind}.txt" for kind in sources.GBIS_KINDS}
    gbis["stops"].write_text(
        "stationId|mobileNo|stationName|regionName|x|y^"
        "200|00000|Unnamed number|수원|127|37.3^"
        "201|00123|First|서울|127.1|37.4^"
        "202|00123|Second|수원|127.2|37.5^")
    gbis["routes"].write_text(
        "routeId|routeName|routeTypeCd|companyName^"
        "20|1|13|Company^21|1|50|DRT company^")
    gbis["route_stations"].write_text(
        "routeId|staOrder|stationId^20|2|201^20|1|200^21|1|202^")
    return {"seoul": seoul, "gbis": gbis}


def seoul_page(kind, date="20261010", seq="77"):
    inf, _, title = sources.SEOUL_PAGES[kind]
    return (f'<form name="frmFile"><input name="infId" value="{inf}">'
            '<input name="infSeq" value="2"></form>'
            f'<span title="{title}(20260902).xlsx" onclick="downloadFile(\'58\');"></span>'
            f'<span title="{title}({date}).xlsx" onclick="downloadFile(\'{seq}\');"></span>')


def gbis_discovery(date="20261010"):
    item = {}
    for kind, prefix in sources.GBIS_KINDS.items():
        filename_prefix = {"stops": "station", "routes": "route", "route_stations": "routestation"}[kind]
        item[prefix + "DownloadUrl"] = f"http://openapi.gbis.go.kr/ws/download?{filename_prefix}{date}V2.txt"
        item[prefix + "Version"] = int(date)
    return {"response": {"msgHeader": {"resultCode": 0}, "msgBody": {"baseInfoItem": item}}}


def test_latest_seoul_file_and_actual_service_number_are_discovered():
    actual = sources._discover_seoul(seoul_page("route_stations", "20261112", "111"), "route_stations")
    assert actual["snapshot_date"] == "2026-11-12"
    assert actual["data"] == {"infId": "OA-1095", "infSeq": "2", "seq": "111"}
    with pytest.raises(sources.SourceError, match="no dated"):
        sources._discover_seoul('<form name="frmFile"><input name="infId" value="OA-1095"><input name="infSeq" value="2"></form>', "route_stations")


def test_gbis_test_value_is_read_from_the_page_not_hardcoded():
    html = "$(document).find('input[name=serviceKey]').val('new_test_42'); /gbis2014/openAPI.action?cmd=getBaseInfo&serviceKey="
    assert "serviceKey=new_test_42" in sources._discover_gbis_test(html)
    with pytest.raises(sources.SourceError):
        sources._discover_gbis_test("test page no longer exposes a test request")


def test_gbis_discovery_rejects_error_and_version_mismatch():
    body = gbis_discovery()
    body["response"]["msgHeader"]["resultCode"] = 4
    with pytest.raises(sources.SourceError):
        sources._discover_gbis(json.dumps(body))
    body = gbis_discovery()
    body["response"]["msgBody"]["baseInfoItem"]["stationVersion"] = 20261009
    with pytest.raises(sources.SourceError, match="mismatch"):
        sources._discover_gbis(json.dumps(body))


def test_seoul_preserves_operators_tourism_ferries_and_duplicate_names(bulk_files):
    warnings = []
    stops, routes = sources._parse_seoul(bulk_files["seoul"], True, warnings)
    assert stops[0]["ref"] == "01234"
    assert stops[1]["mode"] == "ferry"
    assert stops[1]["ref"] == ""
    by_id = {row["id"]: row for row in routes}
    assert by_id["10"]["operator"] == "Company A;Company B"
    assert by_id["10"]["mode"] == "bus"
    # Repeat visits to the same stop are real sequence members, not duplicates.
    assert by_id["10"]["stop_ids"] == ["100", "100"]
    assert by_id["10"]["sequence_complete"] is True
    assert by_id["12"]["name"] == by_id["10"]["name"]
    assert by_id["name:TOUR01"]["sequence_complete"] is False
    assert by_id["name:TOUR01"]["mode"] == "bus"
    assert by_id["11"]["mode"] == "ferry"
    assert any("multiple route IDs" in warning for warning in warnings)


def test_gbis_zero_refs_duplicate_names_and_drt_remain_distinct(bulk_files):
    stops, routes = sources._parse_gbis(bulk_files["gbis"], True, [])
    assert stops[0]["ref"] == ""
    assert stops[1]["ref"] == stops[2]["ref"] == "00123"
    assert len({stop["key"] for stop in stops}) == 3
    assert routes[0]["stop_ids"] == ["200", "201"]
    assert routes[0]["name"] == routes[1]["name"]
    assert routes[0]["key"] != routes[1]["key"]
    assert routes[1]["mode"] == "drt"


def test_supplemental_seoul_stop_requires_consistent_official_rows():
    stops, warnings = [], []
    common = {"NODE_ID": "cross-boundary", "ARS_ID": "1234", "정류소명": "A. B", "X좌표": "127", "Y좌표": "37.5", "노선명": "100"}
    rows = [common, {**common, "정류소명": "A·B", "X좌표": "127.0000000"},
            {**common, "정류소명": "", "X좌표": "", "Y좌표": ""}]
    sources._supplement_seoul_stops(stops, rows, warnings)
    assert len(stops) == 1
    assert stops[0]["origin"] == "route_stations"
    assert stops[0]["ref"] == "01234"
    assert not warnings
    for changed in [{"정류소명": "Different"}, {"X좌표": "127.1"}, {"ARS_ID": "56789"}]:
        result, warnings = [], []
        sources._supplement_seoul_stops(result, [common, {**common, **changed}], warnings)
        assert not result
        assert "conflicting" in warnings[0]


def test_missing_supplemental_coordinates_preserve_incomplete_route():
    result, warnings = [], []
    row = {"NODE_ID": "missing", "정류소명": "Name", "노선명": "100", "X좌표": "", "Y좌표": "37.5"}
    sources._supplement_seoul_stops(result, [row], warnings)
    assert not result
    assert "lack name/coordinates" in warnings[0]


def test_mixed_snapshot_dates_downgrade_completeness(bulk_files, tmp_path):
    files = []
    for provider, paths in bulk_files.items():
        for kind, path in paths.items():
            date = "2026-10-09" if provider == "seoul" and kind == "stops" else "2026-10-10"
            files.append({"provider": provider, "kind": kind, "snapshot_date": date, "path": path.name})
    actual = sources._normalize(tmp_path, files, "now", {})
    assert actual["snapshots"]["seoul"]["comparison_complete"] is False
    assert actual["snapshots"]["seoul"]["snapshot_date"] is None
    assert all(not route["sequence_complete"] for route in actual["routes"] if route["provider"] == "seoul")
    assert any("duplicated public stop" in warning for warning in actual["warnings"])


def test_duplicate_ids_and_truncated_rows_fail_closed(bulk_files):
    path = bulk_files["gbis"]["stops"]
    path.write_text(path.read_text() + "200|12345|Duplicate|수원|127|37.3^")
    with pytest.raises(sources.SourceError, match="duplicate"):
        sources._parse_gbis(bulk_files["gbis"], True, [])
    path.write_text("stationId|mobileNo|stationName|regionName|x|y^200|12")
    with pytest.raises(sources.SourceError, match="truncated"):
        sources._parse_gbis(bulk_files["gbis"], True, [])


def test_missing_sequence_position_or_station_is_incomplete():
    warnings = []
    ids, complete = sources._ordered([{"order": "1", "stop": "known"}, {"order": "3", "stop": "absent"}],
                                     "order", "stop", {"known"}, "route", warnings)
    assert ids == ["known", "absent"]
    assert not complete
    with pytest.raises(sources.SourceError, match="duplicate"):
        sources._ordered([{"order": "1", "stop": "known"}] * 2, "order", "stop", {"known"}, "route", [])


def test_projected_or_nonfinite_coordinates_are_rejected():
    for lon, lat in [("953000", "1950000"), ("nan", "37"), ("127", "inf")]:
        with pytest.raises(sources.SourceError):
            sources._coordinates(lon, lat, "station")


def install_fake_fetch(monkeypatch, bulk_files):
    calls = []

    def fetch(url, destination, *, method="GET", data=None):
        calls.append((url, method))
        if "datasetView" in url:
            kind = next(kind for kind, (inf, _, _) in sources.SEOUL_PAGES.items() if inf in url)
            content = seoul_page(kind).encode()
        elif url == sources.GBIS_TEST_PAGE:
            content = b"$(document).find('input[name=serviceKey]').val('public_test'); /gbis2014/openAPI.action?cmd=getBaseInfo&serviceKey="
        elif "openAPI.action" in url:
            content = json.dumps(gbis_discovery()).encode()
        elif method == "POST":
            kind = next(kind for kind, (inf, _, _) in sources.SEOUL_PAGES.items() if data["infId"] == inf)
            content = bulk_files["seoul"][kind].read_bytes()
        else:
            kind = "route_stations" if "routestation" in url else "stops" if "station" in url else "routes"
            content = bulk_files["gbis"][kind].read_bytes()
        destination.write_bytes(content)
        return {"url": url, "sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content), "last_modified": None}

    monkeypatch.setattr(sources, "_fetch", fetch)
    return calls


def test_acquisition_cache_integrity_and_failed_refresh_preserve_manifest(monkeypatch, bulk_files, tmp_path):
    calls = install_fake_fetch(monkeypatch, bulk_files)
    data = tmp_path / "cache"
    initial = sources.fetch_sources(data)
    assert sources.load_sources(data) == initial
    assert len(initial["files"]) == 6
    assert len(calls) == 11  # Three landing pages, test page, discovery, six files.
    calls.clear()
    sources.fetch_sources(data)
    assert len(calls) == 11  # Same-version upstream corrections must be detected.
    manifest = data / "sources" / "manifest.json"
    before = manifest.read_bytes()

    def unavailable(*args, **kwargs):
        raise RuntimeError("upstream unavailable")

    monkeypatch.setattr(sources, "_fetch", unavailable)
    with pytest.raises(RuntimeError, match="unavailable"):
        sources.fetch_sources(data)
    assert manifest.read_bytes() == before
    assert sources.fetch_sources(data, refresh=False)["stops"] == initial["stops"]
    raw = data / "sources" / initial["files"][0]["path"]
    raw.write_bytes(b"corrupt")
    with pytest.raises(sources.SourceError, match="checksum"):
        sources.load_sources(data)


def test_invalid_new_workbook_does_not_publish_a_partial_snapshot(monkeypatch, bulk_files, tmp_path):
    install_fake_fetch(monkeypatch, bulk_files)
    data = tmp_path / "cache"
    first = sources.fetch_sources(data)
    manifest = data / "sources" / "manifest.json"
    before = manifest.read_bytes()
    discover = sources._discover_seoul

    def new_download(html, kind):
        found = discover(html, kind)
        found["data"]["seq"] = "999"  # New upstream revision must be downloaded.
        return found

    monkeypatch.setattr(sources, "_discover_seoul", new_download)
    bulk_files["seoul"]["stops"].write_bytes(b"<html>upstream maintenance</html>")
    with pytest.raises(sources.SourceError, match="cannot read XLSX"):
        sources.fetch_sources(data)
    assert manifest.read_bytes() == before
    assert sources.load_sources(data) == first
