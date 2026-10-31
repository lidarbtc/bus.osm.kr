import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { beforeEach, test } from "node:test";
import { createApp } from "../lib/app.js";
import { memoryStore } from "../lib/store.js";

const ORIGIN = "https://dash.test";
const RUNNER = { Authorization: "Bearer runner-secret" };
const ENV = {
  OSM_WEB: "https://osm.test", OSM_API: "https://api.osm.test", MIN_KOREA_CHANGESETS: "10", ADMIN_OSM_UIDS: "1004,1003",
  OSM_CLIENT_ID: "client", OSM_CLIENT_SECRET: "client-secret", RUNNER_TOKEN: "runner-secret", SESSION_SECRET: "session-secret",
  RETAIN_RUN_FILES: "2",
};
// uid -> changesets inside/outside South Korea and active blocks
const OSM_USERS = {
  1001: { name: "seoul-mapper", blocks: 0, inside: 10, outside: 30 },
  1002: { name: "almost", blocks: 0, inside: 9, outside: 0 },
  1003: { name: "blocked", blocks: 1, inside: 50, outside: 0 },
  1004: { name: "operator", blocks: 0, inside: 12, outside: 0 },
};

let osmCalls;
let nextLogin;
let store;
let now;
let handle;

function changesets(uid) {
  const { inside, outside } = OSM_USERS[uid];
  const box = (lat, lon) => ({ min_lat: lat, max_lat: lat + 0.01, min_lon: lon, max_lon: lon + 0.01 });
  const list = [];
  // Tokyo and Pyongyang lie inside the query bbox but outside South Korea.
  for (let i = 0; i < outside; i++) list.push({ id: uid * 1000 + i, changes_count: 3, created_at: "2026-10-01T00:00:00Z", ...(i % 2 ? box(35.68, 139.7) : box(39.03, 125.75)) });
  for (let i = 0; i < inside; i++) list.push({ id: uid * 1000 + 500 + i, changes_count: 1, created_at: "2026-09-01T00:00:00Z", ...box(37.56, 126.97) });
  list.push({ id: uid * 1000 + 900, changes_count: 0, created_at: "2026-08-01T00:00:00Z", ...box(37.56, 126.97) });
  return list;
}

async function fakeOsm(input, init = {}) {
  const url = new URL(input);
  osmCalls.push(url.pathname);
  if (url.pathname === "/oauth2/token") {
    const body = new URLSearchParams(init.body);
    assert.equal(body.get("client_secret"), "client-secret");
    assert.ok(body.get("code_verifier").length >= 43);
    return Response.json({ access_token: "token-" + nextLogin });
  }
  if (url.pathname === "/oauth2/revoke") return new Response("");
  if (url.pathname === "/api/0.6/user/details.json") {
    const uid = Number(init.headers.Authorization.replace("Bearer token-", ""));
    return Response.json({ user: { id: uid, display_name: OSM_USERS[uid].name, blocks: { received: { active: OSM_USERS[uid].blocks } } } });
  }
  if (url.pathname === "/api/0.6/changesets.json") {
    assert.equal(url.searchParams.get("closed"), "true");
    return Response.json({ changesets: changesets(Number(url.searchParams.get("user"))) });
  }
  return new Response("unexpected " + url.pathname, { status: 500 });
}

beforeEach(() => {
  osmCalls = [];
  store = memoryStore();
  now = Date.parse("2026-10-31T00:00:00Z");
  handle = createApp({ env: ENV, store, fetcher: fakeOsm, clock: () => now });
});

const get = (path, headers = {}) => handle(new Request(ORIGIN + path, { headers }));
const send = (method, path, body, headers = {}) =>
  handle(new Request(ORIGIN + path, { method, headers: { "Content-Type": "application/json", ...headers }, body: JSON.stringify(body) }));

async function login(uid) {
  nextLogin = uid;
  const start = await get("/api/auth/login");
  assert.equal(start.status, 302);
  const authorize = new URL(start.headers.get("Location"));
  assert.equal(authorize.origin + authorize.pathname, "https://osm.test/oauth2/authorize");
  assert.equal(authorize.searchParams.get("code_challenge_method"), "S256");
  assert.equal(authorize.searchParams.get("scope"), "read_prefs");
  assert.equal(authorize.searchParams.get("redirect_uri"), ORIGIN + "/api/auth/callback");
  const stateCookie = start.headers.get("Set-Cookie").split(";")[0];
  const done = await get("/api/auth/callback?code=abc&state=" + authorize.searchParams.get("state"), { Cookie: stateCookie });
  assert.equal(done.status, 302, await done.clone().text());
  const cookie = done.headers.getSetCookie().find((c) => c.startsWith("__Host-session=")).split(";")[0];
  const me = await (await get("/api/me", { Cookie: cookie })).json();
  return { cookie, me, headers: { Cookie: cookie, Origin: ORIGIN, "X-CSRF-Token": me.csrf } };
}

test("OSM login counts only non-empty changesets centred in South Korea and revokes the token", async () => {
  const eligible = await login(1001);
  assert.deepEqual([eligible.me.user.name, eligible.me.korea_changesets, eligible.me.eligible, eligible.me.is_admin], ["seoul-mapper", 10, true, false]);
  assert.ok(osmCalls.includes("/oauth2/revoke"));
  const short = await login(1002);
  assert.deepEqual([short.me.korea_changesets, short.me.eligible], [9, false]);
  // A blocked account is neither eligible nor an operator, even when listed in ADMIN_OSM_UIDS.
  const blocked = await login(1003);
  assert.deepEqual([blocked.me.eligible, blocked.me.is_admin], [false, false]);
  const operator = await login(1004);
  assert.equal(operator.me.is_admin, true);
});

test("login rejects a forged OAuth state and a tampered or expired session", async () => {
  const start = await get("/api/auth/login");
  const stateValue = new URL(start.headers.get("Location")).searchParams.get("state");
  const forged = await get("/api/auth/callback?code=abc&state=" + stateValue, { Cookie: "__Host-oauth=attacker.sig" });
  assert.equal(forged.status, 400);
  assert.ok(!osmCalls.includes("/oauth2/token"));

  const mapper = await login(1001);
  const [payload, signature] = mapper.cookie.slice("__Host-session=".length).split(".");
  const raised = Buffer.from(JSON.stringify({ ...JSON.parse(Buffer.from(payload, "base64url")), u: 1004 })).toString("base64url");
  assert.deepEqual(await (await get("/api/me", { Cookie: "__Host-session=" + raised + "." + signature })).json(), { user: null });
  now += 13 * 3_600_000;
  assert.deepEqual(await (await get("/api/me", { Cookie: mapper.cookie })).json(), { user: null });
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
  assert.equal(state.control.stopped, false);
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
  assert.equal((await send("POST", "/api/runner/jobs/" + job_id, { status: "completed" }, RUNNER)).status, 409);
  assert.equal((await send("POST", "/api/runner/jobs/" + job_id, { status: "running" }, RUNNER)).status, 200);
  const done = await send("POST", "/api/runner/jobs/" + job_id, { status: "completed", result: { reverted: 3 }, revert_changeset: { id: 778, comment: "Revert 777" } }, RUNNER);
  assert.equal(done.status, 200);
  const state = await (await get("/api/state")).json();
  assert.deepEqual(Object.fromEntries(state.bot_changesets.map((c) => [c.id, [c.kind, c.status]])), { 777: ["sync", "reverted"], 778: ["revert", "applied"] });
  assert.equal(state.rollback_jobs[0].status, "completed");
  assert.equal((await send("POST", "/api/rollback", { changeset_id: 777, reason: "다시 되돌리기" }, mapper.headers)).status, 409);
});

test("runner API rejects missing or wrong tokens", async () => {
  assert.equal((await get("/api/runner/control")).status, 401);
  assert.equal((await get("/api/runner/control", { Authorization: "Bearer nope" })).status, 401);
  assert.equal((await send("POST", "/api/runner/changesets", { id: 1 }, { Authorization: "Bearer nope" })).status, 401);
});

test("runner heartbeat is written only when stale or when the version changes", async () => {
  const writes = () => store.read().then(({ etag }) => etag);
  await get("/api/runner/control", { ...RUNNER, "X-Runner-Version": "abc" });
  const first = await writes();
  now += 60 * 60_000;
  await get("/api/runner/control", { ...RUNNER, "X-Runner-Version": "abc" });
  assert.equal(await writes(), first);
  await get("/api/runner/control", { ...RUNNER, "X-Runner-Version": "def" });
  assert.notEqual(await writes(), first);
});

test("reports are uploaded through presigned URLs and latest only points at completed runs", async () => {
  const run = "20261031T041700Z";
  assert.equal((await send("POST", "/api/runner/runs/" + run + "/uploads", { name: "summary.md", size: 10 }, RUNNER)).status, 404);
  assert.equal((await send("POST", "/api/runner/runs/" + run, { status: "running", started_at: "2026-10-31T04:17:00Z" }, RUNNER)).status, 200);
  const presigned = await send("POST", "/api/runner/runs/" + run + "/uploads", { name: "report-data.json", size: 1234 }, RUNNER);
  assert.equal(presigned.status, 200);
  const { url, content_type } = await presigned.json();
  assert.equal(content_type, "application/gzip");
  assert.match(url, /^https:\/\/blob\.test\/put\/runs\/20261031T041700Z\/report-data\.json\.gz\?/);
  assert.equal((await send("POST", "/api/runner/runs/" + run + "/uploads", { name: "report.html", size: 10 }, RUNNER)).status, 400);
  assert.equal((await send("POST", "/api/runner/runs/" + run + "/uploads", { name: "summary.md", size: 0 }, RUNNER)).status, 413);
  assert.equal((await send("POST", "/api/runner/runs/" + run + "/files", { name: "report-data.json", size: 1234 }, RUNNER)).status, 200);

  assert.equal((await get("/api/reports/latest/report-data.json")).status, 404);
  assert.equal((await get("/api/reports/" + run + "/report-data.json")).status, 302);
  await send("POST", "/api/runner/runs/" + run, { status: "completed", finished_at: "2026-10-31T04:30:00Z", summary: { warning_count: 2 } }, RUNNER);
  const served = await get("/api/reports/latest/report-data.json");
  assert.equal(served.status, 302);
  assert.equal(served.headers.get("X-Report-Run"), run);
  assert.match(served.headers.get("Location"), /^https:\/\/blob\.test\/get\/runs\/20261031T041700Z\/report-data\.json\.gz/);
  assert.equal((await get("/api/reports/../state.json")).status, 404);
  assert.equal((await get("/api/reports/latest/state.json")).status, 404);

  const state = await (await get("/api/state")).json();
  assert.deepEqual([state.runs[0].status, state.runs[0].files, state.runs[0].summary], ["completed", ["report-data.json"], { warning_count: 2 }]);
});

test("old run files are purged beyond RETAIN_RUN_FILES", async () => {
  const runs = ["20261028T041700Z", "20261029T041700Z", "20261030T041700Z"];
  for (const run of runs) {
    const startedAt = run.replace(/^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z$/, "$1-$2-$3T$4:$5:$6Z");
    await send("POST", "/api/runner/runs/" + run, { status: "running", started_at: startedAt }, RUNNER);
    await send("POST", "/api/runner/runs/" + run + "/files", { name: "summary.md", size: 10 }, RUNNER);
    await send("POST", "/api/runner/runs/" + run, { status: "completed" }, RUNNER);
  }
  assert.deepEqual(store.deleted, ["runs/20261028T041700Z/"]);
  const state = await (await get("/api/state")).json();
  assert.deepEqual(state.runs.map((r) => [r.id, r.files.length]), [[runs[2], 1], [runs[1], 1], [runs[0], 0]]);
});

test("vercel.json routes the API to one function and sends a restrictive CSP", () => {
  const config = JSON.parse(readFileSync(new URL("../vercel.json", import.meta.url), "utf8"));
  assert.deepEqual(config.rewrites, [{ source: "/api/:path*", destination: "/api/handler?__path=:path*" }]);
  const csp = config.headers[0].headers.find((h) => h.key === "Content-Security-Policy").value;
  assert.match(csp, /script-src 'self';/);
  assert.match(csp, /frame-ancestors 'none'/);
});
