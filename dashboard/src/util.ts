export const encoder = new TextEncoder();

export class HttpError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

export const nowIso = (): string => new Date().toISOString();

export function b64url(bytes: Uint8Array): string {
  let text = "";
  for (const byte of bytes) text += String.fromCharCode(byte);
  return btoa(text).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export function randomToken(bytes = 32): string {
  const value = new Uint8Array(bytes);
  crypto.getRandomValues(value);
  return b64url(value);
}

export async function sha256(value: string): Promise<Uint8Array> {
  return new Uint8Array(await crypto.subtle.digest("SHA-256", encoder.encode(value)));
}

export async function sha256Hex(value: string): Promise<string> {
  return [...(await sha256(value))].map((b) => b.toString(16).padStart(2, "0")).join("");
}

/** Constant-time comparison of two strings of any length. */
export async function safeEqual(a: string, b: string): Promise<boolean> {
  const [x, y] = await Promise.all([sha256(a), sha256(b)]);
  return crypto.subtle.timingSafeEqual(x, y);
}

export function parseCookies(header: string | null): Record<string, string> {
  const result: Record<string, string> = {};
  for (const part of (header ?? "").split(";")) {
    const index = part.indexOf("=");
    if (index > 0) result[part.slice(0, index).trim()] = part.slice(index + 1).trim();
  }
  return result;
}

export function cookie(name: string, value: string, maxAge: number): string {
  return `${name}=${value}; Max-Age=${maxAge}; Path=/; HttpOnly; Secure; SameSite=Lax`;
}

export function json(data: unknown, status = 200, headers: HeadersInit = {}): Response {
  const response = new Response(JSON.stringify(data), { status, headers });
  response.headers.set("Content-Type", "application/json; charset=utf-8");
  response.headers.set("Cache-Control", "no-store");
  return response;
}

export async function readJson(request: Request): Promise<Record<string, unknown>> {
  if (!(request.headers.get("Content-Type") ?? "").startsWith("application/json")) {
    throw new HttpError(415, "JSON 요청만 받습니다.");
  }
  const text = await request.text();
  if (text.length > 20_000) throw new HttpError(413, "요청이 너무 큽니다.");
  try {
    const value = JSON.parse(text);
    if (value && typeof value === "object" && !Array.isArray(value)) return value;
  } catch {
    // Fall through to the uniform error below.
  }
  throw new HttpError(400, "JSON 형식이 올바르지 않습니다.");
}
