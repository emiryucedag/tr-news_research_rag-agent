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
import { json, corsHeaders, nextResetIso, rpc, visitorHash } from "../_shared/http.ts";

const GEMINI = "https://generativelanguage.googleapis.com/v1beta/models";
const EMBED_MODEL = "gemini-embedding-001";
const EMBED_DIMS = 768;
// Primary model first, then a second one with its own free-tier quota if the first fails.
const GEN_MODELS = [...new Set([Deno.env.get("GEN_MODEL") ?? "gemini-3.5-flash-lite", "gemini-3.1-flash-lite"])];
const EMBED_TIMEOUT_MS = 8000;
const GENERATE_TIMEOUT_MS = 10000; // per model
const CANDIDATES = 12;
const FALLBACK_ITEMS = 5;
const MAX_QUESTION_CHARS = 300;

const GEMINI_KEY = Deno.env.get("GEMINI_API_KEY")!;
const SUPABASE_URL = Deno.env.get("SUPABASE_URL")!;
const SERVICE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;

const num = (name: string, fallback: number) => Number(Deno.env.get(name) ?? fallback);
const USER_DAILY_LIMIT = num("USER_DAILY_LIMIT", 10);
const USER_POOL_LIMIT = num("USER_POOL_LIMIT", 250); // all signed-in users together
const GUEST_DAILY_LIMIT = num("GUEST_DAILY_LIMIT", 1); // per IP
const GUEST_POOL_LIMIT = num("GUEST_POOL_LIMIT", 30); // all guests together
const EVAL_DAILY_LIMIT = num("EVAL_DAILY_LIMIT", 60); // the evaluation runner, with its own pool
const EVAL_KEY = Deno.env.get("EVAL_KEY") ?? "";

const JWKS = createRemoteJWKSet(new URL(`${SUPABASE_URL}/auth/v1/.well-known/jwks.json`));

type Article = {
  id: string;
  title: string;
  summary: string | null;
  link: string;
  source: string;
  language: string;
  published_at: string;
  similarity: number;
};

type Identity = { scope: "user" | "guest" | "eval"; subject: string; limit: number; pool: string; poolLimit: number };

// --- identity -----------------------------------------------------------------

function timingSafeEqual(a: string, b: string): boolean {
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

/** Returns the caller's identity, or null when an Authorization header is present but invalid. */
async function identify(req: Request): Promise<Identity | null> {
  // The evaluation runner proves itself with a secret header and gets its own small quota.
  const evalKey = req.headers.get("x-eval-key");
  if (evalKey !== null) {
    if (EVAL_KEY && evalKey.length === EVAL_KEY.length && timingSafeEqual(evalKey, EVAL_KEY)) {
      return { scope: "eval", subject: "eval:runner", limit: EVAL_DAILY_LIMIT, pool: "pool:eval", poolLimit: EVAL_DAILY_LIMIT };
    }
    return null;
  }
  const auth = req.headers.get("authorization");
  if (!auth) {
    return { scope: "guest", subject: `guest:${await visitorHash(req)}`, limit: GUEST_DAILY_LIMIT, pool: "pool:guests", poolLimit: GUEST_POOL_LIMIT };
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

// --- gemini -------------------------------------------------------------------

async function geminiPost(path: string, body: unknown, timeoutMs: number) {
  const r = await fetch(`${GEMINI}/${path}`, {
    method: "POST",
    headers: { "x-goog-api-key": GEMINI_KEY, "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(timeoutMs), // never wait forever for the model
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
  }, EMBED_TIMEOUT_MS);
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
    "If several candidates are near-duplicates (the same event with the same facts), keep only the most",
    "informative one. Keep candidates that add a different angle, such as reactions, analysis,",
    "background, market effects or follow-ups: a question about one event can have several useful items.",
    "Return each item as {n, title, sentence}: n is the candidate's number, title is that candidate's",
    "exact title copied unchanged, and sentence is ONE sentence in Turkish, in your own words, based",
    "only on that candidate's title and summary. Do not add facts, numbers or names that are not in",
    "that candidate, and do not use information from other candidates.",
    "Write correct Turkish using all diacritics (ç, ğ, ı, ö, ş, ü, İ).",
    "Always write numbers as digits (for example 30 bin, 249 dolar), never as words.",
    "If no candidate is relevant, return an empty items list.",
    "Also fill `asked`: the specific pieces of information the user literally asked for, as short",
    "Turkish noun phrases (for example \"puan durumu\", \"dolar kuru\"), each with found=true if at",
    "least one candidate contains it and found=false if none does. List only what the user literally",
    "asked for, never aspects they did not mention. For an open question such as \"what happened\"",
    "return an empty list.",
    "",
    `Question: ${question}`,
    "",
    "Candidates:",
    list,
  ].join("\n");
}

const RESPONSE_SCHEMA = {
  type: "OBJECT",
  properties: {
    items: {
      type: "ARRAY",
      items: {
        type: "OBJECT",
        properties: { n: { type: "INTEGER" }, title: { type: "STRING" }, sentence: { type: "STRING" } },
        required: ["n", "title", "sentence"],
      },
    },
    asked: {
      type: "ARRAY",
      items: { type: "OBJECT", properties: { what: { type: "STRING" }, found: { type: "BOOLEAN" } }, required: ["what", "found"] },
    },
  },
  required: ["items", "asked"],
};

async function generate(question: string, articles: Article[]) {
  const prompt = buildPrompt(question, articles);
  let lastError: unknown = new Error("no model configured");
  for (const model of GEN_MODELS) {
    try {
      const data = await geminiPost(`${model}:generateContent`, {
        contents: [{ parts: [{ text: prompt }] }],
        generationConfig: { responseMimeType: "application/json", responseSchema: RESPONSE_SCHEMA, temperature: 0 },
      }, GENERATE_TIMEOUT_MS);
      const parts = data.candidates?.[0]?.content?.parts ?? [];
      const text = parts.filter((p: { thought?: boolean }) => !p.thought).map((p: { text?: string }) => p.text ?? "").join("");
      const parsed = JSON.parse(text) as {
        items: { n: number; title: string; sentence: string }[];
        asked?: { what: string; found: boolean }[];
      };
      // The model only says which asked-for items it could not find; the sentence is built here,
      // so it cannot contradict the answer or wander into things nobody asked about.
      const missing = (parsed.asked ?? [])
        .filter((a) => a && a.found === false && typeof a.what === "string")
        .map((a) => a.what.trim().slice(0, 60))
        .filter((w) => w.length >= 3)
        .slice(0, 3);
      return {
        model,
        picks: parsed.items ?? [],
        notCovered: missing.length ? `Haberlerde ${missing.join(", ")} yer almıyor.` : "",
        usage: data.usageMetadata ?? {},
      };
    } catch (e) {
      lastError = e; // timeout, overload or invalid JSON: try the next model
    }
  }
  throw lastError;
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


// --- guards (same rules as backend/jobs/guards.py; keep them in step) ----------

const NUMBER_WORDS: Record<number, string[]> = {
  1: ["one", "bir"], 2: ["two", "iki"], 3: ["three", "üç"], 4: ["four", "dört"],
  5: ["five", "beş"], 6: ["six", "altı"], 7: ["seven", "yedi"], 8: ["eight", "sekiz"],
  9: ["nine", "dokuz"], 10: ["ten", "on"], 11: ["eleven", "on bir"], 12: ["twelve", "on iki"],
  13: ["thirteen"], 14: ["fourteen"], 15: ["fifteen"], 16: ["sixteen"], 17: ["seventeen"],
  18: ["eighteen"], 19: ["nineteen"], 20: ["twenty", "yirmi"],
};

/** Every number in the sentence must appear in the article's own text, as digits or as a
 *  number word (sources often write "two weeks" while the sentence says "2 hafta"). */
function numbersSupported(sentence: string, sourceText: string): boolean {
  const text = sourceText.toLocaleLowerCase("tr");
  for (const num of sentence.match(/\d+/g) ?? []) {
    if (text.includes(num)) continue;
    const words = num.length <= 2 ? NUMBER_WORDS[Number(num)] ?? [] : [];
    const found = words.some((w) => new RegExp(`(?<!\\p{L})${w}(?!\\p{L})`, "u").test(text));
    if (!found) return false;
  }
  return true;
}

const STEM_LENGTH = 5;
const stemOf = (word: string) => word.split(/['’]/)[0].toLocaleLowerCase("tr").slice(0, STEM_LENGTH);
const wordsOf = (text: string) => text.match(/\p{L}[\p{L}\p{N}_'’]*/gu) ?? [];

/** Capitalised words that look like names. The first word counts only when it clearly is one
 *  ("Name, ...", "Name'nin ...", an all-caps acronym, or a capital inside the word as in OpenAI). */
function nameStems(sentence: string): Set<string> {
  const stems = new Set<string>();
  wordsOf(sentence).forEach((word, i) => {
    const base = word.split(/['’]/)[0];
    if (base.length < 3 || !/^\p{Lu}/u.test(word)) return;
    if (i === 0) {
      const afterName = sentence.slice(word.length).match(/^\s*[,:]/) !== null;
      const allCaps = word === word.toLocaleUpperCase("tr") && word !== word.toLocaleLowerCase("tr");
      const innerCapital = /\p{Lu}/u.test(word.slice(1));
      if (!(afterName || /['’]/.test(word) || allCaps || innerCapital)) return;
    }
    stems.add(stemOf(word));
  });
  return stems;
}

/** False only when the sentence has names or numbers and shares none of them with the source
 *  (and, for Turkish sources, fewer than two ordinary word stems). */
function grounded(sentence: string, sourceText: string, language: string): boolean {
  const names = nameStems(sentence);
  const digits = sentence.match(/\d+/g) ?? [];
  if (names.size === 0 && digits.length === 0) return true;
  const lowered = sourceText.toLocaleLowerCase("tr");
  const sourceStems = new Set(wordsOf(sourceText).filter((w) => w.length >= 4).map(stemOf));
  for (const n of names) if (sourceStems.has(n)) return true;
  if (digits.some((d) => lowered.includes(d))) return true;
  if (language === "tr") {
    const shared = new Set(wordsOf(sentence).filter((w) => w.length >= 4).map(stemOf).filter((x) => sourceStems.has(x)));
    return shared.size >= 2;
  }
  return false;
}

const normaliseTitle = (title: string) =>
  (title ?? "").toLocaleLowerCase("tr").replace(/[^\p{L}\p{N}]+/gu, " ").trim();

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

  const t0 = performance.now(); // total_ms covers everything below, including login and quota checks
  const timings: Record<string, number> = {};
  const lap = (name: string, from: number) => (timings[name] = Math.round(performance.now() - from));

  const who = await identify(req);
  lap("auth_ms", t0);
  if (!who) return json(req, { error: "invalid_token", timings }, 401);

  // Take one unit before any model work; give it back if we cannot answer.
  let used: number;
  const tQuota = performance.now();
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
    lap("quota_ms", tQuota);
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

    // Code-side checks. The model copies the title of the candidate it means; we match on the
    // title (not on the number it wrote) and then require numbers and names to be supported by
    // that article. Links, sources and dates always come from the database.
    const titleIndex = new Map<string, number>();
    articles.forEach((a, i) => { if (!titleIndex.has(normaliseTitle(a.title))) titleIndex.set(normaliseTitle(a.title), i); });
    const seen = new Set<number>();
    const items = [];
    const dropped = { title: 0, number: 0, grounding: 0, other: 0 };
    for (const p of result.picks) {
      const position = titleIndex.get(normaliseTitle(p.title));
      if (position === undefined) { dropped.title++; continue; }
      let sentence = (p.sentence ?? "").trim();
      if (seen.has(position) || !sentence) { dropped.other++; continue; }
      const a = articles[position];
      const sourceText = `${a.title} ${a.summary ?? ""}`;
      let ok = true;
      if (!numbersSupported(sentence, sourceText)) { dropped.number++; ok = false; }
      else if (!grounded(sentence, sourceText, a.language)) { dropped.grounding++; ok = false; }
      if (!ok) {
        if (a.language !== "tr") continue; // an English title does not belong in a Turkish answer
        sentence = a.title;
      }
      seen.add(position);
      items.push({
        sentence,
        title: a.title,
        source: a.source,
        language: a.language,
        date: formatDate(a.published_at),
        published_at: a.published_at,
        link: a.link,
      });
    }
    lap("total_ms", t0);
    return respond({
      mode: items.length ? "answer" : "none_relevant",
      model: result.model,
      items,
      candidates: articles.length,
      dropped,
      not_covered: result.notCovered,
      usage: result.usage,
    });
  } catch (e) {
    refund = true;
    await rpc("refund_quota", { p_subject: who.subject, p_pool: who.pool }).catch(() => {});
    lap("total_ms", t0);
    return respond({ error: String(e).slice(0, 200) }, 502);
  }
});
