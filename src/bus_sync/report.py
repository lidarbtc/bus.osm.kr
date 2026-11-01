"""Local, self-contained reports with no remote scripts or edit links."""

from __future__ import annotations

import csv
import html
import json
from pathlib import Path

REPORT_FILES = ("comparison.json", "stops.csv", "routes.csv", "stops.geojson", "report.html", "report-data.json",
                "summary.md", "status.json", "source-manifest.json", "osm-manifest.json",
                "ref-plan.json", "ref-tasks.geojson", "ref-changes.osc")
# The dashboard renders report-data.json itself, so the self-contained report.html stays local.
UPLOAD_FILES = tuple(name for name in REPORT_FILES if name != "report.html")


def dump(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


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
    lines = ["# 서울·경기 버스 비교", "", "읽기 전용 결과입니다. 미일치는 폐지·삭제·신규 추가 판정이 아닙니다.", "",
             f"생성: {result['generated_at']} · OSM 기준: {result.get('osm_snapshot_at') or '알 수 없음'}", "",
             "## 원천 기준일", "", "| 원천 | 기준일 | 정류소 레코드 | 노선 레코드 |", "|---|---|---:|---:|"]
    for provider, snapshot in result["source_snapshots"].items():
        dates = ", ".join(f"{k}={v}" for k, v in snapshot.get("dates", {}).items())
        lines.append(f"| {provider} | {dates} | {snapshot.get('stop_count', '')} | {snapshot.get('route_count', '')} |")
    lines += ["", "## 지역 내 정류소 / 경유 노선 객체", "",
              "정류소 수치는 공급원별 레코드와 OSM 객체 수입니다. 승강장·정차 지점, 양방향 노선은 별도 객체일 수 있습니다.", "",
              "| 구분 | 상태 | 개수 |", "|---|---|---:|"]
    for kind, counts in result["counts"].items():
        for status, count in sorted(counts.items()):
            lines.append(f"| {kind} | {status} | {count:,} |")
    lines += ["", f"경계 밖 연결용 정류소 객체: {result['cross_boundary_support_stop_objects']:,}",
              f"경계 밖 연결용 노선 객체: {result.get('cross_boundary_support_route_objects', 0):,}",
              f"지역 확인이 필요한 정류소/노선 객체: {result.get('unknown_scope_stop_objects', 0):,} / {result.get('unknown_scope_route_objects', 0):,}", "",
              "## 예외", "", "| 예외 | 발견 | 미발견 | 재검토 기한 도래 |", "|---|---:|---:|---|"]
    for e in result["exceptions"]:
        lines.append(f"| {e['id']} | {len(e['observed'])} | {len(e['not_observed'])} | {e['review_due']} |")
    plan = result.get("ref_plan_summary")
    if plan:
        lines += ["", "## 정류소 번호 추가 계획 (업로드 전)", "",
                  f"번호가 없는 OSM 정류소 {plan['changes']:,}곳에 공식 정류소 번호(ref)를 추가할 수 있습니다. "
                  "근거와 osmChange 파일은 ref-plan.json, ref-changes.osc에 있습니다.", ""]
        check = plan.get("self_check") or {}
        if check.get("agreement") is not None:
            lines += [f"자체 점검: 번호가 있는 OSM 정류소 {check['hidden']:,}곳의 번호를 가리고 다시 계획하니 "
                      f"{check['assigned']:,}곳에 번호를 배정했고, 매퍼가 넣은 번호와 {check['agreement']:.2%} 일치했습니다 "
                      f"(불일치 {check['disagree']:,}곳, 매퍼 번호가 그 자리 공식 데이터에 없는 곳 "
                      f"{check['mapped_number_not_official_here']:,}곳).", ""]
        lines += ["| 매퍼 확인 작업 | 개수 |", "|---|---:|"]
        lines.extend(f"| {kind} | {count:,} |" for kind, count in sorted(plan["tasks"].items(), key=lambda x: -x[1]))
    lines += ["", "미발견 예외도 삭제되지 않습니다. 노선의 도로 형상·시간표는 검증하지 않았습니다.", "",
              "report.html에서 검색·필터, comparison.json에서 대응 근거, CSV/GeoJSON에서 전체 결과를 확인할 수 있습니다.", "",
              f"## 주의 사항 ({len(result['warnings'])})", ""]
    lines.extend("- " + str(w).replace("\n", " ") for w in result["warnings"])
    lines += ["", "© OpenStreetMap contributors — ODbL. 서울 공공파일: 공공누리 1유형. 경기: 원천 이용조건은 source-manifest.json 참조.", ""]
    (output / "summary.md").write_text("\n".join(lines), encoding="utf-8")
    compact = [{"key": r["key"], "side": r["side"], "kind": r["kind"], "status": r["status"],
                "provider": r["provider"], "name": r["name"], "ref": r.get("ref", ""),
                "lon": r.get("lon"), "lat": r.get("lat"), "in_scope": r.get("in_scope", True),
                "scope_state": r.get("scope_state", "inside"),
                "exception": (r.get("exception") or {}).get("id", ""),
                "candidates": r["candidates"]} for r in result["stops"] + result["routes"]]
    title = f"OSM {result.get('osm_snapshot_at') or '기준일 미상'} · 생성 {result['generated_at']}"
    data = json.dumps({"title": title, "rows": compact, "boundaries": boundaries, "warnings": result["warnings"]},
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
    (output / "summary.md").write_text("# 비교 실패\n\n전체 비교가 완료되지 않았습니다. 차이 0건을 뜻하지 않습니다.\n\n"
                                      + message.replace("\n", " ") + "\n", encoding="utf-8")
    (output / "report.html").write_text("<!doctype html><meta charset=utf-8><title>비교 실패</title>"
                                       "<h1>전체 비교 미완료</h1><p>차이 0건을 뜻하지 않습니다.</p><pre>"
                                       + html.escape(message) + "</pre>", encoding="utf-8")


HTML = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>서울·경기 버스 비교</title><style>
:root{font-family:system-ui,sans-serif;color:#172c3b;background:#f4f7fa}body{max-width:1450px;margin:auto;padding:28px}
h1{font-size:28px;margin-bottom:8px}p{line-height:1.6}.muted{color:#536777;font-size:14px}.controls{display:flex;gap:12px;flex-wrap:wrap;margin:22px 0}
input,select,button{font:inherit;border:1px solid #becad3;border-radius:7px;padding:9px;background:white}input[type=search]{min-width:280px}
button{cursor:pointer}canvas{width:100%;height:320px;background:white;border:1px solid #d7e0e7;border-radius:10px}
.table{overflow:auto;background:white;border-radius:10px}table{border-collapse:collapse;width:100%;font-size:14px}td,th{padding:12px;text-align:left;border-bottom:1px solid #e3e9ee;vertical-align:top}
th{background:#eaf0f5}a{color:#075c9b}.status{font-family:monospace;font-size:12px}details{margin:16px 0}pre{white-space:pre-wrap;overflow-wrap:anywhere}.nav{display:flex;align-items:center;gap:15px;margin:15px 0}
</style></head><body><h1>서울·경기 버스 비교</h1><p class="muted">__TITLE__</p>
<p>공식 데이터와 OSM의 대응 근거를 확인하는 읽기 전용 보고서입니다. <b>미일치는 폐지나 삭제 판정이 아닙니다.</b>
정류소는 원천 레코드·OSM 객체 단위이며 노선의 도로 형상과 시간표는 검증하지 않았습니다.</p>
<div class="controls"><input id="search" type="search" placeholder="이름, 번호, 객체 ID 검색" aria-label="이름 번호 객체 ID 검색">
<select id="kind" aria-label="종류"><option value="">전체 종류</option value="stop">정류소</option><option value="route">노선</option><option value="route_master">노선 그룹</option></select>
<select id="side" aria-label="원천"><option value="">전체 원천</option><option value="seoul">서울 공식</option><option value="gbis">경기 공식</option><option value="osm">OSM</option></select>
<select id="status" aria-label="비교 상태"><option value="">전체 상태</option></select>
<label><input type="checkbox" id="support"> 경계 밖 정류소·노선 포함</label></div>
<p class="muted">지도는 행정경계와 필터에 해당하는 정류소 위치를 표시합니다. 파랑: 공식 데이터 · 주황: OSM · 초록: 보존 예외. 경계 밖 연결용 객체는 기본 지도 범위 밖에 있을 수 있습니다.</p>
<canvas id="map" aria-label="서울 경기 행정경계 및 정류소 분포"></canvas>
<div class="nav"><strong id="count"></strong><button id="prev">이전</button><span id="page"></span><button id="next">다음</button></div>
<div class="table"><table><thead><tr><th>이름 / 번호</th><th>원천 · 종류</th><th>객체</th><th>상태 / 예외</th><th>대응 근거</th></tr></thead><tbody id="body"></tbody></table></div>
<details><summary id="warnings-title">데이터 주의 사항</summary><pre id="warnings"></pre></details>
<p class="muted">© OpenStreetMap contributors — <a href="https://www.openstreetmap.org/copyright">ODbL</a> · 서울 공공누리 1유형 · 경기 원천 조건은 source-manifest.json 참조</p>
<script id="payload" type="application/json">__DATA__</script><script>
'use strict';const data=JSON.parse(document.getElementById('payload').textContent);const $=id=>document.getElementById(id);
const labels={matched:'정류소 대응 확인',matched_metadata:'노선 속성 대응',candidate_match:'대응 후보 검토',osm_unmatched:'공식 대응 미확인',source_unmatched:'OSM 대응 미확인',preserved_exception:'예외 보존',needs_classification:'교통수단 확인 필요',needs_scope_review:'지역 확인 필요',outside_scope_support:'경계 밖 연결용',out_of_mode_scope:'버스 외 교통수단'};
let filtered=[],page=0;const size=100;
for(const s of [...new Set(data.rows.map(r=>r.status))].sort()){const o=document.createElement('option');o.value=s;o.textContent=labels[s]||s;$('status').append(o)}
$('warnings-title').textContent='데이터 주의 사항 ('+data.warnings.length+')';$('warnings').textContent=data.warnings.join('\\n');
const rings=[];for(const f of data.boundaries.features){const polys=f.geometry.type==='Polygon'?[f.geometry.coordinates]:f.geometry.coordinates;for(const p of polys)for(const r of p)rings.push(r)}
let minX=Infinity,maxX=-Infinity,minY=Infinity,maxY=-Infinity;for(const ring of rings)for(const [x,y]of ring){minX=Math.min(minX,x);maxX=Math.max(maxX,x);minY=Math.min(minY,y);maxY=Math.max(maxY,y)}
function draw(){const c=$('map'),dpr=window.devicePixelRatio||1;c.width=c.clientWidth*dpr;c.height=320*dpr;const ctx=c.getContext('2d');ctx.scale(dpr,dpr);const w=c.clientWidth,h=320,p=15;
const scale=Math.min((w-2*p)/((maxX-minX)*.79),(h-2*p)/(maxY-minY));const ox=(w-(maxX-minX)*.79*scale)/2,oy=(h-(maxY-minY)*scale)/2;
const xy=(x,y)=>[ox+(x-minX)*.79*scale,h-oy-(y-minY)*scale];ctx.strokeStyle='#879ba8';ctx.lineWidth=1;
for(const ring of rings){ctx.beginPath();ring.forEach(([x,y],i)=>{const a=xy(x,y);i?ctx.lineTo(...a):ctx.moveTo(...a)});ctx.closePath();ctx.stroke()}
for(const r of filtered){if(r.lon===null||r.lat===null)continue;const[x,y]=xy(r.lon,r.lat);ctx.fillStyle=r.exception?'#127548':r.side==='osm'?'#ce6425':'#1676b5';ctx.globalAlpha=.6;ctx.fillRect(x-1,y-1,2,2)}ctx.globalAlpha=1}
function render(){const total=Math.max(1,Math.ceil(filtered.length/size));page=Math.max(0,Math.min(page,total-1));$('body').replaceChildren();
for(const r of filtered.slice(page*size,(page+1)*size)){const tr=document.createElement('tr');const add=text=>{const td=document.createElement('td');td.textContent=text;tr.append(td);return td};
add((r.name||'(이름 없음)')+(r.ref?' / '+r.ref:''));add(r.provider+' · '+r.kind);const id=add('');
if(r.side==='osm'){const a=document.createElement('a');a.href='https://www.openstreetmap.org/'+r.key;a.textContent=r.key;a.target='_blank';a.rel='noopener';id.append(a)}else{id.textContent=r.key}
const st=add((labels[r.status]||r.status)+(r.exception?' / '+r.exception:''));st.className='status';const td=add('');const de=document.createElement('details');const su=document.createElement('summary');su.textContent=r.candidates.length+'개 후보';de.append(su);const pr=document.createElement('pre');pr.textContent=JSON.stringify(r.candidates,null,2);de.append(pr);td.append(de);$('body').append(tr)}
$('count').textContent=filtered.length.toLocaleString()+'개 레코드';$('page').textContent=(page+1)+' / '+total;$('prev').disabled=page===0;$('next').disabled=page+1>=total;draw()}
function filter(){const q=$('search').value.trim().toLowerCase();filtered=data.rows.filter(r=>(!$('kind').value||r.kind===$('kind').value)&&(!$('side').value||r.provider===$('side').value)&&(!$('status').value||r.status===$('status').value)&&($('support').checked||r.in_scope||r.scope_state==='unknown')&&(!q||(r.name+' '+r.ref+' '+r.key).toLowerCase().includes(q)));page=0;render()}
for(const id of ['search','kind','side','status','support'])$(id).addEventListener('input',filter);$('prev').onclick=()=>{page--;render()};$('next').onclick=()=>{page++;render()};window.addEventListener('resize',draw);filter();
</script></body></html>"""
