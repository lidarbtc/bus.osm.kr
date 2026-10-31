"use strict";
// Renders report-data.json with textContent only; OSM and public-data strings are never parsed as HTML.
const $ = (id) => document.getElementById(id);
const labels = {matched: "정류소 대응 확인", matched_metadata: "노선 속성 대응", candidate_match: "대응 후보 검토", osm_unmatched: "공식 대응 미확인",
  source_unmatched: "OSM 대응 미확인", preserved_exception: "예외 보존", needs_classification: "교통수단 확인 필요", needs_scope_review: "지역 확인 필요",
  outside_scope_support: "경계 밖 연결용", out_of_mode_scope: "버스 외 교통수단"};
const size = 100;
let data, filtered = [], page = 0, rings = [], bounds;

function draw() {
  const c = $("map"), dpr = window.devicePixelRatio || 1;
  c.width = c.clientWidth * dpr; c.height = 320 * dpr;
  const ctx = c.getContext("2d"); ctx.scale(dpr, dpr);
  const [minX, minY, maxX, maxY] = bounds, w = c.clientWidth, h = 320, pad = 15;
  const scale = Math.min((w - 2 * pad) / ((maxX - minX) * 0.79), (h - 2 * pad) / (maxY - minY));
  const ox = (w - (maxX - minX) * 0.79 * scale) / 2, oy = (h - (maxY - minY) * scale) / 2;
  const xy = (x, y) => [ox + (x - minX) * 0.79 * scale, h - oy - (y - minY) * scale];
  ctx.strokeStyle = "#879ba8"; ctx.lineWidth = 1;
  for (const ring of rings) { ctx.beginPath(); ring.forEach(([x, y], i) => { const a = xy(x, y); i ? ctx.lineTo(...a) : ctx.moveTo(...a); }); ctx.closePath(); ctx.stroke(); }
  ctx.globalAlpha = 0.6;
  for (const r of filtered) {
    if (r.lon === null || r.lat === null || r.lon === undefined) continue;
    const [x, y] = xy(r.lon, r.lat);
    ctx.fillStyle = r.exception ? "#127548" : r.side === "osm" ? "#ce6425" : "#1676b5";
    ctx.fillRect(x - 1, y - 1, 2, 2);
  }
  ctx.globalAlpha = 1;
}

function render() {
  const total = Math.max(1, Math.ceil(filtered.length / size));
  page = Math.max(0, Math.min(page, total - 1));
  $("body").replaceChildren();
  for (const r of filtered.slice(page * size, (page + 1) * size)) {
    const tr = document.createElement("tr");
    const add = (text) => { const td = document.createElement("td"); td.textContent = text; tr.append(td); return td; };
    add((r.name || "(이름 없음)") + (r.ref ? " / " + r.ref : ""));
    add(r.provider + " · " + r.kind);
    const id = add("");
    if (r.side === "osm" && /^(node|way|relation)\/\d+$/.test(r.key)) {
      const a = document.createElement("a"); a.href = "https://www.openstreetmap.org/" + r.key; a.textContent = r.key; a.target = "_blank"; a.rel = "noopener"; id.append(a);
    } else id.textContent = r.key;
    add((labels[r.status] || r.status) + (r.exception ? " / " + r.exception : "")).className = "status";
    const details = document.createElement("details"), summary = document.createElement("summary"), pre = document.createElement("pre");
    summary.textContent = r.candidates.length + "개 후보"; pre.textContent = JSON.stringify(r.candidates, null, 2);
    details.append(summary, pre); add("").append(details);
    $("body").append(tr);
  }
  $("count").textContent = filtered.length.toLocaleString("ko-KR") + "개 레코드";
  $("page").textContent = (page + 1) + " / " + total;
  $("prev").disabled = page === 0; $("next").disabled = page + 1 >= total;
  draw();
}

function filter() {
  const q = $("search").value.trim().toLowerCase();
  filtered = data.rows.filter((r) => (!$("kind").value || r.kind === $("kind").value) && (!$("side").value || r.provider === $("side").value)
    && (!$("status").value || r.status === $("status").value) && ($("support").checked || r.in_scope || r.scope_state === "unknown")
    && (!q || (r.name + " " + r.ref + " " + r.key).toLowerCase().includes(q)));
  page = 0; render();
}

async function load() {
  const run = new URLSearchParams(location.search).get("run") || "latest";
  const response = await fetch("/api/reports/" + encodeURIComponent(run) + "/report-data.json");
  if (!response.ok) throw new Error(response.status === 404 ? "보고서를 찾을 수 없습니다." : "보고서를 내려받지 못했습니다.");
  const text = await new Response(response.body.pipeThrough(new DecompressionStream("gzip"))).text();
  data = JSON.parse(text);
  $("title").textContent = data.title || "";
  for (const s of [...new Set(data.rows.map((r) => r.status))].sort()) {
    const o = document.createElement("option"); o.value = s; o.textContent = labels[s] || s; $("status").append(o);
  }
  $("warnings-title").textContent = "데이터 주의 사항 (" + data.warnings.length + ")";
  $("warnings").textContent = data.warnings.join("\n");
  for (const f of data.boundaries.features) {
    const polys = f.geometry.type === "Polygon" ? [f.geometry.coordinates] : f.geometry.coordinates;
    for (const p of polys) for (const r of p) rings.push(r);
  }
  bounds = rings.flat().reduce((b, [x, y]) => [Math.min(b[0], x), Math.min(b[1], y), Math.max(b[2], x), Math.max(b[3], y)], [Infinity, Infinity, -Infinity, -Infinity]);
  for (const id of ["search", "kind", "side", "status", "support"]) $(id).addEventListener("input", filter);
  $("prev").onclick = () => { page--; render(); };
  $("next").onclick = () => { page++; render(); };
  window.addEventListener("resize", draw);
  filter();
}
load().catch((e) => { $("title").textContent = e.message; });
