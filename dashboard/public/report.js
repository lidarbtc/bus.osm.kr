"use strict";
const STATUS = {
  matched: ["대응", "ok"], matched_metadata: ["속성 대응", "ok"], candidate_match: ["후보", "info"],
  osm_unmatched: ["공식 대응 없음", "warn"], source_unmatched: ["OSM 대응 없음", "warn"], preserved_exception: ["예외", "muted"],
  needs_classification: ["분류 필요", "muted"], needs_scope_review: ["지역 확인 필요", "muted"],
  outside_scope_support: ["경계 밖", "muted"], out_of_mode_scope: ["버스 외", "muted"],
};
const PROVIDERS = {seoul: "서울", gbis: "경기", osm: "OSM"};
const KINDS = {stop: "정류소", route: "노선", route_master: "노선 그룹"};
const BASIS = {source_id: "ID", public_ref: "정류소 번호", name: "이름", route_ref: "노선 번호", operator: "운영사", stop_sequence: "정류소 순서"};
const FILTERS = ["kind", "side", "status"];
const PAGE = 100;
const KST = "Asia/Seoul";
const $ = (id) => document.getElementById(id);
let data = null;
let rows = [];
let haystack = [];
let filtered = [];
let page = 0;
let rings = [];
let bounds = null;

function el(tag, props, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (key === "class") node.className = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (value !== null && value !== undefined && value !== false) node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) if (child !== null && child !== undefined && child !== false) node.append(child instanceof Node ? child : String(child));
  return node;
}
const chip = ([text, tone]) => el("span", {class: "chip chip-" + tone}, text);
const osmLink = (key) => el("a", {href: "https://www.openstreetmap.org/" + key, target: "_blank", rel: "noopener", class: "mono"}, key);
const td = (label, ...content) => el("td", label ? {"data-label": label} : {}, ...content);
const when = (value) => new Intl.DateTimeFormat("ko-KR", {timeZone: KST, month: "long", day: "numeric", hour: "2-digit", minute: "2-digit", hourCycle: "h23"}).format(new Date(value));
const day = (value) => new Intl.DateTimeFormat("ko-KR", {timeZone: KST, month: "long", day: "numeric"}).format(new Date(value));

function candidate(c) {
  const target = c.osm_key ? osmLink(c.osm_key) : el("span", {}, (PROVIDERS[c.provider] || c.provider) + " " + (c.source_name || c.source_id || ""));
  const facts = [];
  if (c.source_operator) facts.push(c.source_operator);
  if (typeof c.distance_m === "number") facts.push(Math.round(c.distance_m) + "m");
  const basis = (c.basis || []).filter((b) => BASIS[b]).map((b) => BASIS[b]);
  if (basis.length) facts.push(basis.join("·") + " 일치");
  const strong = c.confidence === "strong" || c.confidence === "metadata";
  return el("li", {}, el("span", {class: "cand-head"}, target, chip(strong ? ["확정", "ok"] : ["검토", "muted"])),
    facts.length ? el("span", {class: "cand-sub"}, facts.join(" · ")) : null);
}

function row(r) {
  const object = r.provider === "osm" && /^(node|way|relation)\/\d+$/.test(r.key) ? osmLink(r.key) : el("span", {class: "mono"}, r.key.split(":").pop());
  return el("tr", {},
    td(null, r.name ? el("span", {class: "cell-title"}, r.name) : el("span", {class: "muted"}, "이름 없음"),
      r.ref && r.ref !== r.name ? el("span", {class: "cell-sub"}, r.ref) : null),
    td("구분", (PROVIDERS[r.provider] || r.provider) + " " + (KINDS[r.kind] || r.kind)),
    td("객체", object),
    td("상태", chip(STATUS[r.status] || [r.status, "muted"]), r.exception ? el("span", {class: "cell-sub"}, r.exception) : null),
    td("대응", r.candidates.length ? el("details", {class: "evidence"}, el("summary", {}, "후보 " + r.candidates.length),
      el("ul", {}, r.candidates.map(candidate))) : el("span", {class: "muted"}, "—")));
}

function pager(total) {
  for (const box of document.querySelectorAll("[data-pager]")) {
    box.replaceChildren(
      el("button", {class: "btn btn-sm", type: "button", "aria-label": "이전 페이지", disabled: page === 0, onclick: () => go(page - 1)}, "‹"),
      el("span", {}, (page + 1).toLocaleString("ko-KR") + " / " + total.toLocaleString("ko-KR")),
      el("button", {class: "btn btn-sm", type: "button", "aria-label": "다음 페이지", disabled: page + 1 >= total, onclick: () => go(page + 1)}, "›"));
    box.hidden = total <= 1;
  }
}

function go(next) {
  page = next;
  render();
  $("count").scrollIntoView({block: "start", behavior: "smooth"});
}

function render() {
  const total = Math.max(1, Math.ceil(filtered.length / PAGE));
  page = Math.max(0, Math.min(page, total - 1));
  $("body").replaceChildren(...filtered.slice(page * PAGE, (page + 1) * PAGE).map(row));
  $("no-results").hidden = filtered.length > 0;
  $("count").replaceChildren(el("strong", {}, filtered.length.toLocaleString("ko-KR")), "건");
  pager(total);
  draw();
}

function apply() {
  const q = $("search").value.trim().toLowerCase();
  const [kind, side, status] = FILTERS.map((id) => $(id).value);
  const support = $("support").checked;
  filtered = [];
  for (let i = 0; i < rows.length; i++) {
    const r = rows[i];
    if (kind && r.kind !== kind) continue;
    if (side === "source" ? r.provider === "osm" : side && r.provider !== side) continue;
    if (status && r.status !== status) continue;
    if (!support && !r.in_scope && r.scope_state !== "unknown") continue;
    if (q && !haystack[i].includes(q)) continue;
    filtered.push(r);
  }
  page = 0;
  const params = new URLSearchParams(location.search);
  for (const [key, value] of [["q", q], ["kind", kind], ["side", side], ["status", status], ["support", support ? "1" : ""]]) {
    if (value) params.set(key, value);
    else params.delete(key);
  }
  history.replaceState(null, "", "?" + params);
  render();
}

function draw() {
  const canvas = $("map");
  const width = canvas.clientWidth;
  const height = canvas.clientHeight;
  const ratio = window.devicePixelRatio || 1;
  canvas.width = Math.round(width * ratio);
  canvas.height = Math.round(height * ratio);
  const ctx = canvas.getContext("2d");
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx.clearRect(0, 0, width, height);
  if (!bounds) return;
  const css = getComputedStyle(document.documentElement);
  const color = (name) => css.getPropertyValue(name).trim();
  const points = filtered.filter((r) => typeof r.lon === "number" && typeof r.lat === "number");
  const [minX, minY, maxX, maxY] = bounds;
  const k = Math.cos(((minY + maxY) / 2) * Math.PI / 180);
  const pad = 10;
  const scale = Math.min((width - 2 * pad) / ((maxX - minX) * k), (height - 2 * pad) / (maxY - minY));
  const ox = (width - (maxX - minX) * k * scale) / 2;
  const oy = (height - (maxY - minY) * scale) / 2;
  const xy = (x, y) => [ox + (x - minX) * k * scale, height - oy - (y - minY) * scale];
  ctx.strokeStyle = color("--map-boundary");
  ctx.lineWidth = 1;
  for (const ring of rings) {
    ctx.beginPath();
    ring.forEach(([x, y], i) => (i ? ctx.lineTo : ctx.moveTo).call(ctx, ...xy(x, y)));
    ctx.closePath();
    ctx.stroke();
  }
  // Few results get large dots so that they stand out on the whole region.
  const few = points.length <= 200;
  const size = few ? 7 : points.length > 20000 ? 1.5 : points.length > 2000 ? 2 : 3;
  ctx.globalAlpha = points.length > 20000 ? 0.5 : 0.8;
  const groups = [["--map-official", (r) => r.provider !== "osm" && !r.exception], ["--map-osm", (r) => r.provider === "osm" && !r.exception],
    ["--map-exception", (r) => Boolean(r.exception)]];
  for (const [name, test] of groups) {
    ctx.fillStyle = color(name);
    for (const r of points) {
      if (!test(r)) continue;
      const [x, y] = xy(r.lon, r.lat);
      if (few) {
        ctx.beginPath();
        ctx.arc(x, y, size / 2, 0, 2 * Math.PI);
        ctx.fill();
      } else {
        ctx.fillRect(x - size / 2, y - size / 2, size, size);
      }
    }
  }
  ctx.globalAlpha = 1;
}

function meta() {
  let generated = data.generated_at;
  let osm = data.osm_snapshot_at;
  const parsed = !generated && /^OSM (\S+) · 생성 (\S+)$/.exec(data.title || "");
  if (parsed) [osm, generated] = [parsed[1], parsed[2]];
  if (!generated || Number.isNaN(Date.parse(generated))) return [data.title || ""];
  const dates = [["OSM", osm], ["서울", data.source_dates && data.source_dates.seoul], ["경기", data.source_dates && data.source_dates.gbis]]
    .filter(([, value]) => value && !Number.isNaN(Date.parse(value))).map(([name, value]) => name + " " + day(value));
  return [el("time", {datetime: generated}, when(generated)), dates.length ? el("span", {}, "기준 " + dates.join(" · ")) : null];
}

async function load() {
  const params = new URLSearchParams(location.search);
  const run = params.get("run") || "latest";
  const response = await fetch("/api/reports/" + encodeURIComponent(run) + "/report-data.json");
  if (!response.ok) throw new Error(response.status === 404 ? "보고서를 찾을 수 없습니다." : "보고서를 불러오지 못했습니다.");
  data = JSON.parse(await new Response(response.body.pipeThrough(new DecompressionStream("gzip"))).text());
  rows = data.rows;
  haystack = rows.map((r) => (r.name + " " + r.ref + " " + r.key).toLowerCase());
  $("meta").replaceChildren(...meta().filter(Boolean));
  const present = new Set(rows.map((r) => r.status));
  for (const [value, [label]] of Object.entries(STATUS)) if (present.has(value)) $("status").append(el("option", {value}, label));
  for (const f of data.boundaries.features) {
    const polygons = f.geometry.type === "Polygon" ? [f.geometry.coordinates] : f.geometry.coordinates;
    for (const polygon of polygons) for (const ring of polygon) rings.push(ring);
  }
  bounds = rings.flat().reduce((b, [x, y]) => [Math.min(b[0], x), Math.min(b[1], y), Math.max(b[2], x), Math.max(b[3], y)],
    [Infinity, Infinity, -Infinity, -Infinity]);

  $("search").value = params.get("q") || "";
  for (const id of FILTERS) {
    const value = params.get(id) || "";
    if ([...$(id).options].some((o) => o.value === value)) $(id).value = value;
  }
  $("support").checked = params.get("support") === "1";
  for (const id of ["search", "support", ...FILTERS]) $(id).disabled = false;
  let timer = 0;
  $("search").addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(apply, 150); });
  for (const id of ["support", ...FILTERS]) $(id).addEventListener("change", apply);
  let frame = 0;
  window.addEventListener("resize", () => { cancelAnimationFrame(frame); frame = requestAnimationFrame(draw); });
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", draw);
  apply();
}

load().catch((e) => {
  $("meta").textContent = e.message;
  $("meta").classList.add("is-error");
});
