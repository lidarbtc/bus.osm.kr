"use strict";
const RUN_STATUS = {running: ["진행 중", "info"], completed: ["완료", "ok"], completed_with_warnings: ["완료", "ok"],
  failed: ["실패", "danger"], skipped_stopped: ["건너뜀", "muted"]};
const JOB_STATUS = {queued: ["대기", "info"], running: ["진행 중", "info"], completed: ["완료", "ok"],
  needs_manual: ["수동 처리 필요", "warn"], failed: ["실패", "danger"]};
const CHANGESET_STATUS = {applied: ["반영", "ok"], reverted: ["되돌림", "muted"]};
const CONFLICTS = {edited_after_bot: "봇 이후 다시 편집됨", no_visible_previous_version: "되돌릴 이전 버전 없음"};
const ACTIONS = {emergency_stop: "비상 정지", resume: "재개", rollback_requested: "롤백 요청", rollback_completed: "롤백 완료",
  rollback_needs_manual: "롤백 수동 처리", rollback_failed: "롤백 실패"};
const FILES = [
  ["ref-changes.osc", "번호 추가 변경 (JOSM)"],
  ["ref-tasks.geojson", "매퍼 확인 작업"],
  ["ref-plan.json", "번호 추가 근거"],
  ["stops.csv", "정류소 비교"],
  ["routes.csv", "노선 비교"],
  ["stops.geojson", "정류소 위치"],
  ["comparison.json", "전체 비교 결과"],
  ["summary.md", "요약"],
];
const RUNS_SHOWN = 6;
const STALE_MS = 8 * 3600e3;
const KST = "Asia/Seoul";
let me = {user: null};
let state = null;
let openForm = null;
let showAllRuns = false;

const $ = (id) => document.getElementById(id);

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

const yearOf = (date) => new Intl.DateTimeFormat("ko-KR", {timeZone: KST, year: "numeric"}).format(date);
const THIS_YEAR = yearOf(new Date());
function when(value) {
  if (!value) return "—";
  const date = new Date(value);
  return new Intl.DateTimeFormat("ko-KR", {timeZone: KST, year: yearOf(date) === THIS_YEAR ? undefined : "numeric",
    month: "long", day: "numeric", hour: "2-digit", minute: "2-digit", hourCycle: "h23"}).format(date);
}
const day = (value) => new Intl.DateTimeFormat("ko-KR", {timeZone: KST, month: "long", day: "numeric"}).format(new Date(value));
function ago(value) {
  const hours = Math.round((Date.now() - Date.parse(value)) / 3600e3);
  const format = new Intl.RelativeTimeFormat("ko", {numeric: "auto"});
  return hours < 48 ? format.format(-hours, "hour") : format.format(-Math.round(hours / 24), "day");
}
const num = (value) => typeof value === "number" ? value.toLocaleString("ko-KR") : "—";
const chip = ([text, tone]) => el("span", {class: "chip chip-" + tone}, text);
const badge = (map, value) => chip(map[value] || [value, "muted"]);
const external = (href, text, cls) => el("a", {href, target: "_blank", rel: "noopener", class: cls}, text);
const osmUser = (name) => external("https://www.openstreetmap.org/user/" + encodeURIComponent(name), name);
const changesetLink = (id) => external("https://www.openstreetmap.org/changeset/" + id, String(id), "mono");
const osmObject = (key) => /^(node|way|relation)\/\d+$/.test(key) ? external("https://www.openstreetmap.org/" + key, key, "mono") : String(key);
const errorText = (message) => String(message).replace(/^\w+(Error|Exception): /, "");
const fileUrl = (run, name) => "/api/reports/" + encodeURIComponent(run) + "/" + name;
const reportUrl = (run, params) => "/report.html?" + new URLSearchParams({run, ...params});
const td = (label, ...content) => el("td", label ? {"data-label": label} : {}, ...content);

function table(headers, rows) {
  return el("div", {class: "card table-card"}, el("div", {class: "table-wrap"}, el("table", {class: "data-table align-middle"},
    el("thead", {}, el("tr", {}, headers.map((h) => el("th", {}, h)))), el("tbody", {}, rows))));
}

async function api(path, body) {
  const response = await fetch(path, {method: "POST", credentials: "same-origin",
    headers: {"Content-Type": "application/json", "X-CSRF-Token": me.csrf || ""}, body: JSON.stringify(body || {})});
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || "요청에 실패했습니다.");
  if (data.state) state = data.state;
  return data;
}

function reasonForm(label, submitText, tone, submit) {
  const area = el("textarea", {maxlength: "500", rows: "3", placeholder: "사유", "aria-label": label + " 사유", required: true});
  const error = el("p", {class: "form-error", role: "alert"});
  const button = el("button", {class: "btn btn-" + tone, type: "submit"}, submitText);
  const cancel = el("button", {class: "btn btn-ghost", type: "button", onclick: () => { openForm = null; renderAll(); }}, "취소");
  const form = el("form", {class: "reason-form"}, area, el("div", {class: "form-actions"}, button, cancel), error);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    error.textContent = "";
    button.disabled = true;
    try {
      await submit(area.value.trim());
      openForm = null;
      renderAll();
    } catch (e) {
      error.textContent = e.message;
      button.disabled = false;
    }
  });
  setTimeout(() => area.focus(), 0);
  return form;
}

function open(form) {
  openForm = form;
  renderAll();
}

async function logout() {
  await api("/api/auth/logout").catch(() => undefined);
  me = {user: null};
  openForm = null;
  renderAll();
}

function renderAccount() {
  const box = $("account");
  box.replaceChildren();
  if (me.user) {
    box.append(el("details", {class: "menu align-end"},
      el("summary", {class: "btn btn-sm btn-ghost", title: me.user.name},
        el("span", {class: "account-name"}, me.user.name), me.is_admin ? chip(["운영자", "info"]) : null),
      el("div", {class: "menu-list"},
        external("https://www.openstreetmap.org/user/" + encodeURIComponent(me.user.name), "OSM 프로필"),
        el("button", {type: "button", onclick: logout}, "로그아웃"))));
  } else if (state.config.oauth_configured) {
    box.append(el("a", {class: "btn btn-sm", href: "/api/auth/login"}, "OSM 로그인"));
  }
}

function renderStatus() {
  const c = state.control;
  const lines = [];
  if (c.stopped) {
    lines.push(el("span", {}, [c.actor_name, when(c.updated_at)].filter(Boolean).join(" · ")));
  } else {
    const last = state.runs[0];
    if (last && last.status === "failed") lines.push(el("span", {class: "warn"}, "최근 비교 실패 · " + when(last.started_at)));
    else if (last && last.status === "running") lines.push(el("span", {}, "비교 진행 중 · " + when(last.started_at)));
    else if (last) lines.push(el("span", {}, "최근 비교 " + when(last.started_at)));
  }
  if (!c.runner_seen_at) lines.push(el("span", {class: "warn"}, "실행기 연결 전"));
  else if (Date.now() - Date.parse(c.runner_seen_at) > STALE_MS) lines.push(el("span", {class: "warn"}, "실행기 응답 없음 · 마지막 응답 " + ago(c.runner_seen_at)));

  const actions = [];
  let form = null;
  if (c.stopped) {
    if (me.is_admin) {
      if (openForm === "resume") form = reasonForm("재개", "재개", "primary", (reason) => api("/api/resume", {reason}));
      else actions.push(el("button", {class: "btn btn-primary", type: "button", onclick: () => open("resume")}, "재개"));
    }
  } else if (me.user && me.eligible) {
    if (openForm === "stop") form = reasonForm("비상 정지", "정지", "danger", (reason) => api("/api/stop", {reason}));
    else actions.push(el("button", {class: "btn btn-danger", type: "button", onclick: () => open("stop")}, "비상 정지"));
  } else if (me.user) {
    actions.push(el("span", {class: "hint"}, "비상 정지: 한국 변경세트 " + state.config.min_korea_changesets + "개 이상 필요"));
  } else if (state.config.oauth_configured) {
    actions.push(el("a", {class: "btn btn-danger-outline", href: "/api/auth/login", title: "OSM 로그인 후 정지",
      onclick: () => sessionStorage.setItem("intent", "stop")}, "비상 정지"));
  }

  const box = $("status");
  box.className = "card status" + (c.stopped ? " is-stopped" : "");
  box.replaceChildren(...[
    el("div", {class: "status-main"},
      el("p", {class: "status-title"}, el("span", {class: "status-dot", "aria-hidden": "true"}), c.stopped ? "비상 정지됨" : "자동 작업 동작 중"),
      lines.length ? el("p", {class: "status-lines"}, lines) : null,
      c.stopped && c.reason ? el("p", {class: "status-reason"}, c.reason) : null),
    actions.length ? el("div", {class: "status-actions"}, actions) : null,
    form,
  ].filter(Boolean));
}

function filesMenu(run, label, end) {
  const files = run.files || [];
  const items = FILES.filter(([name]) => files.includes(name));
  if (!items.length) return null;
  return el("details", {class: "menu" + (end ? " align-end" : "")},
    el("summary", {class: end ? "btn btn-sm btn-ghost" : "btn"}, label),
    el("div", {class: "menu-list"}, items.map(([name, text]) =>
      el("a", {href: fileUrl(run.id, name)}, el("span", {}, text), el("small", {}, name + ".gz")))));
}

function metric(label, value, href) {
  const body = [el("span", {class: "metric-label"}, label), el("span", {class: "metric-value"}, value)];
  return href ? el("a", {class: "metric", href}, body) : el("div", {class: "metric"}, body);
}

function renderLatest() {
  const run = state.runs.find((r) => (r.status === "completed" || r.status === "completed_with_warnings")
    && (r.files || []).includes("report-data.json"));
  const meta = $("latest-meta");
  meta.replaceChildren();
  if (!run) {
    $("latest").replaceChildren(el("div", {class: "card empty"}, "아직 비교 결과가 없습니다."));
    return;
  }
  const s = run.summary || {};
  const h = s.headline || {};
  const dates = [["OSM", s.osm_snapshot_at], ["서울", s.source_dates && s.source_dates.seoul], ["경기", s.source_dates && s.source_dates.gbis]]
    .filter(([, value]) => value).map(([name, value]) => name + " " + day(value));
  meta.append(...[el("time", {datetime: run.started_at}, when(run.started_at)), dates.length ? el("span", {}, "기준 " + dates.join(" · ")) : null].filter(Boolean));
  const link = (params) => reportUrl(run.id, params);
  const plan = s.ref_plan;
  $("latest").replaceChildren(el("div", {class: "card"},
    el("div", {class: "latest-metrics"},
      el("div", {class: "metrics metrics-4"},
        metric("대응된 OSM 정류소", num(h["OSM 정류소 대응"]), link({kind: "stop", side: "osm", status: "matched"})),
        metric("공식 데이터에 없는 OSM 정류소", num(h["OSM 정류소 대응 미확인"]), link({kind: "stop", side: "osm", status: "osm_unmatched"})),
        metric("OSM에 없는 공식 정류소", num(h["공식 정류소 대응 미확인"]), link({kind: "stop", side: "source", status: "source_unmatched"})),
        metric("OSM에 없는 공식 노선", num(h["공식 노선 대응 미확인"]), link({kind: "route", side: "source", status: "source_unmatched"}))),
      plan ? el("div", {class: "metrics metrics-3"},
        metric("번호 추가 대상", num(plan.changes)),
        metric("매퍼 확인 필요", num(plan.tasks)),
        metric("기존 번호 일치율", typeof plan.agreement === "number" ? (plan.agreement * 100).toFixed(2) + "%" : "—")) : null),
    el("div", {class: "card-foot"}, el("a", {class: "btn btn-primary", href: reportUrl(run.id)}, "보고서 보기"), filesMenu(run, "파일"))));
}

function renderRuns() {
  const runs = state.runs;
  if (!runs.length) {
    $("runs").replaceChildren(el("div", {class: "card empty"}, "아직 실행 기록이 없습니다."));
    return;
  }
  const items = (showAllRuns ? runs : runs.slice(0, RUNS_SHOWN)).map((run) => {
    const s = run.summary || {};
    const files = run.files || [];
    const note = s.error ? el("span", {class: "run-note is-error", title: s.error}, errorText(s.error))
      : s.stop_reason ? el("span", {class: "run-note", title: s.stop_reason}, s.stop_reason)
        : run.files_expired ? el("span", {class: "run-note"}, "파일 만료") : null;
    return el("li", {class: "run"},
      el("time", {class: "run-time", datetime: run.started_at}, when(run.started_at)),
      el("div", {class: "run-body"}, badge(RUN_STATUS, run.status), note),
      el("div", {class: "run-links"},
        files.includes("report-data.json") ? el("a", {class: "btn btn-sm btn-ghost", href: reportUrl(run.id)}, "보고서") : null,
        filesMenu(run, "파일", true)));
  });
  $("runs").replaceChildren(el("div", {class: "card"}, el("ol", {class: "runs"}, items),
    runs.length > RUNS_SHOWN ? el("button", {class: "more", type: "button", onclick: () => { showAllRuns = !showAllRuns; renderRuns(); }},
      showAllRuns ? "접기" : "전체 " + runs.length + "개 보기") : null));
}

function renderChangesets() {
  const list = state.bot_changesets;
  if (!list.length) {
    $("changesets").replaceChildren(el("div", {class: "card empty"}, "아직 없습니다."));
    return;
  }
  const active = new Set(state.rollback_jobs.filter((j) => j.status === "queued" || j.status === "running").map((j) => j.changeset_id));
  const rows = list.flatMap((cs) => {
    const key = "rollback:" + cs.id;
    let action = null;
    if (active.has(cs.id)) action = chip(["롤백 진행 중", "info"]);
    else if (cs.status === "applied" && me.user && me.eligible && openForm !== key) {
      action = el("button", {class: "btn btn-sm btn-danger-outline", type: "button", onclick: () => open(key)}, "롤백");
    }
    const row = el("tr", {},
      td("변경세트", changesetLink(cs.id)),
      td("시각", when(cs.created_at)),
      td("내용", cs.kind === "revert" ? [chip(["롤백", "muted"]), " "] : null, cs.comment || "—"),
      td("상태", badge(CHANGESET_STATUS, cs.status)),
      td(null, action));
    if (openForm !== key) return [row];
    return [row, el("tr", {class: "form-row"}, el("td", {colspan: "5"},
      reasonForm("변경세트 " + cs.id + " 롤백", "롤백 요청", "danger", (reason) => api("/api/rollback", {changeset_id: cs.id, reason}))))];
  });
  $("changesets").replaceChildren(table(["변경세트", "시각", "내용", "상태", ""], rows));
}

function jobResult(result) {
  if (!result) return null;
  if (result.error) return el("span", {class: "cell-sub"}, errorText(result.error));
  const count = (list) => Array.isArray(list) ? list.length : Number(list) || 0;
  const conflicts = Array.isArray(result.conflicts) ? result.conflicts : [];
  return [
    result.revert_changeset ? el("span", {}, "되돌림 ", changesetLink(result.revert_changeset)) : null,
    el("span", {class: "cell-sub"}, "복원 " + num(count(result.restore)) + " · 삭제 " + num(count(result.delete))),
    conflicts.length ? el("details", {class: "evidence"}, el("summary", {}, "겹치는 편집 " + conflicts.length + "곳"),
      el("ul", {}, conflicts.map((c) => el("li", {}, osmObject(c.element), el("span", {class: "cand-sub"}, CONFLICTS[c.reason] || c.reason))))) : null,
  ];
}

function renderJobs() {
  const jobs = state.rollback_jobs;
  $("jobs-section").hidden = !jobs.length;
  if (!jobs.length) return;
  $("jobs").replaceChildren(table(["요청", "변경세트", "상태", "요청자", "사유", "결과"], jobs.map((j) => el("tr", {},
    td("요청", el("span", {class: "cell-title"}, "#" + j.id), el("span", {class: "cell-sub"}, when(j.requested_at))),
    td("변경세트", changesetLink(j.changeset_id)),
    td("상태", badge(JOB_STATUS, j.status)),
    td("요청자", osmUser(j.requested_by_name)),
    td("사유", j.reason),
    td("결과", jobResult(j.result))))));
}

function renderAudit() {
  const entries = state.audit;
  if (!entries.length) {
    $("audit").replaceChildren(el("div", {class: "card empty"}, "아직 없습니다."));
    return;
  }
  $("audit").replaceChildren(table(["시각", "계정", "작업", "사유"], entries.map((a) => {
    const target = /^changeset\/(\d+)$/.exec(a.target || "");
    return el("tr", {},
      td("시각", when(a.at)),
      td("계정", a.actor_uid ? osmUser(a.actor_name) : a.actor_name),
      td("작업", ACTIONS[a.action] || a.action, target ? [" ", changesetLink(target[1])] : null),
      td("사유", a.reason || "—"));
  })));
}

function renderAll() {
  renderAccount();
  renderStatus();
  renderLatest();
  renderRuns();
  renderChangesets();
  renderJobs();
  renderAudit();
}

async function getJson(path) {
  const response = await fetch(path, {credentials: "same-origin"});
  if (!response.ok) throw new Error(String(response.status));
  return response.json();
}

async function load() {
  [state, me] = await Promise.all([getJson("/api/state"), getJson("/api/me")]);
  if (sessionStorage.getItem("intent") === "stop") {
    sessionStorage.removeItem("intent");
    if (me.user && me.eligible && !state.control.stopped) openForm = "stop";
  }
  renderAll();
}

document.addEventListener("click", (event) => {
  for (const menu of document.querySelectorAll("details.menu[open]")) if (!menu.contains(event.target)) menu.open = false;
});
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  for (const menu of document.querySelectorAll("details.menu[open]")) {
    menu.open = false;
    menu.querySelector("summary").focus();
  }
});

load().catch(() => {
  const box = $("status");
  box.className = "card status";
  box.replaceChildren(el("p", {class: "status-title"}, "상태를 불러오지 못했습니다."),
    el("button", {class: "btn btn-sm", type: "button", onclick: () => location.reload()}, "다시 시도"));
});
