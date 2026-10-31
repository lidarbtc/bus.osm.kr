import { countKoreaChangesets } from "./geo.js";
import { GZIP, emptyState, mutate } from "./store.js";
import { HttpError, cookie, json, parseCookies, randomToken, readJson, safeEqual, sha256, sign, verify } from "./util.js";

const SESSION_COOKIE = "__Host-session";
const STATE_COOKIE = "__Host-oauth";
const RUN_ID = /^\d{8}T\d{6}Z(?:-[a-z0-9]{1,16})?$/;
const RUN_STATUSES = new Set(["running", "completed", "completed_with_warnings", "failed", "skipped_stopped"]);
const FINAL = new Set(["completed", "completed_with_warnings", "failed", "skipped_stopped"]);
const DONE = new Set(["completed", "completed_with_warnings"]);
/** Report files the runner may upload; each is stored gzip-compressed as `<name>.gz`. */
export const REPORT_FILES = new Set([
  "report-data.json", "summary.md", "status.json", "comparison.json", "source-manifest.json",
  "osm-manifest.json", "stops.csv", "routes.csv", "stops.geojson",
]);
const RUNNER = { uid: null, name: "oracle-runner" };

function config(env) {
  const number = (key, fallback) => (Number.isFinite(Number(env[key])) && env[key] !== "" && env[key] !== undefined ? Number(env[key]) : fallback);
  return {
    osmWeb: env.OSM_WEB || "https://www.openstreetmap.org",
    osmApi: env.OSM_API || "https://api.openstreetmap.org",
    minChangesets: number("MIN_KOREA_CHANGESETS", 10),
    sessionHours: number("SESSION_HOURS", 12),
    keepRunFiles: number("RETAIN_RUN_FILES", 20),
    maxUpload: number("MAX_UPLOAD_BYTES", 300 * 1024 * 1024),
    admins: new Set(String(env.ADMIN_OSM_UIDS || "").split(",").map((v) => Number(v.trim())).filter((v) => Number.isSafeInteger(v) && v > 0)),
    oauthReady: Boolean(env.OSM_CLIENT_ID && env.OSM_CLIENT_SECRET && env.SESSION_SECRET),
    publicOrigin: env.PUBLIC_ORIGIN || "",
  };
}

function addAudit(state, at, actor, action, target, reason, detail = null) {
  const id = (state.audit.at(-1)?.id ?? 0) + 1;
  state.audit.push({ id, at, actor_uid: actor.uid, actor_name: actor.name, action, target, reason, detail });
}

export function publicState(state, cfg) {
  const s = state ?? emptyState(null);
  const runs = [...s.runs].sort((a, b) => b.started_at.localeCompare(a.started_at)).slice(0, 30);
  return {
    control: s.control,
    runs,
    bot_changesets: [...s.bot_changesets].reverse().slice(0, 100),
    rollback_jobs: [...s.rollback_jobs].reverse().slice(0, 50),
    audit: [...s.audit].reverse().slice(0, 100).map(({ detail, ...rest }) => rest),
    config: { min_korea_changesets: cfg.minChangesets, oauth_configured: cfg.oauthReady },
  };
}

function reasonOf(body) {
  const reason = typeof body.reason === "string" ? body.reason.trim() : "";
  if (reason.length < 3 || reason.length > 500) throw new HttpError(400, "사유를 3~500자로 입력하세요.");
  return reason;
}

function stopNow(state, at, actor, reason) {
  if (state.control.stopped) return false;
  state.control = { ...state.control, stopped: true, reason, actor_uid: actor.uid, actor_name: actor.name, updated_at: at };
  return true;
}

export function createApp({ env, store, fetcher = fetch, clock = () => Date.now() }) {
  const cfg = config(env);
  const iso = () => new Date(clock()).toISOString();

  function session(request) {
    const data = verify(env.SESSION_SECRET, parseCookies(request.headers.get("Cookie"))[SESSION_COOKIE], clock());
    if (!data) return null;
    // Operator rights come from configuration on every request, so removal is immediate.
    return { uid: data.u, name: data.n, korea_changesets: data.k, eligible: Boolean(data.e), csrf: data.c, is_admin: cfg.admins.has(data.u) && Boolean(data.ok) };
  }

  function requireUser(request, url, role) {
    if (request.headers.get("Origin") !== url.origin) throw new HttpError(403, "요청 출처를 확인할 수 없습니다.");
    const user = session(request);
    if (!user) throw new HttpError(401, "OSM 계정으로 로그인하세요.");
    if (!safeEqual(request.headers.get("X-CSRF-Token") ?? "", user.csrf)) throw new HttpError(403, "요청 확인 토큰이 맞지 않습니다. 새로고침 후 다시 시도하세요.");
    if (role === "admin" && !user.is_admin) throw new HttpError(403, "운영자 계정만 재개할 수 있습니다.");
    if (role === "eligible" && !user.eligible) throw new HttpError(403, `한국에서 ${cfg.minChangesets}개 이상의 변경세트를 올린 계정만 사용할 수 있습니다.`);
    return { uid: user.uid, name: user.name };
  }

  async function change(fn) {
    const { state, result } = await mutate(store, iso(), fn);
    return json({ ok: true, ...(result ?? {}), state: publicState(state, cfg) });
  }

  async function stop(request, url) {
    const user = requireUser(request, url, "eligible");
    const reason = reasonOf(await readJson(request));
    return change((state) => {
      if (!stopNow(state, iso(), user, reason)) throw new HttpError(409, "이미 비상 정지 상태입니다.");
      addAudit(state, iso(), user, "emergency_stop", null, reason);
    });
  }

  async function resume(request, url) {
    const user = requireUser(request, url, "admin");
    const reason = reasonOf(await readJson(request));
    return change((state) => {
      if (!state.control.stopped) throw new HttpError(409, "정지 상태가 아닙니다.");
      state.control = { ...state.control, stopped: false, reason, actor_uid: user.uid, actor_name: user.name, updated_at: iso() };
      addAudit(state, iso(), user, "resume", null, reason);
    });
  }

  async function requestRollback(request, url) {
    const user = requireUser(request, url, "eligible");
    const body = await readJson(request);
    const reason = reasonOf(body);
    const changesetId = Number(body.changeset_id);
    if (!Number.isSafeInteger(changesetId) || changesetId <= 0) throw new HttpError(400, "변경세트 번호가 올바르지 않습니다.");
    return change((state) => {
      const changeset = state.bot_changesets.find((c) => c.id === changesetId);
      if (!changeset) throw new HttpError(404, "이 시스템의 봇이 만든 변경세트만 롤백할 수 있습니다.");
      if (changeset.status !== "applied") throw new HttpError(409, "이미 되돌린 변경세트입니다.");
      if (state.rollback_jobs.some((j) => j.changeset_id === changesetId && ["queued", "running"].includes(j.status))) {
        throw new HttpError(409, "이 변경세트의 롤백이 이미 진행 중입니다.");
      }
      const at = iso();
      const job = { id: state.next_job_id++, changeset_id: changesetId, status: "queued", requested_at: at, requested_by_uid: user.uid,
        requested_by_name: user.name, reason, updated_at: at, result: null };
      state.rollback_jobs.push(job);
      addAudit(state, at, user, "rollback_requested", `changeset/${changesetId}`, reason, { job_id: job.id });
      // A rollback always halts further automated edits until an operator resumes.
      if (stopNow(state, at, user, `롤백 요청 #${job.id}: ${reason}`)) addAudit(state, at, user, "emergency_stop", null, `롤백 요청 #${job.id}에 따른 자동 정지`);
      return { job_id: job.id };
    });
  }

  async function login(url) {
    if (!cfg.oauthReady) throw new HttpError(503, "OSM 로그인이 아직 설정되지 않았습니다.");
    if (cfg.publicOrigin && url.origin !== cfg.publicOrigin) {
      return new Response(null, { status: 302, headers: { Location: `${cfg.publicOrigin}/api/auth/login` } });
    }
    const stateValue = randomToken();
    const verifier = randomToken(48);
    const target = new URL("/oauth2/authorize", cfg.osmWeb);
    target.search = new URLSearchParams({
      response_type: "code", client_id: env.OSM_CLIENT_ID, redirect_uri: `${url.origin}/api/auth/callback`, scope: "read_prefs",
      state: stateValue, code_challenge: sha256(verifier).toString("base64url"), code_challenge_method: "S256",
    }).toString();
    // Allow time for OSM sign-in (including two-factor) before the login request expires.
    const sealed = sign(env.SESSION_SECRET, { s: stateValue, v: verifier, x: clock() + 1_800_000 });
    return new Response(null, { status: 302, headers: { Location: target.toString(), "Set-Cookie": cookie(STATE_COOKIE, sealed, 1800) } });
  }

  async function callback(request, url) {
    if (!cfg.oauthReady) throw new HttpError(503, "OSM 로그인이 아직 설정되지 않았습니다.");
    const sealed = verify(env.SESSION_SECRET, parseCookies(request.headers.get("Cookie"))[STATE_COOKIE], clock());
    const code = url.searchParams.get("code") ?? "";
    if (!code || !sealed || !safeEqual(url.searchParams.get("state") ?? "", sealed.s)) {
      throw new HttpError(400, "로그인 요청을 확인할 수 없거나 만료되었습니다. 다시 로그인하세요.");
    }
    const form = (values) => ({ method: "POST", headers: { "Content-Type": "application/x-www-form-urlencoded", Accept: "application/json" }, body: new URLSearchParams(values) });
    const tokenResponse = await fetcher(new URL("/oauth2/token", cfg.osmWeb).toString(), form({
      grant_type: "authorization_code", code, redirect_uri: `${url.origin}/api/auth/callback`,
      client_id: env.OSM_CLIENT_ID, client_secret: env.OSM_CLIENT_SECRET, code_verifier: sealed.v,
    }));
    const accessToken = tokenResponse.ok ? (await tokenResponse.json()).access_token : null;
    if (!accessToken) throw new HttpError(502, "OSM 로그인 확인에 실패했습니다.");
    const details = await fetcher(new URL("/api/0.6/user/details.json", cfg.osmApi).toString(), {
      headers: { Authorization: `Bearer ${accessToken}`, Accept: "application/json" },
    });
    // The token is only needed for identity; revoke it at once and never store it.
    await fetcher(new URL("/oauth2/revoke", cfg.osmWeb).toString(), form({ token: accessToken, client_id: env.OSM_CLIENT_ID, client_secret: env.OSM_CLIENT_SECRET })).catch(() => undefined);
    const user = details.ok ? (await details.json()).user : null;
    if (!user || !Number.isSafeInteger(user.id) || !user.display_name) throw new HttpError(502, "OSM 계정 정보를 가져오지 못했습니다.");
    const count = await countKoreaChangesets(cfg.osmApi, user.id, cfg.minChangesets, fetcher);
    const unblocked = !((user.blocks?.received?.active ?? 0) > 0);
    const value = sign(env.SESSION_SECRET, {
      u: user.id, n: user.display_name, k: count, e: count >= cfg.minChangesets && unblocked, ok: unblocked,
      c: randomToken(), x: clock() + cfg.sessionHours * 3_600_000,
    });
    const headers = new Headers({ Location: "/" });
    headers.append("Set-Cookie", cookie(SESSION_COOKIE, value, cfg.sessionHours * 3600));
    headers.append("Set-Cookie", cookie(STATE_COOKIE, "", 0));
    return new Response(null, { status: 302, headers });
  }

  function me(request) {
    const user = session(request);
    if (!user) return json({ user: null });
    return json({ user: { uid: user.uid, name: user.name }, korea_changesets: user.korea_changesets, eligible: user.eligible, is_admin: user.is_admin, csrf: user.csrf });
  }

  function requireRunner(request) {
    const header = request.headers.get("Authorization") ?? "";
    if (!env.RUNNER_TOKEN || !header.startsWith("Bearer ") || !safeEqual(header.slice(7), env.RUNNER_TOKEN)) {
      throw new HttpError(401, "runner authentication failed");
    }
  }

  async function runnerControl(request) {
    const version = (request.headers.get("X-Runner-Version") ?? "").slice(0, 64) || null;
    let { state } = await store.read();
    state ??= emptyState(iso());
    const seen = Date.parse(state.control.runner_seen_at ?? "") || 0;
    // Heartbeats are written at most every 3 hours to stay within Blob write quotas.
    if (clock() - seen > 3 * 3_600_000 || state.control.runner_version !== version) {
      ({ state } = await mutate(store, iso(), (s) => {
        s.control = { ...s.control, runner_seen_at: iso(), runner_version: version };
      }));
    }
    const jobs = state.rollback_jobs.filter((j) => j.status === "queued")
      .map(({ id, changeset_id, reason, requested_by_name }) => ({ id, changeset_id, reason, requested_by_name }));
    return json({ stopped: Boolean(state.control.stopped), reason: state.control.reason, updated_at: state.control.updated_at, rollback_jobs: jobs });
  }

  async function runnerRun(request, runId) {
    const body = await readJson(request);
    const status = String(body.status ?? "");
    if (!RUN_STATUSES.has(status)) throw new HttpError(400, "invalid run status");
    const text = (key, max) => (typeof body[key] === "string" ? body[key].slice(0, max) : null);
    if (body.summary !== undefined && JSON.stringify(body.summary).length > 100_000) throw new HttpError(413, "summary too large");
    const { state, result } = await mutate(store, iso(), (s) => {
      let run = s.runs.find((r) => r.id === runId);
      if (!run) {
        run = { id: runId, status, started_at: text("started_at", 40) ?? iso(), finished_at: null, code_version: null, summary: null, files: [] };
        s.runs.push(run);
      }
      run.status = status;
      run.finished_at = text("finished_at", 40) ?? run.finished_at;
      run.code_version = text("code_version", 64) ?? run.code_version;
      if (body.summary !== undefined) run.summary = body.summary;
      s.runs.sort((a, b) => a.started_at.localeCompare(b.started_at));
      const purge = [];
      if (FINAL.has(status)) {
        const withFiles = s.runs.filter((r) => r.files.length);
        for (const old of withFiles.slice(0, Math.max(0, withFiles.length - cfg.keepRunFiles))) {
          old.files = [];
          old.files_expired = true;
          purge.push(old.id);
        }
        s.runs = s.runs.slice(-200);
      }
      return { purge };
    });
    for (const id of result.purge) await store.deletePrefix(`runs/${id}/`);
    return json({ ok: true, runs: state.runs.length });
  }

  async function runnerUpload(request, runId, record) {
    const body = await readJson(request);
    const name = String(body.name ?? "");
    const size = Number(body.size);
    if (!REPORT_FILES.has(name)) throw new HttpError(400, "unknown report file");
    if (!Number.isSafeInteger(size) || size <= 0 || size > cfg.maxUpload) throw new HttpError(413, "invalid upload size");
    if (record) {
      await mutate(store, iso(), (s) => {
        const run = s.runs.find((r) => r.id === runId);
        if (!run) throw new HttpError(404, "unknown run");
        if (!run.files.includes(name)) run.files.push(name);
      });
      return json({ ok: true });
    }
    const { state } = await store.read();
    if (!state?.runs.some((r) => r.id === runId)) throw new HttpError(404, "unknown run");
    const validUntil = clock() + 30 * 60_000;
    const uploadUrl = await store.presign("put", `runs/${runId}/${name}.gz`, { validUntil, maximumSizeInBytes: cfg.maxUpload });
    return json({ url: uploadUrl, content_type: GZIP, valid_until: new Date(validUntil).toISOString() });
  }

  async function runnerJob(request, jobId) {
    const body = await readJson(request);
    const status = String(body.status ?? "");
    const allowed = { running: ["queued"], completed: ["running"], needs_manual: ["running"], failed: ["queued", "running"] };
    if (!allowed[status]) throw new HttpError(400, "invalid job status");
    await mutate(store, iso(), (s) => {
      const job = s.rollback_jobs.find((j) => j.id === jobId);
      if (!job) throw new HttpError(404, "unknown job");
      if (!allowed[status].includes(job.status)) throw new HttpError(409, `cannot move job from ${job.status} to ${status}`);
      job.status = status;
      job.updated_at = iso();
      if (body.result !== undefined) job.result = body.result;
      if (status === "completed") {
        const changeset = s.bot_changesets.find((c) => c.id === job.changeset_id);
        if (changeset) changeset.status = "reverted";
        const revert = body.revert_changeset;
        if (revert && Number.isSafeInteger(revert.id) && !s.bot_changesets.some((c) => c.id === revert.id)) {
          s.bot_changesets.push({ id: revert.id, run_id: null, kind: "revert", created_at: iso(), comment: String(revert.comment ?? "").slice(0, 500), status: "applied" });
        }
      }
      if (status !== "running") addAudit(s, iso(), RUNNER, `rollback_${status}`, `changeset/${job.changeset_id}`, "", { job_id: jobId });
    });
    return json({ ok: true });
  }

  async function runnerChangeset(request) {
    const body = await readJson(request);
    const id = Number(body.id);
    if (!Number.isSafeInteger(id) || id <= 0) throw new HttpError(400, "invalid changeset id");
    await mutate(store, iso(), (s) => {
      if (!s.bot_changesets.some((c) => c.id === id)) {
        s.bot_changesets.push({ id, run_id: typeof body.run_id === "string" ? body.run_id.slice(0, 40) : null, kind: "sync",
          created_at: iso(), comment: String(body.comment ?? "").slice(0, 500), status: "applied" });
      }
    });
    return json({ ok: true });
  }

  async function report(runId, name) {
    if (!REPORT_FILES.has(name) || (runId !== "latest" && !RUN_ID.test(runId))) throw new HttpError(404, "보고서 파일이 없습니다.");
    const { state } = await store.read();
    const runs = [...(state?.runs ?? [])].sort((a, b) => b.started_at.localeCompare(a.started_at));
    const run = runId === "latest" ? runs.find((r) => DONE.has(r.status) && r.files.includes(name)) : runs.find((r) => r.id === runId);
    if (!run || !run.files.includes(name)) throw new HttpError(404, runId === "latest" ? "완료된 비교 보고서가 아직 없습니다." : "보고서 파일이 없습니다.");
    // Private blob, short-lived signed link. Blob serves it as a gzip download with CORS enabled.
    const location = await store.presign("get", `runs/${run.id}/${name}.gz`, { validUntil: clock() + 10 * 60_000 });
    return new Response(null, { status: 302, headers: { Location: location, "Cache-Control": "no-store", "X-Report-Run": run.id } });
  }

  async function route(request) {
    const url = new URL(request.url);
    // vercel.json rewrites /api/* to the single function and passes the original path.
    const path = url.searchParams.has("__path") ? `/api/${url.searchParams.get("__path")}` : url.pathname;
    url.searchParams.delete("__path");
    const method = request.method;
    if (method === "GET" && path === "/api/state") {
      const { state } = await store.read();
      return json(publicState(state, cfg), 200, { "Cache-Control": "public, max-age=0, s-maxage=15, stale-while-revalidate=45" });
    }
    if (method === "GET" && path === "/api/me") return me(request);
    if (method === "POST" && path === "/api/stop") return stop(request, url);
    if (method === "POST" && path === "/api/resume") return resume(request, url);
    if (method === "POST" && path === "/api/rollback") return requestRollback(request, url);
    if (method === "GET" && path === "/api/auth/login") return login(url);
    if (method === "GET" && path === "/api/auth/callback") return callback(request, url);
    if (method === "POST" && path === "/api/auth/logout") {
      if (request.headers.get("Origin") !== url.origin) throw new HttpError(403, "요청 출처를 확인할 수 없습니다.");
      return json({ ok: true }, 200, { "Set-Cookie": cookie(SESSION_COOKIE, "", 0) });
    }
    if (path.startsWith("/api/runner/")) {
      requireRunner(request);
      const parts = path.split("/").slice(3);
      if (method === "GET" && parts.join("/") === "control") return runnerControl(request);
      if (method === "POST" && parts.join("/") === "changesets") return runnerChangeset(request);
      if (parts[0] === "runs" && RUN_ID.test(parts[1] ?? "") && method === "POST") {
        if (parts.length === 2) return runnerRun(request, parts[1]);
        if (parts.length === 3 && parts[2] === "uploads") return runnerUpload(request, parts[1], false);
        if (parts.length === 3 && parts[2] === "files") return runnerUpload(request, parts[1], true);
      }
      if (method === "POST" && parts[0] === "jobs" && /^\d+$/.test(parts[1] ?? "") && parts.length === 2) return runnerJob(request, Number(parts[1]));
      throw new HttpError(404, "not found");
    }
    const match = /^\/api\/reports\/([^/]+)\/([^/]+)$/.exec(path);
    if (method === "GET" && match) return report(match[1], match[2]);
    throw new HttpError(404, "찾을 수 없는 주소입니다.");
  }

  return async function handle(request) {
    try {
      return await route(request);
    } catch (error) {
      if (error instanceof HttpError) return json({ error: error.message }, error.status);
      console.error(error);
      return json({ error: "서버 오류가 발생했습니다." }, 500);
    }
  };
}
