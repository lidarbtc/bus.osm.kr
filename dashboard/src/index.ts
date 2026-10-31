import { countKoreaChangesets } from "./geo";
import { APP_CSS, APP_JS, DASHBOARD_HTML } from "./ui";
import {
  HttpError,
  cookie,
  json,
  nowIso,
  parseCookies,
  randomToken,
  readJson,
  safeEqual,
  sha256Hex,
  b64url,
  sha256,
} from "./util";

export interface Env {
  DB: D1Database;
  REPORTS: R2Bucket;
  OSM_WEB: string;
  OSM_API: string;
  MIN_KOREA_CHANGESETS: string;
  ADMIN_OSM_UIDS: string;
  SESSION_HOURS: string;
  REPORT_RETENTION_DAYS: string;
  OSM_CLIENT_ID?: string;
  OSM_CLIENT_SECRET?: string;
  RUNNER_TOKEN?: string;
}

interface Session {
  uid: number;
  name: string;
  korea_changesets: number;
  eligible: number;
  is_admin: number;
  csrf: string;
}

const SESSION_COOKIE = "__Host-session";
const STATE_COOKIE = "__Host-oauth-state";
const RUN_ID = /^\d{8}T\d{6}Z(?:-[a-z0-9]{1,16})?$/;
const RUN_STATUSES = new Set(["running", "completed", "completed_with_warnings", "failed", "skipped_stopped"]);
const REPORT_TYPES: Record<string, string> = {
  "report.html": "text/html; charset=utf-8",
  "summary.md": "text/markdown; charset=utf-8",
  "status.json": "application/json; charset=utf-8",
  "comparison.json": "application/json; charset=utf-8",
  "source-manifest.json": "application/json; charset=utf-8",
  "osm-manifest.json": "application/json; charset=utf-8",
  "stops.csv": "text/csv; charset=utf-8",
  "routes.csv": "text/csv; charset=utf-8",
  "stops.geojson": "application/geo+json; charset=utf-8",
};
const MAX_UPLOAD = 95 * 1024 * 1024;
const DASHBOARD_CSP =
  "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; " +
  "form-action 'self' https://www.openstreetmap.org; base-uri 'none'; frame-ancestors 'none'";

const minChangesets = (env: Env) => Number(env.MIN_KOREA_CHANGESETS || "10");
const admins = (env: Env) =>
  new Set(
    (env.ADMIN_OSM_UIDS || "")
      .split(",")
      .map((v) => Number(v.trim()))
      .filter((v) => Number.isSafeInteger(v) && v > 0),
  );
const oauthReady = (env: Env) => Boolean(env.OSM_CLIENT_ID && env.OSM_CLIENT_SECRET);

async function audit(env: Env, actor: { uid: number | null; name: string }, action: string, target: string | null, reason: string, detail?: unknown) {
  await env.DB.prepare(
    "INSERT INTO audit_log (at, actor_uid, actor_name, action, target, reason, detail) VALUES (?, ?, ?, ?, ?, ?, ?)",
  )
    .bind(nowIso(), actor.uid, actor.name, action, target, reason, detail === undefined ? null : JSON.stringify(detail))
    .run();
}

async function getSession(request: Request, env: Env): Promise<Session | null> {
  const id = parseCookies(request.headers.get("Cookie"))[SESSION_COOKIE];
  if (!id) return null;
  return env.DB.prepare(
    "SELECT uid, name, korea_changesets, eligible, is_admin, csrf FROM sessions WHERE id_hash = ? AND expires_at > ?",
  )
    .bind(await sha256Hex(id), nowIso())
    .first<Session>();
}

async function requireUser(request: Request, env: Env, role: "eligible" | "admin"): Promise<Session> {
  if (request.headers.get("Origin") !== new URL(request.url).origin) {
    throw new HttpError(403, "요청 출처를 확인할 수 없습니다.");
  }
  const session = await getSession(request, env);
  if (!session) throw new HttpError(401, "OSM 계정으로 로그인하세요.");
  if (!(await safeEqual(request.headers.get("X-CSRF-Token") ?? "", session.csrf))) {
    throw new HttpError(403, "요청 확인 토큰이 맞지 않습니다. 새로고침 후 다시 시도하세요.");
  }
  if (role === "admin" && !session.is_admin) throw new HttpError(403, "운영자 계정만 재개할 수 있습니다.");
  if (role === "eligible" && !session.eligible) {
    throw new HttpError(403, `한국에서 ${minChangesets(env)}개 이상의 변경세트를 올린 계정만 사용할 수 있습니다.`);
  }
  return session;
}

function reasonOf(body: Record<string, unknown>): string {
  const reason = typeof body.reason === "string" ? body.reason.trim() : "";
  if (reason.length < 3 || reason.length > 500) throw new HttpError(400, "사유를 3~500자로 입력하세요.");
  return reason;
}

function parseJsonColumn(value: unknown): unknown {
  if (typeof value !== "string") return null;
  try {
    return JSON.parse(value);
  } catch {
    return null;
  }
}

async function state(env: Env): Promise<Response> {
  const [control, runs, changesets, jobs, log] = await env.DB.batch([
    env.DB.prepare("SELECT stopped, reason, actor_name, updated_at, runner_seen_at, runner_version FROM control WHERE id = 1"),
    env.DB.prepare("SELECT id, status, started_at, finished_at, code_version, summary, files FROM runs ORDER BY started_at DESC LIMIT 30"),
    env.DB.prepare("SELECT id, run_id, kind, created_at, comment, status FROM bot_changesets ORDER BY created_at DESC LIMIT 100"),
    env.DB.prepare(
      "SELECT id, changeset_id, status, requested_at, requested_by_name, reason, updated_at, result FROM rollback_jobs ORDER BY id DESC LIMIT 50",
    ),
    env.DB.prepare("SELECT id, at, actor_uid, actor_name, action, target, reason FROM audit_log ORDER BY id DESC LIMIT 100"),
  ]);
  type Row = Record<string, unknown>;
  return json({
    control: control.results[0],
    runs: (runs.results as Row[]).map((r) => ({ ...r, summary: parseJsonColumn(r.summary), files: parseJsonColumn(r.files) ?? [] })),
    bot_changesets: changesets.results,
    rollback_jobs: (jobs.results as Row[]).map((r) => ({ ...r, result: parseJsonColumn(r.result) })),
    audit: log.results,
    config: { min_korea_changesets: minChangesets(env), oauth_configured: oauthReady(env) },
  });
}

async function stop(request: Request, env: Env): Promise<Response> {
  const user = await requireUser(request, env, "eligible");
  const reason = reasonOf(await readJson(request));
  const result = await env.DB.prepare(
    "UPDATE control SET stopped = 1, reason = ?, actor_uid = ?, actor_name = ?, updated_at = ? WHERE id = 1 AND stopped = 0",
  )
    .bind(reason, user.uid, user.name, nowIso())
    .run();
  if (!result.meta.changes) throw new HttpError(409, "이미 비상 정지 상태입니다.");
  await audit(env, user, "emergency_stop", null, reason);
  return json({ ok: true });
}

async function resume(request: Request, env: Env): Promise<Response> {
  const user = await requireUser(request, env, "admin");
  const reason = reasonOf(await readJson(request));
  const result = await env.DB.prepare(
    "UPDATE control SET stopped = 0, reason = ?, actor_uid = ?, actor_name = ?, updated_at = ? WHERE id = 1 AND stopped = 1",
  )
    .bind(reason, user.uid, user.name, nowIso())
    .run();
  if (!result.meta.changes) throw new HttpError(409, "정지 상태가 아닙니다.");
  await audit(env, user, "resume", null, reason);
  return json({ ok: true });
}

async function requestRollback(request: Request, env: Env): Promise<Response> {
  const user = await requireUser(request, env, "eligible");
  const body = await readJson(request);
  const reason = reasonOf(body);
  const changesetId = Number(body.changeset_id);
  if (!Number.isSafeInteger(changesetId) || changesetId <= 0) throw new HttpError(400, "변경세트 번호가 올바르지 않습니다.");
  const changeset = await env.DB.prepare("SELECT status FROM bot_changesets WHERE id = ?").bind(changesetId).first<{ status: string }>();
  if (!changeset) throw new HttpError(404, "이 시스템의 봇이 만든 변경세트만 롤백할 수 있습니다.");
  if (changeset.status !== "applied") throw new HttpError(409, "이미 되돌린 변경세트입니다.");
  const now = nowIso();
  let jobId: number;
  try {
    const inserted = await env.DB.prepare(
      "INSERT INTO rollback_jobs (changeset_id, status, requested_at, requested_by_uid, requested_by_name, reason, updated_at) VALUES (?, 'queued', ?, ?, ?, ?, ?) RETURNING id",
    )
      .bind(changesetId, now, user.uid, user.name, reason, now)
      .first<{ id: number }>();
    jobId = inserted!.id;
  } catch {
    throw new HttpError(409, "이 변경세트의 롤백이 이미 진행 중입니다.");
  }
  // A rollback always halts further automated edits until an operator resumes.
  const stopped = await env.DB.prepare(
    "UPDATE control SET stopped = 1, reason = ?, actor_uid = ?, actor_name = ?, updated_at = ? WHERE id = 1 AND stopped = 0",
  )
    .bind(`롤백 요청 #${jobId}: ${reason}`, user.uid, user.name, now)
    .run();
  await audit(env, user, "rollback_requested", `changeset/${changesetId}`, reason, { job_id: jobId });
  if (stopped.meta.changes) await audit(env, user, "emergency_stop", null, `롤백 요청 #${jobId}에 따른 자동 정지`);
  return json({ ok: true, job_id: jobId });
}

async function login(request: Request, env: Env): Promise<Response> {
  if (!oauthReady(env)) throw new HttpError(503, "OSM 로그인이 아직 설정되지 않았습니다.");
  const origin = new URL(request.url).origin;
  const stateValue = randomToken();
  const verifier = randomToken(48);
  await env.DB.prepare("INSERT INTO oauth_states (state_hash, verifier, expires_at) VALUES (?, ?, ?)")
    .bind(await sha256Hex(stateValue), verifier, new Date(Date.now() + 10 * 60_000).toISOString())
    .run();
  const target = new URL("/oauth2/authorize", env.OSM_WEB);
  target.search = new URLSearchParams({
    response_type: "code",
    client_id: env.OSM_CLIENT_ID!,
    redirect_uri: `${origin}/auth/callback`,
    scope: "read_prefs",
    state: stateValue,
    code_challenge: b64url(await sha256(verifier)),
    code_challenge_method: "S256",
  }).toString();
  return new Response(null, { status: 302, headers: { Location: target.toString(), "Set-Cookie": cookie(STATE_COOKIE, stateValue, 600) } });
}

async function callback(request: Request, env: Env): Promise<Response> {
  if (!oauthReady(env)) throw new HttpError(503, "OSM 로그인이 아직 설정되지 않았습니다.");
  const url = new URL(request.url);
  const stateValue = url.searchParams.get("state") ?? "";
  const code = url.searchParams.get("code") ?? "";
  const expected = parseCookies(request.headers.get("Cookie"))[STATE_COOKIE] ?? "";
  if (!code || !stateValue || !expected || !(await safeEqual(stateValue, expected))) {
    throw new HttpError(400, "로그인 요청을 확인할 수 없습니다. 다시 로그인하세요.");
  }
  const stateRow = await env.DB.prepare("DELETE FROM oauth_states WHERE state_hash = ? AND expires_at > ? RETURNING verifier")
    .bind(await sha256Hex(stateValue), nowIso())
    .first<{ verifier: string }>();
  if (!stateRow) throw new HttpError(400, "로그인 요청이 만료되었습니다. 다시 로그인하세요.");
  const tokenResponse = await fetch(new URL("/oauth2/token", env.OSM_WEB).toString(), {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded", Accept: "application/json" },
    body: new URLSearchParams({
      grant_type: "authorization_code",
      code,
      redirect_uri: `${url.origin}/auth/callback`,
      client_id: env.OSM_CLIENT_ID!,
      client_secret: env.OSM_CLIENT_SECRET!,
      code_verifier: stateRow.verifier,
    }),
  });
  if (!tokenResponse.ok) throw new HttpError(502, "OSM 로그인 확인에 실패했습니다.");
  const accessToken = ((await tokenResponse.json()) as { access_token?: string }).access_token;
  if (!accessToken) throw new HttpError(502, "OSM 로그인 확인에 실패했습니다.");
  const detailsResponse = await fetch(new URL("/api/0.6/user/details.json", env.OSM_API).toString(), {
    headers: { Authorization: `Bearer ${accessToken}`, Accept: "application/json" },
  });
  // The token is only needed for identity; revoke it immediately and never store it.
  await fetch(new URL("/oauth2/revoke", env.OSM_WEB).toString(), {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ token: accessToken, client_id: env.OSM_CLIENT_ID!, client_secret: env.OSM_CLIENT_SECRET! }),
  }).catch(() => undefined);
  if (!detailsResponse.ok) throw new HttpError(502, "OSM 계정 정보를 가져오지 못했습니다.");
  const user = ((await detailsResponse.json()) as {
    user?: { id?: number; display_name?: string; blocks?: { received?: { active?: number } } };
  }).user;
  if (!user || !Number.isSafeInteger(user.id) || !user.display_name) throw new HttpError(502, "OSM 계정 정보가 올바르지 않습니다.");
  const need = minChangesets(env);
  const count = await countKoreaChangesets(env.OSM_API, user.id!, need);
  const blocked = (user.blocks?.received?.active ?? 0) > 0;
  const sessionId = randomToken();
  const hours = Number(env.SESSION_HOURS || "12");
  await env.DB.prepare(
    "INSERT INTO sessions (id_hash, uid, name, korea_changesets, eligible, is_admin, csrf, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
  )
    .bind(
      await sha256Hex(sessionId),
      user.id,
      user.display_name,
      count,
      count >= need && !blocked ? 1 : 0,
      admins(env).has(user.id!) && !blocked ? 1 : 0,
      randomToken(),
      nowIso(),
      new Date(Date.now() + hours * 3_600_000).toISOString(),
    )
    .run();
  const headers = new Headers({ Location: "/" });
  headers.append("Set-Cookie", cookie(SESSION_COOKIE, sessionId, hours * 3600));
  headers.append("Set-Cookie", cookie(STATE_COOKIE, "", 0));
  return new Response(null, { status: 302, headers });
}

async function logout(request: Request, env: Env): Promise<Response> {
  if (request.headers.get("Origin") !== new URL(request.url).origin) throw new HttpError(403, "요청 출처를 확인할 수 없습니다.");
  const id = parseCookies(request.headers.get("Cookie"))[SESSION_COOKIE];
  if (id) await env.DB.prepare("DELETE FROM sessions WHERE id_hash = ?").bind(await sha256Hex(id)).run();
  return json({ ok: true }, 200, { "Set-Cookie": cookie(SESSION_COOKIE, "", 0) });
}

async function me(request: Request, env: Env): Promise<Response> {
  const session = await getSession(request, env);
  if (!session) return json({ user: null });
  return json({
    user: { uid: session.uid, name: session.name },
    korea_changesets: session.korea_changesets,
    eligible: Boolean(session.eligible),
    is_admin: Boolean(session.is_admin),
    csrf: session.csrf,
  });
}

async function requireRunner(request: Request, env: Env): Promise<void> {
  const header = request.headers.get("Authorization") ?? "";
  if (!env.RUNNER_TOKEN || !header.startsWith("Bearer ") || !(await safeEqual(header.slice(7), env.RUNNER_TOKEN))) {
    throw new HttpError(401, "runner authentication failed");
  }
}

const RUNNER = { uid: null, name: "oracle-runner" };

async function runnerControl(request: Request, env: Env): Promise<Response> {
  const version = (request.headers.get("X-Runner-Version") ?? "").slice(0, 64);
  const [, control, jobs] = await env.DB.batch([
    env.DB.prepare("UPDATE control SET runner_seen_at = ?, runner_version = ? WHERE id = 1").bind(nowIso(), version || null),
    env.DB.prepare("SELECT stopped, reason, updated_at FROM control WHERE id = 1"),
    env.DB.prepare("SELECT id, changeset_id, reason, requested_by_name FROM rollback_jobs WHERE status = 'queued' ORDER BY id"),
  ]);
  const row = control.results[0] as { stopped: number; reason: string; updated_at: string };
  return json({ stopped: Boolean(row.stopped), reason: row.reason, updated_at: row.updated_at, rollback_jobs: jobs.results });
}

async function runnerRun(request: Request, env: Env, runId: string): Promise<Response> {
  const body = await readJson(request);
  const status = String(body.status ?? "");
  if (!RUN_STATUSES.has(status)) throw new HttpError(400, "invalid run status");
  const text = (key: string, max: number) => (typeof body[key] === "string" ? (body[key] as string).slice(0, max) : null);
  const summary = body.summary === undefined ? null : JSON.stringify(body.summary);
  if (summary && summary.length > 200_000) throw new HttpError(413, "summary too large");
  await env.DB.prepare(
    `INSERT INTO runs (id, status, started_at, finished_at, code_version, summary) VALUES (?, ?, ?, ?, ?, ?)
     ON CONFLICT (id) DO UPDATE SET status = excluded.status,
       finished_at = COALESCE(excluded.finished_at, runs.finished_at),
       code_version = COALESCE(excluded.code_version, runs.code_version),
       summary = COALESCE(excluded.summary, runs.summary)`,
  )
    .bind(runId, status, text("started_at", 40) ?? nowIso(), text("finished_at", 40), text("code_version", 64), summary)
    .run();
  return json({ ok: true });
}

async function runnerUpload(request: Request, env: Env, runId: string, name: string): Promise<Response> {
  const contentType = REPORT_TYPES[name];
  if (!contentType) throw new HttpError(400, "unknown report file");
  const length = Number(request.headers.get("Content-Length"));
  if (!Number.isSafeInteger(length) || length <= 0 || length > MAX_UPLOAD || !request.body) throw new HttpError(413, "invalid upload size");
  const run = await env.DB.prepare("SELECT id FROM runs WHERE id = ?").bind(runId).first();
  if (!run) throw new HttpError(404, "unknown run");
  const stream = new FixedLengthStream(length);
  const pumped = request.body.pipeTo(stream.writable);
  await env.REPORTS.put(`runs/${runId}/${name}`, stream.readable, {
    httpMetadata: { contentType, contentEncoding: "gzip" },
  });
  await pumped;
  await env.DB.prepare(
    "UPDATE runs SET files = json_insert(files, '$[#]', ?) WHERE id = ? AND NOT EXISTS (SELECT 1 FROM json_each(runs.files) WHERE value = ?)",
  )
    .bind(name, runId, name)
    .run();
  return json({ ok: true });
}

async function runnerJob(request: Request, env: Env, jobId: number): Promise<Response> {
  const body = await readJson(request);
  const status = String(body.status ?? "");
  const allowed: Record<string, string[]> = { running: ["queued"], completed: ["running"], needs_manual: ["running"], failed: ["queued", "running"] };
  if (!allowed[status]) throw new HttpError(400, "invalid job status");
  const job = await env.DB.prepare("SELECT changeset_id, status FROM rollback_jobs WHERE id = ?").bind(jobId).first<{ changeset_id: number; status: string }>();
  if (!job) throw new HttpError(404, "unknown job");
  if (!allowed[status].includes(job.status)) throw new HttpError(409, `cannot move job from ${job.status} to ${status}`);
  const result = body.result === undefined ? null : JSON.stringify(body.result).slice(0, 100_000);
  const now = nowIso();
  const statements = [
    env.DB.prepare("UPDATE rollback_jobs SET status = ?, updated_at = ?, result = COALESCE(?, result) WHERE id = ?").bind(status, now, result, jobId),
  ];
  if (status === "completed") {
    statements.push(env.DB.prepare("UPDATE bot_changesets SET status = 'reverted' WHERE id = ?").bind(job.changeset_id));
    const revert = body.revert_changeset as { id?: number; comment?: string } | undefined;
    if (revert && Number.isSafeInteger(revert.id)) {
      statements.push(
        env.DB.prepare("INSERT OR IGNORE INTO bot_changesets (id, kind, created_at, comment) VALUES (?, 'revert', ?, ?)").bind(
          revert.id,
          now,
          String(revert.comment ?? "").slice(0, 500),
        ),
      );
    }
  }
  await env.DB.batch(statements);
  if (status !== "running") await audit(env, RUNNER, `rollback_${status}`, `changeset/${job.changeset_id}`, "", { job_id: jobId, result: body.result ?? null });
  return json({ ok: true });
}

async function runnerChangeset(request: Request, env: Env): Promise<Response> {
  const body = await readJson(request);
  const id = Number(body.id);
  if (!Number.isSafeInteger(id) || id <= 0) throw new HttpError(400, "invalid changeset id");
  await env.DB.prepare("INSERT OR IGNORE INTO bot_changesets (id, run_id, kind, created_at, comment) VALUES (?, ?, 'sync', ?, ?)")
    .bind(id, typeof body.run_id === "string" ? body.run_id.slice(0, 40) : null, nowIso(), String(body.comment ?? "").slice(0, 500))
    .run();
  return json({ ok: true });
}

async function report(env: Env, runId: string, name: string): Promise<Response> {
  const contentType = REPORT_TYPES[name];
  if (!contentType) throw new HttpError(404, "보고서 파일이 없습니다.");
  let id = runId;
  if (runId === "latest") {
    const latest = await env.DB.prepare(
      "SELECT id FROM runs WHERE status IN ('completed', 'completed_with_warnings') AND EXISTS (SELECT 1 FROM json_each(runs.files) WHERE value = ?) ORDER BY started_at DESC LIMIT 1",
    )
      .bind(name)
      .first<{ id: string }>();
    if (!latest) throw new HttpError(404, "완료된 비교 보고서가 아직 없습니다.");
    id = latest.id;
  } else if (!RUN_ID.test(runId)) {
    throw new HttpError(404, "보고서 파일이 없습니다.");
  }
  const object = await env.REPORTS.get(`runs/${id}/${name}`);
  if (!object) throw new HttpError(404, "보고서 파일이 없습니다.");
  const headers = new Headers({
    "Content-Type": contentType,
    "Content-Encoding": "gzip",
    "Cache-Control": runId === "latest" ? "no-cache" : "public, max-age=86400, immutable",
    // Reports embed third-party OSM/public-data strings; isolate them from the dashboard origin.
    "Content-Security-Policy": name.endsWith(".html")
      ? "sandbox allow-scripts allow-popups allow-popups-to-escape-sandbox; default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:"
      : "sandbox; default-src 'none'",
  });
  if (name.endsWith(".csv")) headers.set("Content-Disposition", `attachment; filename="${id}-${name}"`);
  return new Response(object.body, { headers, encodeBody: "manual" });
}

async function route(request: Request, env: Env): Promise<Response> {
  const url = new URL(request.url);
  const { pathname } = url;
  const method = request.method;
  if (method === "GET" && pathname === "/") {
    return new Response(DASHBOARD_HTML, { headers: { "Content-Type": "text/html; charset=utf-8", "Content-Security-Policy": DASHBOARD_CSP, "Cache-Control": "no-cache" } });
  }
  if (method === "GET" && pathname === "/app.js") return new Response(APP_JS, { headers: { "Content-Type": "text/javascript; charset=utf-8", "Cache-Control": "no-cache" } });
  if (method === "GET" && pathname === "/app.css") return new Response(APP_CSS, { headers: { "Content-Type": "text/css; charset=utf-8", "Cache-Control": "no-cache" } });
  if (method === "GET" && pathname === "/api/state") return state(env);
  if (method === "GET" && pathname === "/api/me") return me(request, env);
  if (method === "POST" && pathname === "/api/stop") return stop(request, env);
  if (method === "POST" && pathname === "/api/resume") return resume(request, env);
  if (method === "POST" && pathname === "/api/rollback") return requestRollback(request, env);
  if (method === "GET" && pathname === "/auth/login") return login(request, env);
  if (method === "GET" && pathname === "/auth/callback") return callback(request, env);
  if (method === "POST" && pathname === "/auth/logout") return logout(request, env);
  if (pathname.startsWith("/api/runner/")) {
    await requireRunner(request, env);
    const parts = pathname.split("/").slice(3);
    if (method === "GET" && parts.join("/") === "control") return runnerControl(request, env);
    if (method === "POST" && parts.join("/") === "changesets") return runnerChangeset(request, env);
    if (parts[0] === "runs" && RUN_ID.test(parts[1] ?? "")) {
      if (method === "POST" && parts.length === 2) return runnerRun(request, env, parts[1]);
      if (method === "PUT" && parts.length === 4 && parts[2] === "files") return runnerUpload(request, env, parts[1], parts[3]);
    }
    if (method === "POST" && parts[0] === "jobs" && /^\d+$/.test(parts[1] ?? "") && parts.length === 2) return runnerJob(request, env, Number(parts[1]));
    throw new HttpError(404, "not found");
  }
  const match = /^\/reports\/([^/]+)\/([^/]+)$/.exec(pathname);
  if (method === "GET" && match) return report(env, match[1], match[2]);
  throw new HttpError(404, "페이지를 찾을 수 없습니다.");
}

async function cleanup(env: Env): Promise<void> {
  const now = nowIso();
  const cutoff = new Date(Date.now() - Number(env.REPORT_RETENTION_DAYS || "120") * 86_400_000).toISOString();
  const old = await env.DB.prepare("SELECT id FROM runs WHERE started_at < ? LIMIT 50").bind(cutoff).all<{ id: string }>();
  for (const { id } of old.results) {
    const listed = await env.REPORTS.list({ prefix: `runs/${id}/` });
    if (listed.objects.length) await env.REPORTS.delete(listed.objects.map((o) => o.key));
    await env.DB.prepare("DELETE FROM runs WHERE id = ?").bind(id).run();
  }
  await env.DB.batch([
    env.DB.prepare("DELETE FROM sessions WHERE expires_at <= ?").bind(now),
    env.DB.prepare("DELETE FROM oauth_states WHERE expires_at <= ?").bind(now),
  ]);
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    let response: Response;
    try {
      response = await route(request, env);
    } catch (error) {
      if (error instanceof HttpError) response = json({ error: error.message }, error.status);
      else {
        console.error(error);
        response = json({ error: "서버 오류가 발생했습니다." }, 500);
      }
    }
    response.headers.set("X-Content-Type-Options", "nosniff");
    response.headers.set("Referrer-Policy", "no-referrer");
    if (!response.headers.has("Content-Security-Policy")) response.headers.set("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'");
    return response;
  },
  async scheduled(_event: ScheduledController, env: Env, ctx: ExecutionContext): Promise<void> {
    ctx.waitUntil(cleanup(env));
  },
} satisfies ExportedHandler<Env>;
