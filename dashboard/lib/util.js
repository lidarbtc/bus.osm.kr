import { createHash, createHmac, randomBytes, timingSafeEqual } from "node:crypto";

export class HttpError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

export const b64url = (buffer) => Buffer.from(buffer).toString("base64url");
export const randomToken = (bytes = 32) => b64url(randomBytes(bytes));
export const sha256 = (value) => createHash("sha256").update(value).digest();

/** Constant-time comparison of two strings of any length. */
export function safeEqual(a, b) {
  return timingSafeEqual(sha256(String(a)), sha256(String(b)));
}

/** `payload.signature` cookie values; tampering or expiry yields null. */
export function sign(secret, data) {
  const payload = b64url(JSON.stringify(data));
  return `${payload}.${b64url(createHmac("sha256", secret).update(payload).digest())}`;
}

export function verify(secret, value, now) {
  if (!secret || typeof value !== "string") return null;
  const [payload, signature] = value.split(".");
  if (!payload || !signature) return null;
  const expected = b64url(createHmac("sha256", secret).update(payload).digest());
  if (!safeEqual(signature, expected)) return null;
  try {
    const data = JSON.parse(Buffer.from(payload, "base64url").toString("utf8"));
    return typeof data.x === "number" && data.x > now ? data : null;
  } catch {
    return null;
  }
}

export function parseCookies(header) {
  const result = {};
  for (const part of (header ?? "").split(";")) {
    const index = part.indexOf("=");
    if (index > 0) result[part.slice(0, index).trim()] = part.slice(index + 1).trim();
  }
  return result;
}

export const cookie = (name, value, maxAge) => `${name}=${value}; Max-Age=${maxAge}; Path=/; HttpOnly; Secure; SameSite=Lax`;

export function json(data, status = 200, headers = {}) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store", ...headers },
  });
}

export async function readJson(request) {
  if (!(request.headers.get("Content-Type") ?? "").startsWith("application/json")) throw new HttpError(415, "JSON 요청만 받습니다.");
  const text = await request.text();
  if (text.length > 200_000) throw new HttpError(413, "요청이 너무 큽니다.");
  try {
    const value = JSON.parse(text);
    if (value && typeof value === "object" && !Array.isArray(value)) return value;
  } catch {
    // Fall through to the uniform error below.
  }
  throw new HttpError(400, "JSON 형식이 올바르지 않습니다.");
}
