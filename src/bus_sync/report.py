"""Local, self-contained reports with no remote scripts or edit links."""

from __future__ import annotations

import csv
import html
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

REPORT_FILES = ("comparison.json", "stops.csv", "routes.csv", "stops.geojson", "report.html", "report-data.json",
                "summary.md", "status.json", "source-manifest.json", "osm-manifest.json",
                "ref-plan.json", "ref-tasks.geojson", "ref-changes.osc")
# The dashboard renders report-data.json itself, so the self-contained report.html stays local.
UPLOAD_FILES = tuple(name for name in REPORT_FILES if name != "report.html")
STATUS_LABELS = {"matched": "대응", "matched_metadata": "속성 대응", "candidate_match": "후보",
                 "osm_unmatched": "공식 대응 없음", "source_unmatched": "OSM 대응 없음", "preserved_exception": "예외",
                 "needs_classification": "분류 필요", "needs_scope_review": "지역 확인 필요",
                 "outside_scope_support": "경계 밖", "out_of_mode_scope": "버스 외"}
PROVIDER_LABELS = {"seoul": "서울", "gbis": "경기"}
ATTRIBUTION = "© OpenStreetMap contributors (ODbL) · 서울특별시 (공공누리 제1유형) · 경기도"


def dump(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _kst(value) -> str:
    if not value:
        return "알 수 없음"
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value)
    return moment.astimezone(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M")


def _count_label(kind: str, key: str) -> tuple[str, str]:
    side, *rest = key.split(":")
    subject = "OSM" if side == "osm" else "공식"
    if rest[0] == "route_master":
        subject += " 노선 그룹"
    return ("정류소" if kind == "stops" else "노선") + " · " + subject, STATUS_LABELS.get(rest[-1], rest[-1])


def _csv(path, rows):
    fields = ["key", "side", "kind", "status", "provider", "name", "ref", "operator", "lon", "lat",
              "in_scope", "scope_state", "action", "exception_id", "candidate_keys", "evidence"]
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            result = {k: row.get(k, "") for k in fields}
            result["exception_id"] = (row.get("exception") or {}).get("id", "")
            result["candidate_keys"] = ";".join(c.get("osm_key", c.get("source_key", "")) for c in row["candidates"])
            result["evidence"] = json.dumps(row["candidates"], ensure_ascii=False)
            for key, value in result.items():
                if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
                    result[key] = "'" + value
            writer.writerow(result)


def write_ref_plan(output: Path, plan: dict, meta: dict) -> None:
    """The stop-number plan: every change with its evidence, mapper tasks, and an osmChange for JOSM."""
    from .refs import osm_change, tasks_geojson
    dump(output / "ref-plan.json", {**meta, "summary": plan["summary"], "changes": plan["changes"], "tasks": plan["tasks"]})
    dump(output / "ref-tasks.geojson", tasks_geojson(plan["tasks"]))
    (output / "ref-changes.osc").write_bytes(osm_change(plan["changes"]))


def write_report(output: Path, result: dict, boundaries: dict) -> None:
    output.mkdir(parents=True, exist_ok=True)
    dump(output / "comparison.json", result)
    dump(output / "status.json", {"status": "completed_with_warnings" if result["warnings"] else "completed",
                                 "read_only": True, "generated_at": result["generated_at"],
                                 "warning_count": len(result["warnings"])})
    for kind in ("stops", "routes"):
        _csv(output / f"{kind}.csv", result[kind])
    features = [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [r["lon"], r["lat"]]},
                 "properties": {k: r[k] for k in ("key", "side", "status", "provider", "name", "in_scope", "action")}}
                for r in result["stops"] if r.get("lon") is not None and r.get("lat") is not None]
    dump(output / "stops.geojson", {"type": "FeatureCollection", "features": features,
                                   "attribution": "© OpenStreetMap contributors; Seoul/GBIS public data"})
    from .refs import TASK_LABELS
    lines = ["# 서울·경기 버스 비교", "",
             f"생성 {_kst(result['generated_at'])} · OSM 기준 {_kst(result.get('osm_snapshot_at'))} (KST)", "",
             "## 원천", "", "| 원천 | 기준일 | 정류소 | 노선 |", "|---|---|---:|---:|"]
    for provider, snapshot in result["source_snapshots"].items():
        dates = sorted(set(snapshot.get("dates", {}).values()))
        date = " ~ ".join(dict.fromkeys([dates[0], dates[-1]])) if dates else ""
        stops, routes = snapshot.get("stop_count"), snapshot.get("route_count")
        lines.append(f"| {PROVIDER_LABELS.get(provider, provider)} | {date} | "
                     f"{f'{stops:,}' if isinstance(stops, int) else ''} | {f'{routes:,}' if isinstance(routes, int) else ''} |")
    lines += ["", "## 대응 현황", "", "| 구분 | 상태 | 개수 |", "|---|---|---:|"]
    for kind, counts in result["counts"].items():
        for status, count in sorted(counts.items()):
            subject, label = _count_label(kind, status)
            lines.append(f"| {subject} | {label} | {count:,} |")
    lines += ["", "## 예외", ""]
    if result["exceptions"]:
        lines += ["| 예외 | 발견 | 미발견 | 재검토 |", "|---|---:|---:|---|"]
        lines.extend(f"| {e['id']} | {len(e['observed'])} | {len(e['not_observed'])} | {'필요' if e['review_due'] else ''} |"
                     for e in result["exceptions"])
    else:
        lines.append("없음")
    plan = result.get("ref_plan_summary")
    if plan:
        facts = [f"추가 대상 {plan['changes']:,}곳", f"확인 작업 {sum(plan['tasks'].values()):,}곳"]
        check = plan.get("self_check") or {}
        if check.get("agreement") is not None:
            facts.append(f"기존 번호 일치율 {check['agreement']:.2%}")
        lines += ["", "## 정류소 번호 추가 계획", "", " · ".join(facts), "", "| 확인 작업 | 개수 |", "|---|---:|"]
        lines.extend(f"| {TASK_LABELS.get(kind, kind)} | {count:,} |"
                     for kind, count in sorted(plan["tasks"].items(), key=lambda x: -x[1]))
    if result["warnings"]:
        lines += ["", f"## 데이터 경고 ({len(result['warnings'])})", ""]
        lines.extend("- " + str(w).replace("\n", " ") for w in result["warnings"])
    lines += ["", ATTRIBUTION, ""]
    (output / "summary.md").write_text("\n".join(lines), encoding="utf-8")
    compact = [{"key": r["key"], "side": r["side"], "kind": r["kind"], "status": r["status"],
                "provider": r["provider"], "name": r["name"], "ref": r.get("ref", ""),
                "lon": r.get("lon"), "lat": r.get("lat"), "in_scope": r.get("in_scope", True),
                "scope_state": r.get("scope_state", "inside"),
                "exception": (r.get("exception") or {}).get("id", ""),
                "candidates": r["candidates"]} for r in result["stops"] + result["routes"]]
    title = f"OSM {result.get('osm_snapshot_at') or '기준일 미상'} · 생성 {result['generated_at']}"
    source_dates = {provider: min(snapshot.get("dates", {}).values(), default=None)
                    for provider, snapshot in result["source_snapshots"].items()}
    data = json.dumps({"title": title, "generated_at": result["generated_at"], "osm_snapshot_at": result.get("osm_snapshot_at"),
                       "source_dates": source_dates, "rows": compact, "boundaries": boundaries},
                      ensure_ascii=False, separators=(",", ":"))
    (output / "report-data.json").write_text(data, encoding="utf-8")
    page = HTML.replace("__TITLE__", html.escape(title)).replace("__DATA__", data.replace("<", "\\u003c"))
    (output / "report.html").write_text(page, encoding="utf-8")


def write_failure(output: Path, message: str, generated_at: str) -> None:
    output.mkdir(parents=True, exist_ok=True)
    # Only our named report files are replaced; never remove the output folder.
    for filename in REPORT_FILES:
        (output / filename).unlink(missing_ok=True)
    dump(output / "status.json", {"status": "failed", "read_only": True, "generated_at": generated_at,
                                 "error": message, "comparison_completed": False})
    (output / "summary.md").write_text("# 비교 실패\n\n" + message.replace("\n", " ") + "\n", encoding="utf-8")
    (output / "report.html").write_text("<!doctype html><meta charset=utf-8><title>비교 실패</title>"
                                       "<h1>비교 실패</h1><pre>" + html.escape(message) + "</pre>", encoding="utf-8")


HTML = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>서울·경기 버스 비교</title><style>
:root{color-scheme:light dark;font-family:system-ui,-apple-system,"Apple SD Gothic Neo","Noto Sans KR",sans-serif;color:#13242f;background:#f4f6f8;--line:#e1e7ec;--card:#fff;--muted:#5a6a76}
@media (prefers-color-scheme:dark){:root{color:#e3e9ee;background:#0d1317;--line:#24303a;--card:#141c22;--muted:#9aaab6}}
body{max-width:1200px;margin:auto;padding:28px 20px}h1{font-size:24px;margin:0 0 4px}.muted{color:var(--muted);font-size:14px}
.controls{display:flex;gap:8px;flex-wrap:wrap;margin:20px 0 12px}input,select,button{font:inherit;font-size:14px;border:1px solid var(--line);border-radius:8px;padding:8px 10px;background:var(--card);color:inherit}
input[type=search]{flex:1 1 240px}button{cursor:pointer}canvas{display:block;width:100%;height:300px;background:var(--card);border:1px solid var(--line);border-radius:12px}
.legend{display:flex;gap:16px;margin:8px 4px;color:var(--muted);font-size:13px}.legend i{display:inline-block;width:8px;height:8px;margin-right:6px;border-radius:50%}
.table{overflow:auto;background:var(--card);border:1px solid var(--line);border-radius:12px}table{border-collapse:collapse;width:100%;font-size:14px}
td,th{padding:10px 14px;text-align:left;border-bottom:1px solid var(--line);vertical-align:top}th{color:var(--muted);font-size:12px}a{color:#2a76b9}
pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}.nav{display:flex;align-items:center;gap:12px;margin:14px 0}
</style></head><body><h1>서울·경기 버스 비교</h1><p class="muted">__TITLE__</p>
<div class="controls"><input id="search" type="search" placeholder="이름, 번호, OSM ID" aria-label="검색">
<select id="kind" aria-label="종류"><option value="">모든 종류</option><option value="stop">정류소</option><option value="route">노선</option><option value="route_master">노선 그룹</option></select>
<select id="side" aria-label="출처"><option value="">모든 출처</option><option value="source">공식 (서울·경기)</option><option value="seoul">서울</option><option value="gbis">경기</option><option value="osm">OSM</option></select>
<select id="status" aria-label="상태"><option value="">모든 상태</option></select>
<label class="muted"><input type="checkbox" id="support"> 경계 밖 포함</label></div>
<canvas id="map" aria-label="정류소 위치"></canvas>
<div class="legend"><span><i style="background:#2a76b9"></i>공식</span><span><i style="background:#e0642a"></i>OSM</span><span><i style="background:#1e9b55"></i>예외</span></div>
<div class="nav"><strong id="count"></strong><button id="prev" aria-label="이전 페이지">‹</button><span id="page"></span><button id="next" aria-label="다음 페이지">›</button></div>
<div class="table"><table><thead><tr><th>이름</th><th>구분</th><th>객체</th><th>상태</th><th>대응</th></tr></thead><tbody id="body"></tbody></table></div>
<p class="muted">© OpenStreetMap contributors (ODbL) · 서울특별시 (공공누리 제1유형) · 경기도</p>
<script id="payload" type="application/json">__DATA__</script><script>
'use strict';const data=JSON.parse(document.getElementById('payload').textContent);const $=id=>document.getElementById(id);
const labels={matched:'대응',matched_metadata:'속성 대응',candidate_match:'후보',osm_unmatched:'공식 대응 없음',source_unmatched:'OSM 대응 없음',preserved_exception:'예외',needs_classification:'분류 필요',needs_scope_review:'지역 확인 필요',outside_scope_support:'경계 밖',out_of_mode_scope:'버스 외'};
const providers={seoul:'서울',gbis:'경기',osm:'OSM'},kinds={stop:'정류소',route:'노선',route_master:'노선 그룹'};
let filtered=[],page=0;const size=100;const present=new Set(data.rows.map(r=>r.status));
for(const s of Object.keys(labels)){if(!present.has(s))continue;const o=document.createElement('option');o.value=s;o.textContent=labels[s];$('status').append(o)}
const rings=[];for(const f of data.boundaries.features){const polys=f.geometry.type==='Polygon'?[f.geometry.coordinates]:f.geometry.coordinates;for(const p of polys)for(const r of p)rings.push(r)}
let minX=Infinity,maxX=-Infinity,minY=Infinity,maxY=-Infinity;for(const ring of rings)for(const [x,y]of ring){minX=Math.min(minX,x);maxX=Math.max(maxX,x);minY=Math.min(minY,y);maxY=Math.max(maxY,y)}
const k=Math.cos((minY+maxY)/2*Math.PI/180);
function draw(){const c=$('map'),dpr=window.devicePixelRatio||1,w=c.clientWidth,h=c.clientHeight,p=12;c.width=w*dpr;c.height=h*dpr;const ctx=c.getContext('2d');ctx.scale(dpr,dpr);
const scale=Math.min((w-2*p)/((maxX-minX)*k),(h-2*p)/(maxY-minY));const ox=(w-(maxX-minX)*k*scale)/2,oy=(h-(maxY-minY)*scale)/2;
const xy=(x,y)=>[ox+(x-minX)*k*scale,h-oy-(y-minY)*scale];ctx.strokeStyle='#8a9ba8';ctx.lineWidth=1;
for(const ring of rings){ctx.beginPath();ring.forEach(([x,y],i)=>{const a=xy(x,y);i?ctx.lineTo(...a):ctx.moveTo(...a)});ctx.closePath();ctx.stroke()}
ctx.globalAlpha=filtered.length>20000?.5:.8;for(const r of filtered){if(r.lon===null||r.lat===null)continue;const[x,y]=xy(r.lon,r.lat);ctx.fillStyle=r.exception?'#1e9b55':r.provider==='osm'?'#e0642a':'#2a76b9';ctx.fillRect(x-1,y-1,2,2)}ctx.globalAlpha=1}
function render(){const total=Math.max(1,Math.ceil(filtered.length/size));page=Math.max(0,Math.min(page,total-1));$('body').replaceChildren();
for(const r of filtered.slice(page*size,(page+1)*size)){const tr=document.createElement('tr');const add=text=>{const td=document.createElement('td');td.textContent=text;tr.append(td);return td};
add((r.name||'이름 없음')+(r.ref&&r.ref!==r.name?' · '+r.ref:''));add((providers[r.provider]||r.provider)+' '+(kinds[r.kind]||r.kind));const id=add('');
if(r.provider==='osm'&&/^(node|way|relation)[/][0-9]+$/.test(r.key)){const a=document.createElement('a');a.href='https://www.openstreetmap.org/'+r.key;a.textContent=r.key;a.target='_blank';a.rel='noopener';id.append(a)}else{id.textContent=r.key.split(':').pop()}
add((labels[r.status]||r.status)+(r.exception?' · '+r.exception:''));const td=add('');if(r.candidates.length){const de=document.createElement('details');const su=document.createElement('summary');su.textContent='후보 '+r.candidates.length;de.append(su);const pr=document.createElement('pre');pr.textContent=JSON.stringify(r.candidates,null,2);de.append(pr);td.append(de)}else{td.textContent='—'}$('body').append(tr)}
$('count').textContent=filtered.length.toLocaleString('ko-KR')+'건';$('page').textContent=(page+1)+' / '+total;$('prev').disabled=page===0;$('next').disabled=page+1>=total;draw()}
function filter(){const q=$('search').value.trim().toLowerCase(),side=$('side').value;filtered=data.rows.filter(r=>(!$('kind').value||r.kind===$('kind').value)&&(side==='source'?r.provider!=='osm':!side||r.provider===side)&&(!$('status').value||r.status===$('status').value)&&($('support').checked||r.in_scope||r.scope_state==='unknown')&&(!q||(r.name+' '+r.ref+' '+r.key).toLowerCase().includes(q)));page=0;render()}
for(const id of ['search','kind','side','status','support'])$(id).addEventListener('input',filter);$('prev').onclick=()=>{page--;render()};$('next').onclick=()=>{page++;render()};window.addEventListener('resize',draw);filter();
</script></body></html>"""
