// Helpers shared by the Edge Functions: CORS, JSON responses, anonymous visitor hashes and
// calls to the database with the server-side key.

const SUPABASE_URL = Deno.env.get("SUPABASE_URL")!;
const SERVICE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
const IP_SALT = Deno.env.get("IP_SALT")!;

const ALLOWED_ORIGINS = (Deno.env.get("ALLOWED_ORIGINS") ??
  "http://localhost:8000,http://127.0.0.1:8000,http://localhost:5173").split(",").map((s) => s.trim());

/** Only the listed sites may call these functions from a browser. */
export function corsHeaders(req: Request): Record<string, string> {
  const origin = req.headers.get("origin");
  const headers: Record<string, string> = { Vary: "Origin" };
  if (origin && ALLOWED_ORIGINS.includes(origin)) {
    headers["Access-Control-Allow-Origin"] = origin;
    headers["Access-Control-Allow-Headers"] = "authorization, content-type, apikey, x-client-info";
    headers["Access-Control-Allow-Methods"] = "POST, OPTIONS";
    headers["Access-Control-Max-Age"] = "86400"; // the browser may reuse this answer for a day, saving a round trip
  }
  return headers;
}

export function json(req: Request, body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8", ...corsHeaders(req) },
  });
}

export async function sha256Hex(text: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return Array.from(new Uint8Array(digest)).map((b) => b.toString(16).padStart(2, "0")).join("");
}

function clientIp(req: Request): string {
  return req.headers.get("cf-connecting-ip") ??
    req.headers.get("x-forwarded-for")?.split(",")[0].trim() ?? "unknown";
}

/** A salted hash of the caller's IP address; the address itself is never stored. */
export async function visitorHash(req: Request): Promise<string> {
  return (await sha256Hex(`${IP_SALT}:${clientIp(req)}`)).slice(0, 32);
}

/** Call a database function with the server-side key. */
export async function rpc(name: string, args: Record<string, unknown>) {
  const r = await fetch(`${SUPABASE_URL}/rest/v1/rpc/${name}`, {
    method: "POST",
    headers: { apikey: SERVICE_KEY, Authorization: `Bearer ${SERVICE_KEY}`, "Content-Type": "application/json" },
    body: JSON.stringify(args),
  });
  if (!r.ok) throw new Error(`${name} ${r.status}: ${(await r.text()).slice(0, 160)}`);
  return r.status === 204 ? null : await r.json().catch(() => null);
}

/** Read rows with the server-side key; `query` is a PostgREST query string such as "select=id&link=eq.x". */
export async function restGet(table: string, query: string) {
  const r = await fetch(`${SUPABASE_URL}/rest/v1/${table}?${query}`, {
    headers: { apikey: SERVICE_KEY, Authorization: `Bearer ${SERVICE_KEY}` },
  });
  if (!r.ok) throw new Error(`read ${table} ${r.status}: ${(await r.text()).slice(0, 160)}`);
  return await r.json();
}

/** Insert one row; returns "created" or "exists" when a unique index already has it. */
export async function insertRow(table: string, row: Record<string, unknown>): Promise<"created" | "exists"> {
  const r = await fetch(`${SUPABASE_URL}/rest/v1/${table}`, {
    method: "POST",
    headers: { apikey: SERVICE_KEY, Authorization: `Bearer ${SERVICE_KEY}`, "Content-Type": "application/json", Prefer: "return=minimal" },
    body: JSON.stringify(row),
  });
  if (r.status === 409) return "exists"; // unique violation
  if (!r.ok) throw new Error(`insert ${table} ${r.status}: ${(await r.text()).slice(0, 160)}`);
  return "created";
}

/** The Turkish day ends at 21:00 UTC (Turkey is permanently UTC+3). */
export function nextResetIso(): string {
  const now = new Date();
  const reset = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate(), 21, 0, 0));
  if (reset <= now) reset.setUTCDate(reset.getUTCDate() + 1);
  return reset.toISOString();
}
