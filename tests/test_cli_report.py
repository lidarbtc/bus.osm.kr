import csv
import json
from pathlib import Path

import pytest

from bus_sync import cli
from bus_sync.compare import compare
from bus_sync.osm import read_osm
from bus_sync.report import write_report

FIXTURE = Path(__file__).parent / "fixtures/osm_regions.osm"


def sources():
    return {"schema_version": 1,
            "snapshots": {p: {"dates": {"stops": "2026-10-09"}, "stop_count": 1, "route_count": 0}
                          for p in ["seoul", "gbis"]},
            "stops": [{"key": "seoul:stop:1", "provider": "seoul", "id": "1", "name": "Inside Seoul",
                       "ref": "12345", "lat": 1.0, "lon": 1.0, "mode": "bus"}],
            "routes": [], "warnings": [], "files": []}


def empty_exceptions(path):
    path.write_text(json.dumps({"schema_version": 1,
                               "defaults": {"unmatched_osm_action": "preserve", "expired_exception_action": "preserve_and_review"},
                               "exceptions": []}))


def test_offline_cli_reads_real_osm_and_emits_all_reports(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "load_sources", lambda data: sources())
    rules = tmp_path / "exceptions.json"
    empty_exceptions(rules)
    output = tmp_path / "report"
    code = cli.main(["compare", "--data-dir", str(tmp_path / "data"), "--osm-file", str(FIXTURE),
                     "--output-dir", str(output), "--exceptions", str(rules)])
    assert code == 0
    result = json.loads((output / "comparison.json").read_text())
    assert result["input_mode"] == "explicit_cached_snapshot"
    assert result["read_only"] is True
    assert result["osm_origin"]["coverage"] == "user_supplied_not_independently_guaranteed"
    assert any(r["key"] == "relation/501" for r in result["routes"])
    assert set(cli.REPORT_FILES) == {p.name for p in output.iterdir()}
    assert len(json.loads((output / "stops.geojson").read_text())["features"]) > 0


def test_source_failure_removes_prior_success_reports(monkeypatch, tmp_path):
    def fail(*args):
        raise RuntimeError("GBIS unavailable")
    monkeypatch.setattr(cli, "fetch_sources", fail)
    rules = tmp_path / "exceptions.json"
    empty_exceptions(rules)
    output = tmp_path / "report"
    output.mkdir()
    (output / "comparison.json").write_text('{"old":"success"}')
    (output / "stops.csv").write_text("old data")
    (output / "unrelated.txt").write_text("keep user file")
    assert cli.main(["run", "--output-dir", str(output), "--exceptions", str(rules)]) == 1
    assert not (output / "comparison.json").exists()
    assert not (output / "stops.csv").exists()
    assert (output / "unrelated.txt").exists()
    status = json.loads((output / "status.json").read_text())
    assert status["status"] == "failed" and not status["comparison_completed"]


def test_reports_escape_untrusted_names_and_spreadsheet_formulas(tmp_path):
    inventory = read_osm(FIXTURE)
    inventory["stops"][0]["tags"]["name"] = '</script><script>alert("x")</script>'
    src = sources()
    src["stops"][0]["name"] = '=HYPERLINK("bad")'
    result = compare(src, inventory, {"exceptions": []})
    result["generated_at"] = "2026-10-10T00:00:00Z"
    write_report(tmp_path, result, inventory["boundaries"])
    page = (tmp_path / "report.html").read_text()
    assert '</script><script>alert("x")</script>' not in page
    assert "\\u003c/script>" in page
    with (tmp_path / "stops.csv").open(encoding="utf-8-sig") as stream:
        rows = list(csv.DictReader(stream))
    assert next(r for r in rows if r["key"] == "seoul:stop:1")["name"].startswith("'=")


def test_upload_option_is_not_supported():
    with pytest.raises(SystemExit) as exc:
        cli.main(["run", "--upload"])
    assert exc.value.code == 2
