import json

import gzip

import pytest

from bus_sync.control import ControlClient, ControlError
from bus_sync.rollback import RollbackUnavailable
from bus_sync.runner import control_poll, scheduled


class FakeControl:
    version = "abc123"

    def __init__(self, stopped=False, jobs=(), fail=False):
        self.stopped, self.jobs, self.fail = stopped, list(jobs), fail
        self.runs, self.job_updates, self.uploads = [], [], []

    def control(self):
        if self.fail:
            raise ControlError("unreachable")
        jobs = [j for j in self.jobs if not any(u[0] == j["id"] for u in self.job_updates)]
        return {"stopped": self.stopped, "reason": "테스트 정지", "rollback_jobs": jobs}

    def update_run(self, run_id, **fields):
        self.runs.append(fields)

    def update_job(self, job_id, status, result=None, revert_changeset=None):
        self.job_updates.append((job_id, status, result))

    def upload(self, run_id, path):
        self.uploads.append(path.name)


def unavailable():
    raise RollbackUnavailable("봇 계정 미설정")


def test_unreadable_stop_switch_means_nothing_runs(tmp_path):
    calls = []
    assert scheduled(tmp_path, tmp_path / "out", tmp_path / "e.json", client=FakeControl(fail=True), compare_main=calls.append) == 1
    assert calls == []


def test_stopped_switch_skips_comparison_but_still_processes_rollbacks(tmp_path):
    client = FakeControl(stopped=True, jobs=[{"id": 3, "changeset_id": 77}])
    calls = []
    assert scheduled(tmp_path, tmp_path / "out", tmp_path / "e.json", client=client, compare_main=calls.append, api_factory=unavailable) == 0
    assert calls == []
    assert [u[:2] for u in client.job_updates] == [(3, "running"), (3, "failed")]
    assert "봇 계정 미설정" in client.job_updates[1][2]["error"]
    assert client.runs[-1]["status"] == "skipped_stopped"


def test_completed_run_uploads_reports_and_summary(tmp_path):
    out = tmp_path / "out"
    client = FakeControl()

    def fake_compare(argv):
        out.mkdir()
        (out / "status.json").write_text(json.dumps({"status": "completed_with_warnings"}))
        (out / "report.html").write_text("<html>")
        (out / "report-data.json").write_text("{}")
        (out / "comparison.json").write_text(json.dumps({
            "counts": {"stops": {"osm:matched": 5, "osm:osm_unmatched": 2, "source:source_unmatched": 4}, "routes": {}},
            "osm_snapshot_at": "2026-10-30T20:00:00Z", "warnings": ["a", "b"],
            "source_snapshots": {"seoul": {"dates": {"stops": "2026-09-02"}}}, "exceptions": [{"review_due": True}]}))
        return 0

    assert scheduled(tmp_path, out, tmp_path / "e.json", client=client, compare_main=fake_compare) == 0
    assert [r["status"] for r in client.runs] == ["running", "completed_with_warnings"]
    summary = client.runs[-1]["summary"]
    assert summary["headline"]["OSM 정류소 대응 미확인"] == 2 and summary["warning_count"] == 2
    assert summary["osm_writes"] == "disabled" and summary["exceptions_review_due"] == 1
    # report.html stays local; the dashboard renders report-data.json instead.
    assert set(client.uploads) == {"status.json", "report-data.json", "comparison.json"}


class FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code, self.body, self.text = status_code, body or {}, ""

    def json(self):
        return self.body


class FakeSession:
    def __init__(self, put_status=200, grant_url="https://store.blob.test/runs/x.gz?sig=1"):
        self.calls, self.put_status, self.grant_url = [], put_status, grant_url

    def request(self, method, url, headers=None, timeout=None, **kwargs):
        self.calls.append((method, url, headers, kwargs.get("json")))
        if url.endswith("/uploads"):
            return FakeResponse(body={"url": self.grant_url, "content_type": "application/gzip"})
        return FakeResponse(body={"ok": True})

    def put(self, url, data=None, headers=None, timeout=None):
        self.calls.append(("PUT", url, headers, gzip.decompress(data.read())))
        return FakeResponse(self.put_status)


def test_upload_presigns_puts_gzip_without_runner_token_and_records(tmp_path):
    path = tmp_path / "summary.md"
    path.write_text("요약")
    session = FakeSession()
    ControlClient("https://bus.test", "secret", session=session).upload("20261031T041700Z", path)
    (m1, u1, _, b1), (m2, u2, h2, body), (m3, u3, _, b3) = session.calls
    size = b1["size"]
    assert (m1, u1, b1) == ("POST", "https://bus.test/api/runner/runs/20261031T041700Z/uploads", {"name": "summary.md", "size": size})
    assert (m2, u2, body) == ("PUT", session.grant_url, "요약".encode())
    assert h2["Content-Type"] == "application/gzip" and "Authorization" not in h2 and h2["Content-Length"] == str(size)
    assert (m3, u3, b3) == ("POST", "https://bus.test/api/runner/runs/20261031T041700Z/files", {"name": "summary.md", "size": size})


@pytest.mark.parametrize("session", [FakeSession(put_status=403), FakeSession(grant_url="http://plain.test/x")])
def test_failed_or_insecure_upload_is_not_recorded(tmp_path, session):
    path = tmp_path / "summary.md"
    path.write_text("요약")
    with pytest.raises(ControlError):
        ControlClient("https://bus.test", "secret", session=session).upload("20261031T041700Z", path)
    assert not any(call[1].endswith("/files") for call in session.calls)


def test_failed_comparison_is_reported_as_failed(tmp_path):
    out = tmp_path / "out"
    client = FakeControl()

    def failing(argv):
        out.mkdir()
        (out / "status.json").write_text(json.dumps({"status": "failed", "error": "GBIS unavailable"}))
        return 1

    assert scheduled(tmp_path, out, tmp_path / "e.json", client=client, compare_main=failing) == 1
    assert client.runs[-1]["status"] == "failed" and client.runs[-1]["summary"]["error"] == "GBIS unavailable"


def test_control_poll_marks_conflicting_rollbacks_for_manual_review():
    from tests.test_rollback import FakeApi, node

    history = {"node/1": [node(1, 1), node(1, 2), node(1, 3)]}
    api = FakeApi([("modify", node(1, 2))], history)
    client = FakeControl(jobs=[{"id": 8, "changeset_id": 100}])
    assert control_poll(client, api_factory=lambda: (api, 42)) == 1
    assert [u[:2] for u in client.job_updates] == [(8, "running"), (8, "needs_manual")]
    assert api.uploaded is None
