import { BlobPreconditionFailedError, del, get, issueSignedToken, list, presignUrl, put } from "@vercel/blob";
import { HttpError } from "./util.js";

// The whole dashboard state is one small private JSON document. Writes use the
// blob ETag (If-Match), so concurrent edits retry instead of overwriting.
export const STATE_PATH = "state/state.json";
export const GZIP = "application/gzip";

export function emptyState(now) {
  return {
    version: 1,
    control: { stopped: false, reason: "", actor_uid: null, actor_name: null, updated_at: now, runner_seen_at: null, runner_version: null },
    runs: [],
    bot_changesets: [],
    rollback_jobs: [],
    audit: [],
    next_job_id: 1,
  };
}

const isConflict = (error) => error instanceof BlobPreconditionFailedError || /already exists|precondition/i.test(String(error?.message));

export function blobStore() {
  return {
    async read() {
      const result = await get(STATE_PATH, { access: "private", useCache: false });
      if (!result || result.statusCode !== 200) return { state: null, etag: null };
      return { state: JSON.parse(await new Response(result.stream).text()), etag: result.blob.etag };
    },
    async write(state, etag) {
      try {
        await put(STATE_PATH, JSON.stringify(state), {
          access: "private", contentType: "application/json", addRandomSuffix: false,
          allowOverwrite: Boolean(etag), ...(etag ? { ifMatch: etag } : {}),
        });
      } catch (error) {
        if (isConflict(error)) return false;
        throw error;
      }
      return true;
    },
    async presign(operation, pathname, { validUntil, maximumSizeInBytes } = {}) {
      const upload = operation === "put" ? { maximumSizeInBytes, allowedContentTypes: [GZIP] } : {};
      const token = await issueSignedToken({ pathname, operations: [operation], validUntil, ...upload });
      const options = { operation, pathname, access: "private", validUntil };
      if (operation === "put") Object.assign(options, upload, { allowOverwrite: true, addRandomSuffix: false });
      return (await presignUrl(token, options)).presignedUrl;
    },
    async deletePrefix(prefix) {
      let cursor;
      do {
        const page = await list({ prefix, cursor, limit: 1000 });
        if (page.blobs.length) await del(page.blobs.map((b) => b.url));
        cursor = page.hasMore ? page.cursor : undefined;
      } while (cursor);
    },
  };
}

/** In-memory stand-in with the same ETag semantics, for tests. */
export function memoryStore() {
  let saved = null;
  let version = 0;
  const store = {
    deleted: [],
    async read() {
      return saved ? { state: structuredClone(saved), etag: String(version) } : { state: null, etag: null };
    },
    async write(state, etag) {
      if ((saved ? String(version) : null) !== etag) return false;
      saved = structuredClone(state);
      version++;
      return true;
    },
    async presign(operation, pathname, { validUntil, maximumSizeInBytes } = {}) {
      return `https://blob.test/${operation}/${pathname}?until=${validUntil}&max=${maximumSizeInBytes ?? ""}`;
    },
    async deletePrefix(prefix) {
      store.deleted.push(prefix);
    },
  };
  return store;
}

/** Applies `change(state)` with optimistic concurrency; validation errors abort without writing. */
export async function mutate(store, now, change) {
  for (let attempt = 0; attempt < 6; attempt++) {
    const { state, etag } = await store.read();
    const current = state ?? emptyState(now);
    const result = await change(current);
    if (await store.write(current, etag)) return { state: current, result };
  }
  throw new HttpError(503, "동시에 수정하는 요청이 많아 저장하지 못했습니다. 다시 시도하세요.");
}
