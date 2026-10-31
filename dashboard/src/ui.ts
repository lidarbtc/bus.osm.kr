export const DASHBOARD_HTML = `<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>서울·경기 버스 OSM 동기화</title>
<link rel="stylesheet" href="/app.css">
<script src="/app.js" defer></script>
</head>
<body>
<header>
  <h1>서울·경기 버스 OSM 동기화</h1>
  <div id="account" aria-live="polite"></div>
</header>
<main>
  <section id="status" class="banner" aria-live="polite">상태를 불러오는 중…</section>
  <section id="actions"></section>
  <section>
    <h2>비교 실행</h2>
    <div id="runs"></div>
  </section>
  <section>
    <h2>봇 변경세트</h2>
    <p class="note">이 시스템의 봇 계정이 OSM에 올린 변경세트만 표시합니다. 롤백을 요청하면 자동 작업이 함께 정지됩니다.</p>
    <div id="changesets"></div>
  </section>
  <section>
    <h2>롤백 요청</h2>
    <div id="jobs"></div>
  </section>
  <section>
    <h2>감사 기록</h2>
    <div id="audit"></div>
  </section>
</main>
<footer>지도 데이터 © <a href="https://www.openstreetmap.org/copyright">OpenStreetMap contributors</a> · 서울 공공누리 1유형 · 경기도 버스정보</footer>
</body>
</html>`;

export const APP_CSS = `:root{color-scheme:light;font-family:system-ui,-apple-system,"Noto Sans KR",sans-serif;color:#18303f;background:#f3f6f8}
body{margin:0}header,main,footer{max-width:1180px;margin:auto;padding:0 20px}
header{display:flex;justify-content:space-between;align-items:center;gap:16px;flex-wrap:wrap;padding-top:24px}
h1{font-size:24px;margin:0}h2{font-size:18px;margin:28px 0 10px}
.banner{margin-top:20px;padding:18px 20px;border-radius:10px;background:#e3f2e8;border:1px solid #9dcdac;line-height:1.6}
.banner.stopped{background:#fde8e6;border-color:#e59a90}.banner strong{font-size:18px}
.note,.muted{color:#506572;font-size:14px}.panel{background:#fff;border:1px solid #d6e0e6;border-radius:10px;padding:16px;margin-top:16px}
table{width:100%;border-collapse:collapse;background:#fff;border-radius:10px;overflow:hidden;font-size:14px}
th,td{text-align:left;padding:10px 12px;border-bottom:1px solid #e3e9ed;vertical-align:top}th{background:#e8eef2;font-weight:600}
.table{overflow-x:auto}.empty{padding:16px;background:#fff;border-radius:10px;color:#506572}
button,.button{font:inherit;padding:8px 14px;border-radius:7px;border:1px solid #9bb0bc;background:#fff;color:#18303f;cursor:pointer;text-decoration:none;display:inline-block}
button.danger{background:#b3261e;border-color:#b3261e;color:#fff}button.primary{background:#1a5f8a;border-color:#1a5f8a;color:#fff}
button:disabled{opacity:.55;cursor:not-allowed}textarea{width:100%;box-sizing:border-box;font:inherit;padding:8px;border-radius:7px;border:1px solid #9bb0bc;min-height:64px;margin:8px 0}
.links a{margin-right:10px}.error{color:#a3201a;margin-top:8px}.badge{display:inline-block;padding:2px 8px;border-radius:999px;background:#e8eef2;font-size:13px}
footer{padding:32px 20px;color:#506572;font-size:13px}a{color:#145b8a}`;

export const APP_JS = `"use strict";
const STATUS = {running:"실행 중",completed:"완료",completed_with_warnings:"완료(경고 있음)",failed:"실패",skipped_stopped:"정지로 건너뜀",
  queued:"대기",needs_manual:"수동 처리 필요",applied:"반영됨",reverted:"되돌림"};
const ACTIONS = {emergency_stop:"비상 정지",resume:"재개",rollback_requested:"롤백 요청",rollback_completed:"롤백 완료",
  rollback_needs_manual:"롤백 수동 처리 필요",rollback_failed:"롤백 실패"};
let me = {user:null}, state = null;

function el(tag, props, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (key === "class") node.className = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (value !== null && value !== undefined && value !== false) node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) if (child !== null && child !== undefined) node.append(child instanceof Node ? child : String(child));
  return node;
}
const when = (value) => value ? new Date(value).toLocaleString("ko-KR", {timeZone: "Asia/Seoul"}) : "—";
const label = (map, value) => map[value] || value;

function table(headers, rows, empty) {
  if (!rows.length) return el("div", {class: "empty"}, empty);
  return el("div", {class: "table"}, el("table", {}, el("thead", {}, el("tr", {}, headers.map((h) => el("th", {}, h)))),
    el("tbody", {}, rows.map((cells) => el("tr", {}, cells.map((c) => el("td", {}, c)))))));
}

async function api(path, body) {
  const response = await fetch(path, {method: "POST", headers: {"Content-Type": "application/json", "X-CSRF-Token": me.csrf || ""},
    body: JSON.stringify(body || {}), credentials: "same-origin"});
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || "요청에 실패했습니다.");
  return data;
}

function reasonForm(title, buttonText, buttonClass, onSubmit, help) {
  const area = el("textarea", {maxlength: "500", placeholder: "사유 (3~500자, 공개 감사 기록에 남습니다)", "aria-label": title + " 사유"});
  const error = el("div", {class: "error", role: "alert"});
  const button = el("button", {class: buttonClass, type: "submit"}, buttonText);
  const form = el("form", {class: "panel"}, el("strong", {}, title), help ? el("p", {class: "note"}, help) : null, area, button, error);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    error.textContent = "";
    button.disabled = true;
    try { await onSubmit(area.value.trim()); await load(); }
    catch (e) { error.textContent = e.message; }
    finally { button.disabled = false; }
  });
  return form;
}

function renderAccount() {
  const box = document.getElementById("account");
  box.replaceChildren();
  if (!me.user) {
    if (state && state.config.oauth_configured) box.append(el("a", {class: "button primary", href: "/auth/login"}, "OSM 계정으로 로그인"));
    else box.append(el("span", {class: "muted"}, "OSM 로그인 설정 전"));
    return;
  }
  const need = state ? state.config.min_korea_changesets : 10;
  const right = me.eligible ? "정지·롤백 가능" : "한국 변경세트 " + need + "개 이상 필요";
  box.append(el("span", {}, me.user.name + " · 한국 변경세트 " + me.korea_changesets + "개 확인 · " + right + (me.is_admin ? " · 운영자" : "") + " "),
    el("button", {type: "button", onclick: async () => { await api("/auth/logout"); me = {user: null}; await load(); }}, "로그아웃"));
}

function renderStatus() {
  const c = state.control;
  const box = document.getElementById("status");
  box.className = "banner" + (c.stopped ? " stopped" : "");
  const seen = c.runner_seen_at ? "Oracle 실행기 마지막 확인: " + when(c.runner_seen_at) + (c.runner_version ? " (" + c.runner_version + ")" : "") : "Oracle 실행기가 아직 연결되지 않았습니다.";
  box.replaceChildren(
    el("strong", {}, c.stopped ? "비상 정지됨 — 자동 작업을 실행하지 않습니다" : "자동 작업 동작 중"),
    el("div", {}, c.actor_name ? (c.stopped ? "정지: " : "최근 재개: ") + c.actor_name + " · " + when(c.updated_at) + (c.reason ? " · 사유: " + c.reason : "") : ""),
    el("div", {class: "note"}, "현재 OSM 업로드는 비활성화되어 있으며 읽기 전용 비교만 실행합니다. " + seen));
  const actions = document.getElementById("actions");
  actions.replaceChildren();
  if (me.user && me.eligible && !c.stopped) {
    actions.append(reasonForm("비상 정지", "비상 정지", "danger", (reason) => api("/api/stop", {reason}),
      "정지하면 예정된 비교와 이후 자동 편집이 실행되지 않습니다. 재개는 운영자만 할 수 있습니다."));
  }
  if (me.user && me.is_admin && c.stopped) actions.append(reasonForm("자동 작업 재개", "재개", "primary", (reason) => api("/api/resume", {reason})));
}

function summaryCell(run) {
  const s = run.summary || {};
  const parts = [];
  if (s.error) parts.push(el("div", {class: "error"}, s.error));
  if (s.osm_snapshot_at) parts.push(el("div", {}, "OSM 기준 " + when(s.osm_snapshot_at)));
  if (s.source_dates) parts.push(el("div", {}, Object.entries(s.source_dates).map(([k, v]) => k + " " + v).join(" · ")));
  if (s.headline) parts.push(el("div", {}, Object.entries(s.headline).map(([k, v]) => k + " " + Number(v).toLocaleString("ko-KR")).join(" · ")));
  if (typeof s.warning_count === "number") parts.push(el("div", {class: "note"}, "주의 사항 " + s.warning_count + "건"));
  return parts.length ? parts : "—";
}

function renderRuns() {
  const rows = state.runs.map((run) => {
    const files = run.files || [];
    const links = el("div", {class: "links"}, ["report.html", "summary.md", "comparison.json", "stops.csv", "routes.csv"].filter((f) => files.includes(f))
      .map((f) => el("a", {href: "/reports/" + encodeURIComponent(run.id) + "/" + f, target: "_blank", rel: "noopener"}, f)));
    return [when(run.started_at), el("span", {class: "badge"}, label(STATUS, run.status)), run.code_version || "—", summaryCell(run), links];
  });
  document.getElementById("runs").replaceChildren(table(["시작", "상태", "코드", "요약", "보고서"], rows, "아직 실행 기록이 없습니다."));
}

function renderChangesets() {
  const active = new Set(state.rollback_jobs.filter((j) => j.status === "queued" || j.status === "running").map((j) => j.changeset_id));
  const rows = state.bot_changesets.map((cs) => {
    let action = "—";
    if (me.user && me.eligible && cs.status === "applied" && !active.has(cs.id)) {
      action = el("button", {type: "button", class: "danger", onclick: () => {
        const holder = document.getElementById("rollback-" + cs.id);
        holder.replaceChildren(reasonForm("변경세트 " + cs.id + " 롤백", "롤백 요청", "danger", (reason) => api("/api/rollback", {changeset_id: cs.id, reason}),
          "이후 다른 사람이 같은 객체를 고쳤다면 자동 롤백하지 않고 수동 처리로 표시합니다."));
      }}, "롤백");
    } else if (active.has(cs.id)) action = "롤백 진행 중";
    return [el("a", {href: "https://www.openstreetmap.org/changeset/" + cs.id, target: "_blank", rel: "noopener"}, String(cs.id)),
      when(cs.created_at), cs.kind === "revert" ? "롤백" : "동기화", cs.comment || "—", label(STATUS, cs.status), el("div", {}, action, el("div", {id: "rollback-" + cs.id}))];
  });
  document.getElementById("changesets").replaceChildren(table(["변경세트", "생성", "종류", "설명", "상태", "작업"], rows,
    "봇 변경세트가 없습니다. OSM 업로드는 아직 비활성화되어 있습니다."));
}

function renderJobs() {
  const rows = state.rollback_jobs.map((j) => ["#" + j.id, String(j.changeset_id), label(STATUS, j.status), j.requested_by_name, when(j.requested_at), j.reason,
    j.result ? el("details", {}, el("summary", {}, "결과"), el("pre", {}, JSON.stringify(j.result, null, 2))) : "—"]);
  document.getElementById("jobs").replaceChildren(table(["요청", "변경세트", "상태", "요청자", "시각", "사유", "결과"], rows, "롤백 요청이 없습니다."));
}

function renderAudit() {
  const rows = state.audit.map((a) => [when(a.at), a.actor_uid ? el("a", {href: "https://www.openstreetmap.org/user/" + encodeURIComponent(a.actor_name), target: "_blank", rel: "noopener"}, a.actor_name) : a.actor_name,
    label(ACTIONS, a.action), a.target || "—", a.reason || "—"]);
  document.getElementById("audit").replaceChildren(table(["시각", "계정", "작업", "대상", "사유"], rows, "기록이 없습니다."));
}

async function load() {
  const [s, m] = await Promise.all([fetch("/api/state").then((r) => r.json()), fetch("/api/me", {credentials: "same-origin"}).then((r) => r.json())]);
  state = s; me = m;
  renderAccount(); renderStatus(); renderRuns(); renderChangesets(); renderJobs(); renderAudit();
}
load().catch((e) => { document.getElementById("status").textContent = "상태를 불러오지 못했습니다: " + e.message; });
`;
