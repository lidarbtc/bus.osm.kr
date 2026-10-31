import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { after, before, beforeEach, test } from "node:test";
import { gunzipSync, gzipSync } from "node:zlib";
import { Miniflare } from "miniflare";

const ORIGIN = "https://dash.test";
const RUNNER = { Authorization: "Bearer runner-secret" };
const osmCalls = [];
// uid -> { changesets, active blocks }
const OSM_USERS = {
  1001: { name: "seoul-mapper", blocks: 0, inside: 10, outside: 30 },
  1002: { name: "almost", blocks: 0, inside: 9, outside: 0 },
  1003: { name: "blocked", blocks: 1, inside: 50, outside: 0 },
  1004: { name: "operator", blocks: 0, inside: 12, outside: 0 },
};
let nextLogin = 1001;

function changesets(uid) {
  const { inside, outside } = OSM_USERS[uid];
  const list = [];
  const box = (lat, lon) => ({ min_lat: lat, max_lat: lat + 0.01, min_lon: lon, max_lon: lon + 0.01 });
  // Tokyo and Pyongyang lie inside the query bbox but outside South Korea.
  for (let i = 0; i < outside; i++) list.push({ id: uid * 1000 + i, changes_count: 3, created_at: "2026-10-01T00:00:00Z", ...(i % 2 ? box(35.68, 139.7) : box(39.03, 125.75)) });
  for (let i = 0; i < inside; i++) list.push({ id: uid * 1000 + 500 + i, changes_count: 1, created_at: "2026-09-01T00:00:00Z", ...box(37.56, 126.97) });
  list.push({ id: uid * 1000 + 900, changes_count: 0, created_at: "2026-08-01T00:00:00Z", ...box(37.56, 126.97) });
  return list;
}

async function fakeOsm(request) {
  const url = new URL(request.url);
  osmCalls.push(url.pathname);
  if (url.pathname === "/oauth2/token") {
    const body = new URLSearchParams(await request.text());
    assert.equal(body.get("client_secret"), "client-secret");
    assert.ok(body.get("code_verifier").length >= 43);
    return Response.json({ access_token: "token-" + nextLogin });
  }
  if (url.pathname === "/oauth2/revoke") return new Response("");
  if (url.pathname === "/api/0.6/user/details.json") {
    const uid = Number(request.headers.get("Authorization").replace("Bearer token-", ""));
    return Response.json({ user: { id: uid, display_name: OSM_USERS[uid].name, blocks: { received: { active: OSM_USERS[uid].blocks } } } });
  }
  if (url.pathname === "/api/0.6/changesets.json") {
    assert.equal(url.searchParams.get("closed"), "true");
    return Response.json({ changesets: changesets(Number(url.searchParams.get("user"))) });
  }
  return new Response("unexpected " + url.pathname, { status: 500 });
}

let mf;
before(async () => {
  mf = new Miniflare({
    modules: true,
    scriptPath: "dist/index.js",
    compatibilityDate: "2026-07-15",
    d1Databases: { DB: "test-db" },
    r2Buckets: ["REPORTS"],
    bindings: {
      OSM_WEB: "https://osm.test", OSM_API: "https://api.osm.test", MIN_KOREA_CHANGESETS: "10", ADMIN_OSM_UIDS: "1004",
      SESSION_HOURS: "12", REPORT_RETENTION_DAYS: "120", OSM_CLIENT_ID: "client", OSM_CLIENT_SECRET: "client-secret", RUNNER_TOKEN: "runner-secret",
    },
    outboundService: fakeOsm,
  });
});
after(() => mf.dispose());

beforeEach(async () => {
  const db = await mf.getD1Database("DB");
  for (const table of ["rollback_jobs", "bot_changesets", "runs", "oauth_states", "sessions", "audit_log", "control"]) {
    await db.prepare(`DROP TABLE IF EXISTS ${table}`).run();
  }
  const sql = readFileSync("migrations/0001_init.sql", "utf8").replace(/^--.*$/gm, "");
  for (const statement of sql.split(";").map((s) => s.trim()).filter(Boolean)) await db.prepare(statement).run();
  osmCalls.length = 0;
});

const get = (path, headers = {}) => mf.dispatchFetch(ORIGIN + path, { headers, redirect: "manual" });
const send = (method, path, body, headers = {}) =>
  mf.dispatchFetch(ORIGIN + path, { method, headers: { "Content-Type": "application/json", ...headers }, body: JSON.stringify(body) });

async function login(uid) {
  nextLogin = uid;
  const start = await get("/auth/login");
  assert.equal(start.status, 302);
  const authorize = new URL(start.headers.get("Location"));
  assert.equal(authorize.origin + authorize.pathname, "https://osm.test/oauth2/authorize");
  assert.equal(authorize.searchParams.get("code_challenge_method"), "S256");
  assert.equal(authorize.searchParams.get("scope"), "read_prefs");
  const stateCookie = start.headers.get("Set-Cookie").split(";")[0];
  const done = await get(`/auth/callback?code=abc&state=${authorize.searchParams.get("state")}`, { Cookie: stateCookie });
  assert.equal(done.status, 302, await done.clone().text());
  const session = done.headers.getSetCookie().find((c) => c.startsWith("__Host-session="));
  const cookie = session.split(";")[0];
  const me = await (await get("/api/me", { Cookie: cookie })).json();
  return { cookie, me, headers: { Cookie: cookie, Origin: ORIGIN, "X-CSRF-Token": me.csrf } };
}

test("OSM login counts only non-empty changesets centred in South Korea and revokes the token", async () => {
  const eligible = await login(1001);
  assert.deepEqual([eligible.me.user.name, eligible.me.korea_changesets, eligible.me.eligible, eligible.me.is_admin], ["seoul-mapper", 10, true, false]);
  assert.ok(osmCalls.includes("/oauth2/revoke"));
  const short = await login(1002);
  assert.deepEqual([short.me.korea_changesets, short.me.eligible], [9, false]);
  const blocked = await login(1003);
  assert.equal(blocked.me.eligible, false);
});

test("login rejects a callback whose state does not match the browser cookie", async () => {
  const start = await get("/auth/login");
  const stateValue = new URL(start.headers.get("Location")).searchParams.get("state");
  const forged = await get(`/auth/callback?code=abc&state=${stateValue}`, { Cookie: "__Host-oauth-state=attacker" });
  assert.equal(forged.status, 400);
  assert.ok(!osmCalls.includes("/oauth2/token"));
});

test("emergency stop requires an eligible account, same origin and CSRF token; resume requires an operator", async () => {
  assert.equal((await send("POST", "/api/stop", { reason: "잘못된 편집" }, { Origin: ORIGIN })).status, 401);
  const short = await login(1002);
  assert.equal((await send("POST", "/api/stop", { reason: "잘못된 편집" }, short.headers)).status, 403);
  const mapper = await login(1001);
  assert.equal((await send("POST", "/api/stop", { reason: "잘못된 편집" }, { ...mapper.headers, Origin: "https://evil.test" })).status, 403);
  assert.equal((await send("POST", "/api/stop", { reason: "잘못된 편집" }, { ...mapper.headers, "X-CSRF-Token": "wrong" })).status, 403);
  assert.equal((await send("POST", "/api/stop", { reason: "x" }, mapper.headers)).status, 400);
  assert.equal((await send("POST", "/api/stop", { reason: "정류장 이름이 잘못 바뀜" }, mapper.headers)).status, 200);
  assert.equal((await send("POST", "/api/stop", { reason: "중복 정지" }, mapper.headers)).status, 409);

  const control = await (await get("/api/runner/control", RUNNER)).json();
  assert.deepEqual([control.stopped, control.reason], [true, "정류장 이름이 잘못 바뀜"]);
  assert.equal((await send("POST", "/api/resume", { reason: "확인 완료" }, mapper.headers)).status, 403);
  const operator = await login(1004);
  assert.equal((await send("POST", "/api/resume", { reason: "원인 수정 후 재개" }, operator.headers)).status, 200);
  const state = await (await get("/api/state")).json();
  assert.equal(state.control.stopped, 0);
  assert.deepEqual(state.audit.map((a) => [a.actor_name, a.action]), [["operator", "resume"], ["seoul-mapper", "emergency_stop"]]);
});

test("rollback is limited to ledger changesets, stops automation and is completed by the runner", async () => {
  const mapper = await login(1001);
  assert.equal((await send("POST", "/api/rollback", { changeset_id: 555, reason: "남의 편집" }, mapper.headers)).status, 404);
  assert.equal((await send("POST", "/api/runner/changesets", { id: 777, run_id: "20261031T000000Z", comment: "버스 정류장 이름 갱신" }, RUNNER)).status, 200);
  const requested = await send("POST", "/api/rollback", { changeset_id: 777, reason: "이름 오류" }, mapper.headers);
  assert.equal(requested.status, 200);
  const { job_id } = await requested.json();
  assert.equal((await send("POST", "/api/rollback", { changeset_id: 777, reason: "중복 요청" }, mapper.headers)).status, 409);

  const control = await (await get("/api/runner/control", RUNNER)).json();
  assert.equal(control.stopped, true);
  assert.deepEqual(control.rollback_jobs.map((j) => [j.id, j.changeset_id]), [[job_id, 777]]);
  assert.equal((await send("POST", `/api/runner/jobs/${job_id}`, { status: "completed" }, RUNNER)).status, 409);
  assert.equal((await send("POST", `/api/runner/jobs/${job_id}`, { status: "running" }, RUNNER)).status, 200);
  assert.equal((await send("POST", `/api/runner/jobs/${job_id}`, { status: "completed", result: { reverted: 3 }, revert_changeset: { id: 778, comment: "Revert 777" } }, RUNNER)).status, 200);
  const state = await (await get("/api/state")).json();
  assert.deepEqual(Object.fromEntries(state.bot_changesets.map((c) => [c.id, [c.kind, c.status]])), { 777: ["sync", "reverted"], 778: ["revert", "applied"] });
  assert.equal(state.rollback_jobs[0].status, "completed");
});

test("runner API rejects missing or wrong tokens", async () => {
  assert.equal((await get("/api/runner/control")).status, 401);
  assert.equal((await get("/api/runner/control", { Authorization: "Bearer nope" })).status, 401);
  assert.equal((await send("POST", "/api/runner/changesets", { id: 1 }, { Authorization: "Bearer nope" })).status, 401);
});

test("reports are stored gzipped and served in a sandbox, and latest only points at completed runs", async () => {
  const run = "20261031T041700Z";
  assert.equal((await send("POST", `/api/runner/runs/${run}`, { status: "running", started_at: "2026-10-31T04:17:00Z" }, RUNNER)).status, 200);
  const html = "<!doctype html><script>alert(1)</script>보고서";
  const body = gzipSync(html);
  const upload = await mf.dispatchFetch(`${ORIGIN}/api/runner/runs/${run}/files/report.html`, {
    method: "PUT", headers: { ...RUNNER, "Content-Length": String(body.length) }, body,
  });
  assert.equal(upload.status, 200, await upload.clone().text());
  assert.equal((await get("/reports/latest/report.html")).status, 404);
  await send("POST", `/api/runner/runs/${run}`, { status: "completed", finished_at: "2026-10-31T04:30:00Z", summary: { warning_count: 2 } }, RUNNER);
  const served = await get("/reports/latest/report.html");
  assert.equal(served.status, 200);
  assert.match(served.headers.get("Content-Security-Policy"), /^sandbox allow-scripts/);
  const bytes = Buffer.from(await served.arrayBuffer());
  const text = served.headers.get("Content-Encoding") === "gzip" ? gunzipSync(bytes).toString() : bytes.toString();
  assert.equal(text, html);
  assert.equal((await mf.dispatchFetch(`${ORIGIN}/api/runner/runs/${run}/files/evil.html`, { method: "PUT", headers: { ...RUNNER, "Content-Length": "3" }, body: "abc" })).status, 400);
  const state = await (await get("/api/state")).json();
  assert.deepEqual([state.runs[0].status, state.runs[0].files, state.runs[0].summary], ["completed", ["report.html"], { warning_count: 2 }]);
});

test("dashboard page sends a restrictive CSP", async () => {
  const page = await get("/");
  assert.equal(page.status, 200);
  assert.match(page.headers.get("Content-Security-Policy"), /script-src 'self'/);
  assert.equal(page.headers.get("X-Content-Type-Options"), "nosniff");
});
