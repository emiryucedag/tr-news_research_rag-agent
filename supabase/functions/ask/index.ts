// Chat endpoint: question -> embedding -> match_articles -> ONE generation call -> answer.
//
// Access: signed-in users (Supabase Auth JWT in the Authorization header) get a daily
// quota; requests without an Authorization header are guests with a much smaller one,
// keyed by a salted hash of the IP address. Quotas are checked before any model call.
//
// The model only sees numbered candidates and returns [{n, sentence}]. Links, dates and
// source names are rendered here in code, so the model cannot corrupt or invent them.
// If anything goes wrong we fall back to a plain title + link list and give the unit back.
import { createRemoteJWKSet, jwtVerify } from "npm:jose@5";

const GEMINI = "https://generativelanguage.googleapis.com/v1beta/models";
const EMBED_MODEL = "gemini-embedding-001";
const EMBED_DIMS = 768;
const GEN_MODEL = Deno.env.get("GEN_MODEL") ?? "gemini-3.5-flash-lite";
const CANDIDATES = 12;
const FALLBACK_ITEMS = 5;
const MAX_QUESTION_CHARS = 300;

const GEMINI_KEY = Deno.env.get("GEMINI_API_KEY")!;
const SUPABASE_URL = Deno.env.get("SUPABASE_URL")!;
const SERVICE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
const IP_SALT = Deno.env.get("IP_SALT")!;

const num = (name: string, fallback: number) => Number(Deno.env.get(name) ?? fallback);
const USER_DAILY_LIMIT = num("USER_DAILY_LIMIT", 10);
const USER_POOL_LIMIT = num("USER_POOL_LIMIT", 250); // all signed-in users together
const GUEST_DAILY_LIMIT = num("GUEST_DAILY_LIMIT", 1); // per IP
const GUEST_POOL_LIMIT = num("GUEST_POOL_LIMIT", 30); // all guests together

const ALLOWED_ORIGINS = (Deno.env.get("ALLOWED_ORIGINS") ??
  "http://localhost:8000,http://127.0.0.1:8000,http://localhost:5173").split(",").map((s) => s.trim());

const JWKS = createRemoteJWKSet(new URL(`${SUPABASE_URL}/auth/v1/.well-known/jwks.json`));

type Article = {
  id: string;
  title: string;
  summary: string | null;
  link: string;
  source: string;
  published_at: string;
  similarity: number;
};

type Identity = { scope: "user" | "guest"; subject: string; limit: number; pool: string; poolLimit: number };

// --- http helpers -------------------------------------------------------------

function corsHeaders(req: Request): Record<string, string> {
  const origin = req.headers.get("origin");
  const headers: Record<string, string> = { Vary: "Origin" };
  if (origin && ALLOWED_ORIGINS.includes(origin)) {
    headers["Access-Control-Allow-Origin"] = origin;
    headers["Access-Control-Allow-Headers"] = "authorization, content-type, apikey, x-client-info";
    headers["Access-Control-Allow-Methods"] = "POST, OPTIONS";
  }
  return headers;
}

function json(req: Request, body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8", ...corsHeaders(req) },
  });
}

// --- identity -----------------------------------------------------------------

async function sha256Hex(text: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return Array.from(new Uint8Array(digest)).map((b) => b.toString(16).padStart(2, "0")).join("");
}

function clientIp(req: Request): string {
  return req.headers.get("cf-connecting-ip") ??
    req.headers.get("x-forwarded-for")?.split(",")[0].trim() ?? "unknown";
}

/** Returns the caller's identity, or null when an Authorization header is present but invalid. */
async function identify(req: Request): Promise<Identity | null> {
  const auth = req.headers.get("authorization");
  if (!auth) {
    const ipHash = (await sha256Hex(`${IP_SALT}:${clientIp(req)}`)).slice(0, 32);
    return { scope: "guest", subject: `guest:${ipHash}`, limit: GUEST_DAILY_LIMIT, pool: "pool:guests", poolLimit: GUEST_POOL_LIMIT };
  }
  try {
    const token = auth.replace(/^Bearer\s+/i, "");
    const { payload } = await jwtVerify(token, JWKS, {
      issuer: `${SUPABASE_URL}/auth/v1`,
      audience: "authenticated",
    });
    if (!payload.sub || payload.is_anonymous === true) return null;
    return { scope: "user", subject: `user:${payload.sub}`, limit: USER_DAILY_LIMIT, pool: "pool:users", poolLimit: USER_POOL_LIMIT };
  } catch {
    return null;
  }
}

// --- quota --------------------------------------------------------------------

async function rpc(name: string, args: Record<string, unknown>) {
  const r = await fetch(`${SUPABASE_URL}/rest/v1/rpc/${name}`, {
    method: "POST",
    headers: { apikey: SERVICE_KEY, Authorization: `Bearer ${SERVICE_KEY}`, "Content-Type": "application/json" },
    body: JSON.stringify(args),
  });
  if (!r.ok) throw new Error(`${name} ${r.status}: ${(await r.text()).slice(0, 160)}`);
  return r.status === 204 ? null : await r.json().catch(() => null);
}

/** The Turkish day ends at 21:00 UTC (Turkey is permanently UTC+3). */
function nextResetIso(): string {
  const now = new Date();
  const reset = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate(), 21, 0, 0));
  if (reset <= now) reset.setUTCDate(reset.getUTCDate() + 1);
  return reset.toISOString();
}

// --- gemini -------------------------------------------------------------------

async function geminiPost(path: string, body: unknown) {
  const r = await fetch(`${GEMINI}/${path}`, {
    method: "POST",
    headers: { "x-goog-api-key": GEMINI_KEY, "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(`Gemini ${r.status}: ${data?.error?.message?.slice(0, 160) ?? ""}`);
  return data;
}

async function embedQuestion(question: string): Promise<number[]> {
  const data = await geminiPost(`${EMBED_MODEL}:embedContent`, {
    content: { parts: [{ text: question }] },
    taskType: "RETRIEVAL_QUERY",
    outputDimensionality: EMBED_DIMS,
  });
  return data.embedding.values;
}

async function matchArticles(vec: number[], sinceHours: number | null): Promise<Article[]> {
  const since = sinceHours ? new Date(Date.now() - sinceHours * 3600_000).toISOString() : null;
  const r = await rpc("match_articles", { query_embedding: `[${vec.join(",")}]`, match_count: CANDIDATES, since });
  return r ?? [];
}

function buildPrompt(question: string, articles: Article[]): string {
  const list = articles
    .map((a, i) => `[${i + 1}] ${a.title}\n${(a.summary ?? "").slice(0, 400)}`)
    .join("\n\n");
  return [
    "You answer a user's question about recent news using ONLY the numbered candidate articles below.",
    "Select only the candidates that directly help answer the question and ignore the rest.",
    "If several candidates report the same event or story, select only the single most informative one.",
    "For each selected candidate write ONE sentence in Turkish, in your own words, based only on",
    "its title and summary. Do not add facts, numbers or names that are not in the candidate.",
    "Write correct Turkish using all diacritics (ç, ğ, ı, ö, ş, ü, İ).",
    "Always write numbers as digits (for example 30 bin, 249 dolar), never as words.",
    "If no candidate is relevant, return an empty list.",
    "",
    `Question: ${question}`,
    "",
    "Candidates:",
    list,
  ].join("\n");
}

const RESPONSE_SCHEMA = {
  type: "ARRAY",
  items: {
    type: "OBJECT",
    properties: { n: { type: "INTEGER" }, sentence: { type: "STRING" } },
    required: ["n", "sentence"],
  },
};

async function generate(question: string, articles: Article[]) {
  const data = await geminiPost(`${GEN_MODEL}:generateContent`, {
    contents: [{ parts: [{ text: buildPrompt(question, articles) }] }],
    generationConfig: { responseMimeType: "application/json", responseSchema: RESPONSE_SCHEMA, temperature: 0 },
  });
  const parts = data.candidates?.[0]?.content?.parts ?? [];
  const text = parts.filter((p: { thought?: boolean }) => !p.thought).map((p: { text?: string }) => p.text ?? "").join("");
  return { picks: JSON.parse(text) as { n: number; sentence: string }[], usage: data.usageMetadata ?? {} };
}

function formatDate(iso: string): string {
  return new Intl.DateTimeFormat("tr-TR", {
    timeZone: "Europe/Istanbul",
    day: "numeric",
    month: "long",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(iso));
}

function plainList(articles: Article[]) {
  return articles.slice(0, FALLBACK_ITEMS).map((a) => ({
    title: a.title,
    source: a.source,
    date: formatDate(a.published_at),
    link: a.link,
  }));
}

// --- handler ------------------------------------------------------------------

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response(null, { status: 204, headers: corsHeaders(req) });
  if (req.method !== "POST") return json(req, { error: "POST only" }, 405);

  const body = await req.json().catch(() => ({}));
  const question = String(body.question ?? "").trim();
  if (!question || question.length > MAX_QUESTION_CHARS) {
    return json(req, { error: `question must be 1-${MAX_QUESTION_CHARS} characters` }, 400);
  }
  const sinceHours = typeof body.since_hours === "number" ? body.since_hours : null;

  const who = await identify(req);
  if (!who) return json(req, { error: "invalid_token" }, 401);

  const t0 = performance.now();
  const timings: Record<string, number> = {};
  const lap = (name: string, from: number) => (timings[name] = Math.round(performance.now() - from));

  // Take one unit before any model work; give it back if we cannot answer.
  let used: number;
  try {
    const q = await rpc("consume_quota", {
      p_subject: who.subject, p_limit: who.limit, p_pool: who.pool, p_pool_limit: who.poolLimit,
    });
    if (q.status !== "ok") {
      return json(req, {
        error: "quota_exceeded",
        reason: q.status, // "subject_limit" or "pool_limit"
        scope: who.scope,
        quota: { limit: who.limit, used: q.used, remaining: 0, resets_at: nextResetIso() },
      }, 429);
    }
    used = q.used;
  } catch (e) {
    return json(req, { error: `quota check failed: ${String(e).slice(0, 120)}` }, 503);
  }

  let geminiRequests = 0;
  let refund = false;
  const respond = (payload: Record<string, unknown>, status = 200) => {
    const spent = refund ? used - 1 : used;
    return json(req, {
      ...payload,
      scope: who.scope,
      quota: { limit: who.limit, used: spent, remaining: Math.max(who.limit - spent, 0), resets_at: nextResetIso() },
      gemini_requests: geminiRequests,
      timings,
    }, status);
  };

  try {
    let t = performance.now();
    const vec = await embedQuestion(question);
    geminiRequests++;
    lap("embed_ms", t);

    t = performance.now();
    const articles = await matchArticles(vec, sinceHours);
    lap("search_ms", t);
    if (articles.length === 0) {
      refund = true;
      await rpc("refund_quota", { p_subject: who.subject, p_pool: who.pool });
      lap("total_ms", t0);
      return respond({ mode: "empty", items: [] });
    }

    t = performance.now();
    let result;
    try {
      geminiRequests++;
      result = await generate(question, articles);
    } catch (e) {
      lap("generate_ms", t);
      refund = true;
      await rpc("refund_quota", { p_subject: who.subject, p_pool: who.pool });
      lap("total_ms", t0);
      return respond({ mode: "fallback", reason: String(e).slice(0, 200), items: plainList(articles) });
    }
    lap("generate_ms", t);

    // Code-side checks: numbers must exist and be unique; links come from the database.
    const seen = new Set<number>();
    const items = [];
    for (const p of result.picks) {
      const a = articles[p.n - 1];
      if (!a || seen.has(p.n) || !p.sentence?.trim()) continue;
      seen.add(p.n);
      items.push({
        sentence: p.sentence.trim(),
        title: a.title,
        source: a.source,
        date: formatDate(a.published_at),
        link: a.link,
      });
    }
    lap("total_ms", t0);
    return respond({
      mode: items.length ? "answer" : "none_relevant",
      model: GEN_MODEL,
      items,
      candidates: articles.length,
      dropped_invalid: result.picks.length - items.length,
      usage: result.usage,
    });
  } catch (e) {
    refund = true;
    await rpc("refund_quota", { p_subject: who.subject, p_pool: who.pool }).catch(() => {});
    lap("total_ms", t0);
    return respond({ error: String(e).slice(0, 200) }, 502);
  }
});
