"""Scheduled entry points on the Oracle server.

Every run asks the dashboard for the emergency-stop switch first and does not
run when the switch is on or cannot be read. Rollback jobs run even while
stopped, because stopping is what a rollback request does.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from .control import ControlClient, ControlError
from .report import UPLOAD_FILES
from .rollback import (OsmApi, RollbackConflict, RollbackRefused, RollbackUnavailable, execute_revert,
                       plan_revert)

log = logging.getLogger("bus-sync.runner")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def process_rollbacks(client: ControlClient, jobs: list[dict], api_factory=OsmApi.from_env) -> int:
    """Returns the number of jobs that did not complete."""
    unfinished = 0
    for job in jobs:
        job_id, changeset_id = int(job["id"]), int(job["changeset_id"])
        try:
            client.update_job(job_id, "running")
        except ControlError as exc:
            # Another timer may have claimed it first; the Worker allows one claim.
            log.warning("could not claim rollback job %s: %s", job_id, exc)
            continue
        try:
            api, bot_uid = api_factory()
            plan = plan_revert(api, changeset_id, bot_uid)
            if plan.conflicts:
                unfinished += 1
                client.update_job(job_id, "needs_manual", result=plan.as_dict())
                continue
            comment = f"Revert changeset {changeset_id} (rollback #{job_id} requested by {job.get('requested_by_name', '?')})"
            new_id = execute_revert(api, plan, comment)
            client.update_job(job_id, "completed", result={**plan.as_dict(), "revert_changeset": new_id},
                              revert_changeset={"id": new_id, "comment": comment})
        except (RollbackConflict, RollbackRefused) as exc:
            unfinished += 1
            client.update_job(job_id, "needs_manual", result={"error": str(exc)})
        except (RollbackUnavailable, Exception) as exc:  # noqa: BLE001 - every failure must reach the dashboard
            unfinished += 1
            log.exception("rollback job %s failed", job_id)
            client.update_job(job_id, "failed", result={"error": f"{type(exc).__name__}: {exc}"})
    return unfinished


def summarize(output_dir: Path) -> dict:
    status_path = output_dir / "status.json"
    status = json.loads(status_path.read_text(encoding="utf-8")) if status_path.is_file() else {}
    summary = {"osm_writes": "disabled", "report_status": status.get("status")}
    if status.get("error"):
        summary["error"] = str(status["error"])[:500]
    comparison = output_dir / "comparison.json"
    if status.get("status") in {"completed", "completed_with_warnings"} and comparison.is_file():
        result = json.loads(comparison.read_text(encoding="utf-8"))
        stops, routes = result["counts"]["stops"], result["counts"]["routes"]
        summary.update(
            osm_snapshot_at=result.get("osm_snapshot_at"),
            source_dates={provider: min(snap.get("dates", {}).values(), default=None)
                          for provider, snap in result.get("source_snapshots", {}).items()},
            headline={
                "OSM 정류소 대응": stops.get("osm:matched", 0),
                "검토 후보": stops.get("osm:candidate_match", 0),
                "OSM 정류소 대응 미확인": stops.get("osm:osm_unmatched", 0),
                "공식 정류소 대응 미확인": stops.get("source:source_unmatched", 0),
                "공식 노선 대응 미확인": routes.get("source:route:source_unmatched", 0),
            },
            warning_count=len(result.get("warnings", [])),
            exceptions_review_due=sum(bool(e.get("review_due")) for e in result.get("exceptions", [])),
        )
    return summary


def control_poll(client: ControlClient | None = None, api_factory=OsmApi.from_env) -> int:
    client = client or ControlClient.from_env()
    try:
        jobs = client.control()["rollback_jobs"]
    except ControlError as exc:
        log.error("dashboard unreachable: %s", exc)
        return 1
    return 1 if process_rollbacks(client, jobs, api_factory) else 0


def scheduled(data_dir: Path, output_dir: Path, exceptions: Path, client: ControlClient | None = None,
              compare_main=None, api_factory=OsmApi.from_env) -> int:
    if compare_main is None:
        from .cli import main as compare_main
    client = client or ControlClient.from_env()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    try:
        control = client.control()
        process_rollbacks(client, control["rollback_jobs"], api_factory)
        control = client.control()
    except ControlError as exc:
        # Fail closed: if the stop switch cannot be read, nothing runs.
        log.error("dashboard unreachable; not running: %s", exc)
        return 1
    if control["stopped"]:
        log.warning("emergency stop is on: %s", control.get("reason"))
        client.update_run(run_id, status="skipped_stopped", started_at=_now(), finished_at=_now(),
                          code_version=client.version, summary={"stop_reason": control.get("reason"), "osm_writes": "disabled"})
        return 0
    client.update_run(run_id, status="running", started_at=_now(), code_version=client.version)
    code = 1
    try:
        code = compare_main(["run", "--data-dir", str(data_dir), "--output-dir", str(output_dir),
                             "--exceptions", str(exceptions)])
    finally:
        summary = summarize(output_dir)
        report_status = summary.get("report_status")
        status = report_status if code == 0 and report_status in {"completed", "completed_with_warnings"} else "failed"
        for name in UPLOAD_FILES:
            path = output_dir / name
            if path.is_file():
                try:
                    client.upload(run_id, path)
                except ControlError as exc:
                    log.error("could not upload %s: %s", name, exc)
                    summary.setdefault("upload_errors", []).append(name)
        client.update_run(run_id, status=status, finished_at=_now(), summary=summary)
    return code
