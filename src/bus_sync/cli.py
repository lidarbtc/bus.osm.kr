"""Public-data acquisition and read-only comparison commands."""

from __future__ import annotations

import argparse
import json
import logging
import os
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .compare import compare
from .exceptions import load_exceptions
from .osm import read_osm
from .osm_download import checksum, fetch_osm
from .refs import osm_change, plan_refs, self_check
from .report import REPORT_FILES, dump, write_failure, write_ref_plan, write_report
from .sources import fetch_sources, load_sources

log = logging.getLogger("bus-sync")


def _parser():
    parser = argparse.ArgumentParser(description="서울·경기 버스와 OSM의 읽기 전용 전체 비교 (업로드 기능 없음)")
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("run", "compare", "fetch", "fetch-osm", "scheduled"):
        sub = commands.add_parser(command)
        sub.add_argument("--data-dir", type=Path, default=Path("data"))
        if command in {"run", "compare", "scheduled"}:
            sub.add_argument("--osm-file", type=Path, required=command == "compare")
            sub.add_argument("--output-dir", type=Path, default=Path("reports"))
            sub.add_argument("--exceptions", type=Path, default=Path("config/exceptions.json"))
    commands.add_parser("control-poll", help="대시보드에 쌓인 롤백 요청만 처리")
    plan = commands.add_parser("rollback-plan", help="봇 변경세트 롤백 계획을 업로드 없이 출력")
    plan.add_argument("--changeset", type=int, required=True)
    plan.add_argument("--bot-uid", type=int, required=True)
    export = commands.add_parser("export-osc", help="정류소 번호 추가 계획을 구·시별 osmChange 파일로 저장 (업로드 없음)")
    export.add_argument("--plan", type=Path, default=Path("reports/ref-plan.json"))
    export.add_argument("--district", action="append", default=[], help="구·시 이름. 여러 번 지정 가능")
    export.add_argument("--output", type=Path, required=True)
    return parser


def _inventory(path, data_dir):
    # Cache identity includes parser code, so new parsing rules cannot reuse old output.
    import bus_sync.osm as osm_module
    digest = checksum(path)
    code_digest = checksum(Path(osm_module.__file__))[:16]
    cache = data_dir / "osm" / f"{digest}-{code_digest}.json"
    if cache.is_file():
        log.info("Loading parsed OSM inventory %s", cache.name)
        return json.loads(cache.read_text(encoding="utf-8")), digest
    log.info("Parsing full OSM input %s", path)
    inventory = read_osm(path)
    cache.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=cache.parent) as stage:
        temporary = Path(stage) / "inventory.json"
        dump(temporary, inventory)
        temporary.replace(cache)
    return inventory, digest


def _freshness(sources, inventory, today):
    warnings = []
    for provider, snap in sources["snapshots"].items():
        for kind, value in snap.get("dates", {}).items():
            age = (today - date.fromisoformat(value)).days
            if age < -1:
                raise ValueError(f"{provider}/{kind}: snapshot date is in the future: {value}")
            limit = 62 if provider == "seoul" else 7
            if age > limit:
                warnings.append(f"{provider}/{kind}: source is {age} days old; recheck publication status")
    timestamp = inventory.get("snapshot_at")
    if timestamp:
        age = (today - datetime.fromisoformat(timestamp.replace("Z", "+00:00")).date()).days
        if age > 7:
            warnings.append(f"OSM extract is {age} days old")
    else:
        warnings.append("OSM snapshot timestamp is unavailable; do not treat download time as the data date")
    return warnings


def main(argv=None):
    args = _parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    generated = datetime.now(timezone.utc).isoformat()
    # Server entry points report to the dashboard themselves.
    if args.command == "scheduled":
        from .runner import scheduled
        return scheduled(args.data_dir.resolve(), args.output_dir.resolve(), args.exceptions.resolve())
    if args.command == "control-poll":
        from .runner import control_poll
        return control_poll()
    if args.command == "rollback-plan":
        from .rollback import OSM_API, OsmApi, plan_revert
        plan = plan_revert(OsmApi(os.environ.get("OSM_API_BASE", OSM_API)), args.changeset, args.bot_uid)
        print(json.dumps(plan.as_dict(), ensure_ascii=False, indent=2))
        return 0
    if args.command == "export-osc":
        plan = json.loads(args.plan.read_text(encoding="utf-8"))
        known = set(plan["summary"]["by_district"])
        unknown = [d for d in args.district if d not in known]
        if unknown:
            log.error("Unknown district %s; the plan has %s", ", ".join(unknown), ", ".join(sorted(known)))
            return 2
        changes = [c for c in plan["changes"] if not args.district or c["district"] in args.district]
        args.output.write_bytes(osm_change(changes))
        log.info("Wrote %s stop numbers to %s", len(changes), args.output)
        return 0
    try:
        if args.command == "fetch":
            result = fetch_sources(args.data_dir)
            log.info("Acquired %s stop records and %s routes", len(result["stops"]), len(result["routes"]))
            return 0
        if args.command == "fetch-osm":
            path, _ = fetch_osm(args.data_dir)
            log.info("Verified OSM extract: %s", path)
            return 0
        today = datetime.now(ZoneInfo("Asia/Seoul")).date()
        exceptions = load_exceptions(args.exceptions, today=today)
        if args.command == "run":
            log.info("Discovering current Seoul/GBIS bulk files (no personal API key)")
            sources = fetch_sources(args.data_dir)
        else:
            log.info("Using explicitly selected cached source snapshots")
            sources = load_sources(args.data_dir)
        if args.osm_file:
            path = args.osm_file
            manifest = {"path": str(path), "coverage": "user_supplied_not_independently_guaranteed"}
        else:
            path, manifest = fetch_osm(args.data_dir)
        inventory, sha = _inventory(path, args.data_dir)
        # A verified downloaded file remains a national extract even in offline mode.
        sidecar = path.with_suffix(path.suffix + ".json")
        if sidecar.is_file():
            recorded = json.loads(sidecar.read_text())
            if recorded.get("sha256") == sha:
                manifest = recorded
        manifest["sha256"] = sha
        sources["warnings"].extend(_freshness(sources, inventory, today))
        if manifest.get("coverage") != "national_extract":
            sources["warnings"].append("Local OSM file: boundary/reference checks do not prove complete regional coverage")
        log.info("Comparing %s source stop records with %s OSM objects", len(sources["stops"]), len(inventory["stops"]))
        result = compare(sources, inventory, exceptions)
        log.info("Planning stop numbers for mapped stops without one")
        plan = plan_refs(sources, inventory)
        plan["summary"]["self_check"] = self_check(sources, inventory)
        result.update(generated_at=generated, input_mode="live_discovery" if args.command == "run" else "explicit_cached_snapshot",
                      osm_origin=manifest, ref_plan_summary=plan["summary"])
        args.output_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".report-", dir=args.output_dir.parent) as stage:
            stage = Path(stage)
            write_report(stage, result, inventory["boundaries"])
            write_ref_plan(stage, plan, {"generated_at": generated, "osm_snapshot_at": inventory.get("snapshot_at"),
                                         "source_dates": {p: s.get("dates") for p, s in sources["snapshots"].items()},
                                         "osm_writes": "disabled"})
            dump(stage / "source-manifest.json", {"files": sources["files"], "snapshots": sources["snapshots"]})
            dump(stage / "osm-manifest.json", manifest)
            for name in REPORT_FILES:
                if (stage / name).exists():
                    (stage / name).replace(args.output_dir / name)
        log.info("Report written to %s (%s warnings)", args.output_dir / "report.html", len(result["warnings"]))
        return 0
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        log.error("%s", message)
        if hasattr(args, "output_dir"):
            write_failure(args.output_dir, message, generated)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
