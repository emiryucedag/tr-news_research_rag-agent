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
import { json, corsHeaders, nextResetIso, restGet, rpc, visitorHash } from "../_shared/http.ts";
import { fetchArticleText, mayReadSource } from "../_shared/articletext.ts";

const GEMINI = "https://generativelanguage.googleapis.com/v1beta/models";
const EMBED_MODEL = "gemini-embedding-001";
const EMBED_DIMS = 768;
// Primary model first, then a second one with its own free-tier quota if the first fails.
const GEN_MODELS = [...new Set([Deno.env.get("GEN_MODEL") ?? "gemini-3.5-flash-lite", "gemini-3.1-flash-lite"])];
const EMBED_TIMEOUT_MS = 8000;
const GENERATE_TIMEOUT_MS = 10000; // per model
const CANDIDATES = 14;
const KEYWORD_CANDIDATES = 10;
const FALLBACK_ITEMS = 5;
const ANSWER_TIMEOUT_MS = 20000; // reading a whole article takes longer than picking titles
const WEB_PAGES = 3;
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

// Words that carry no topic: commands and question words. What is left are the names and subjects.
const FILLER = new Set([
  "getir", "göster", "bul", "ver", "anlat", "söyle", "listele", "özetle", "haber", "haberi", "haberler", "haberleri",
  "haberlerini", "son", "dakika", "durum", "durumu", "neler", "nedir", "nasıl", "nerede", "kimdir", "hakkında", "ilgili",
  "var", "mı", "mi", "mu", "mü", "bana", "bize", "lütfen", "şu", "bugün", "dün", "gelişme", "gelişmeler", "gelişmeleri",
  "ne", "oldu", "olmuş", "yaşandı", "yaşanan", "hakkındaki", "dair", "için", "gibi", "kadar", "bir", "bu", "ve", "ile",
]);

/** The topic words of a question: no commands or filler, at most 4, cut to 6 letters so that Turkish
 *  suffixes still match ("galatasaray" also finds "Galatasaray'ın"). */
function topicTerms(text: string): string[] {
  const words = (text.match(/\p{L}[\p{L}\p{N}]*/gu) ?? []).map((w) => w.toLocaleLowerCase("tr"));
  const kept = words.filter((w) => w.length >= 3 && !FILLER.has(w));
  return [...new Set(kept.map((w) => w.slice(0, 6)))].slice(0, 4);
}

async function keywordArticles(terms: string[], sinceHours: number | null): Promise<(Article & { hits: number })[]> {
  if (terms.length === 0) return [];
  const since = sinceHours ? new Date(Date.now() - sinceHours * 3600_000).toISOString() : new Date(Date.now() - 72 * 3600_000).toISOString();
  const rows = await rpc("keyword_articles", { terms, since, match_count: KEYWORD_CANDIDATES }).catch(() => []);
  return (rows ?? []).map((r: Article & { hits: number }) => ({ ...r, similarity: 0, hits: r.hits }));
}

function buildPrompt(question: string, articles: Article[], aboutTitle: string | null): string {
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
    "If the question only names a subject (a bare name, or a command such as \"X haberlerini getir\"), treat it",
    "as a request for the news about X and select every candidate that is about X. If it asks for something",
    "specific, answer exactly that. Return an empty items list only when no candidate is about the subject.",
    "Also fill `asked`: the specific pieces of information the user literally asked for, as short",
    "Turkish noun phrases (for example \"puan durumu\", \"dolar kuru\"), each with found=true if at",
    "least one candidate actually states it (a candidate about the same event that does not state it does not",
    "count) and found=false if none does. List only what the user literally",
    "asked for, never aspects they did not mention. For an open question such as \"what happened\"",
    "return an empty list.",
    "",
    ...(aboutTitle
      ? [`The user is reading the story "${aboutTitle}" and the question is about it. Answer about that story,`,
         "using other candidates only when they report the same event or add closely related facts.", ""]
      : []),
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

async function generate(question: string, articles: Article[], aboutTitle: string | null) {
  const prompt = buildPrompt(question, articles, aboutTitle);
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

// --- answering from an article's own text ---------------------------------------------------

const ANSWER_SCHEMA = {
  type: "OBJECT",
  properties: { found: { type: "BOOLEAN" }, answer: { type: "STRING" }, used: { type: "ARRAY", items: { type: "INTEGER" } } },
  required: ["found", "answer", "used"],
};

type Source = { title: string; source: string; link: string };

/** One request: answer the question from numbered texts. Returns null when the texts do not contain
 *  the answer, or when the answer fails the same checks the title-based answers go through. */
async function answerFromTexts(
  question: string,
  texts: { title: string; text: string; source: Source; language: string }[],
): Promise<{ answer: string; sources: Source[]; model: string } | null> {
  const blocks = texts.map((t, i) => `[${i + 1}] ${t.title}\n${t.text.slice(0, 2500)}`).join("\n\n");
  const prompt = [
    "A reader asks a question about a news story. Answer it using ONLY the numbered article texts below.",
    "Return {found, answer, used}. found is true only when the texts actually contain the answer.",
    "answer: Turkish, 1 to 4 sentences, direct. Say what happened or what was said, with the figures and",
    "names the texts give, in your own words. Never say that something was 'explained' or 'stated' without",
    "saying what. Add nothing from your own knowledge. If found is false, answer is an empty string.",
    "used: the numbers of the texts the answer relies on.",
    "Write correct Turkish with all diacritics. Write numbers as digits.",
    "",
    `Question: ${question}`,
    "",
    "Texts:",
    blocks,
  ].join("\n");
  for (const model of GEN_MODELS) {
    try {
      const data = await geminiPost(`${model}:generateContent`, {
        contents: [{ parts: [{ text: prompt }] }],
        generationConfig: { responseMimeType: "application/json", responseSchema: ANSWER_SCHEMA, temperature: 0 },
      }, ANSWER_TIMEOUT_MS);
      const parts = data.candidates?.[0]?.content?.parts ?? [];
      const raw = parts.filter((p: { thought?: boolean }) => !p.thought).map((p: { text?: string }) => p.text ?? "").join("");
      const parsed = JSON.parse(raw) as { found: boolean; answer: string; used: number[] };
      const answer = (parsed.answer ?? "").trim();
      if (!parsed.found || !answer || answer.length > 700) return null;
      const used = (parsed.used ?? []).filter((n) => Number.isInteger(n) && n >= 1 && n <= texts.length);
      const chosen = used.length ? used.map((n) => texts[n - 1]) : texts;
      const support = chosen.map((t) => `${t.title} ${t.text}`).join(" ");
      if (!numbersSupported(answer, support) || !grounded(answer, support, chosen[0].language)) return null;
      return { answer, sources: chosen.map((t) => t.source), model };
    } catch {
      // timeout, overload or invalid JSON: try the next model
    }
  }
  return null;
}

const STOP = new Set(["için", "gibi", "kadar", "sonra", "önce", "olan", "oldu", "haber", "haberi", "ile", "daha", "bir"]);

/** Free news search (GDELT). Best effort: it limits requests and sometimes returns nothing. */
async function webSearch(title: string, ownLink: string): Promise<Source[]> {
  const words = [...new Set((title.match(/\p{L}[\p{L}\p{N}]{3,}/gu) ?? []).filter((w) => !STOP.has(w.toLocaleLowerCase("tr"))))];
  const keywords = words.sort((a, b) => b.length - a.length).slice(0, 5).join(" ");
  if (!keywords) return [];
  const url = "https://api.gdeltproject.org/api/v2/doc/doc?mode=artlist&format=json&maxrecords=12&sort=hybridrel&timespan=7d" +
    `&query=${encodeURIComponent(keywords)}`;
  try {
    const r = await fetch(url, { signal: AbortSignal.timeout(8000) });
    if (!r.ok) return [];
    const data = await r.json().catch(() => ({}));
    const seenHosts = new Set<string>();
    const found: Source[] = [];
    for (const a of data.articles ?? []) {
      if (!a?.url || a.url === ownLink || !mayReadSource("", a.url)) continue;
      const host = new URL(a.url).hostname;
      if (seenHosts.has(host)) continue; // one page per site
      seenHosts.add(host);
      found.push({ title: String(a.title ?? host).slice(0, 160), source: String(a.domain ?? host), link: a.url });
    }
    return found.slice(0, 6);
  } catch {
    return [];
  }
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
  const aboutLink = typeof body.about === "string" && /^https?:\/\//.test(body.about) && body.about.length <= 600
    ? body.about : null;
  // A question about one story streams its progress (one JSON object per line); other questions
  // get a single JSON answer as before.
  const streaming = body.stream === true && aboutLink !== null;

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
  const payloadOf = (payload: Record<string, unknown>) => {
    const spent = refund ? used - 1 : used;
    return {
      ...payload,
      scope: who.scope,
      quota: { limit: who.limit, used: spent, remaining: Math.max(who.limit - spent, 0), resets_at: nextResetIso() },
      gemini_requests: geminiRequests,
      timings,
    };
  };
  const giveBack = async () => {
    if (refund) return; // once only: several steps may fail in one question
    refund = true;
    await rpc("refund_quota", { p_subject: who.subject, p_pool: who.pool }).catch(() => {});
  };

  /** Titles and one-sentence summaries from the three-day archive (the path every question can take). */
  async function archiveAnswer(aboutArticle: Article | null): Promise<Record<string, unknown>> {
    let t = performance.now();
    const vec = await embedQuestion((aboutArticle ? `${aboutArticle.title}. ${question}` : question).slice(0, 600));
    geminiRequests++;
    lap("embed_ms", t);

    t = performance.now();
    const vectorHits = await matchArticles(vec, sinceHours);
    const terms = topicTerms(aboutArticle ? `${aboutArticle.title} ${question}` : question);
    const keywordHits = await keywordArticles(terms, sinceHours);
    // Keyword hits first (they include articles that have no embedding yet), then the vector neighbours.
    const merged = new Map<string, Article>();
    for (const a of [...(aboutArticle ? [aboutArticle] : []), ...keywordHits, ...vectorHits]) if (!merged.has(a.id)) merged.set(a.id, a);
    const articles = [...merged.values()].slice(0, CANDIDATES);
    lap("search_ms", t);
    if (articles.length === 0) return { mode: "empty", items: [] };

    t = performance.now();
    let result;
    try {
      geminiRequests++;
      result = await generate(question, articles, aboutArticle?.title ?? null);
    } catch (e) {
      lap("generate_ms", t);
      await giveBack();
      return { mode: "fallback", reason: String(e).slice(0, 200), items: plainList(articles) };
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
    // The model chose nothing. Show the keyword hits only when they are plainly the answer: every topic
    // word of the question appears in them. A loose partial match would list unrelated news.
    const exact = keywordHits.filter((a) => a.hits === terms.length);
    if (!items.length && terms.length > 0 && terms.length <= 2 && exact.length) {
      return { mode: "fallback", reason: "model selected nothing", items: plainList(exact), candidates: articles.length };
    }
    return {
      mode: items.length ? "answer" : "none_relevant",
      model: result.model,
      items,
      candidates: articles.length,
      dropped,
      not_covered: result.notCovered,
      usage: result.usage,
    };
  }

  async function lookupStory(): Promise<Article | null> {
    if (!aboutLink) return null;
    const rows = await restGet(
      "articles",
      `select=id,title,summary,link,source,language,published_at&link=eq.${encodeURIComponent(aboutLink)}&limit=1`,
    ).catch(() => []);
    return rows.length ? { ...rows[0], similarity: 1 } : null;
  }

  /** The order for a question about one story: the story's own page, then the archive, then the web.
   *  `say` tells the reader which step is running. */
  async function aboutAnswer(story: Article, say: (text: string) => void): Promise<Record<string, unknown>> {
    const own: Source = { title: story.title, source: story.source, link: story.link };
    let t = performance.now();

    say("Haberin sayfasını okuyorum…");
    const text = await fetchArticleText(story.link, story.source);
    lap("page_ms", t);
    if (text) {
      t = performance.now();
      geminiRequests++;
      const hit = await answerFromTexts(question, [{ title: story.title, text, source: own, language: story.language }]);
      lap("page_answer_ms", t);
      if (hit) return { mode: "text", origin: "story", answer: hit.answer, sources: hit.sources, model: hit.model };
      say("Haberde bu ayrıntı yok. Arşivdeki diğer haberlere bakıyorum…");
    } else {
      say("Haberin metnine ulaşamadım. Arşivdeki haberlere bakıyorum…");
    }

    const archive = await archiveAnswer(story);
    const items = (archive.items ?? []) as unknown[];
    if (archive.mode === "answer" && items.length > 1) return { ...archive, origin: "archive" };

    say("Arşivde de yeterli bilgi yok. İnternette arıyorum…");
    t = performance.now();
    const found = await webSearch(story.title, story.link);
    lap("web_search_ms", t);
    if (found.length === 0) {
      return { ...archive, mode: "not_found", web: "none" };
    }
    say("Bulunan sayfaları okuyorum…");
    t = performance.now();
    const pages = (await Promise.all(found.map(async (f) => ({ f, text: await fetchArticleText(f.link, f.source) }))))
      .filter((p) => p.text).slice(0, WEB_PAGES);
    lap("web_read_ms", t);
    if (pages.length) {
      geminiRequests++;
      const hit = await answerFromTexts(
        `${story.title}. ${question}`,
        pages.map((p) => ({ title: p.f.title, text: p.text, source: p.f, language: "tr" })),
      );
      if (hit) return { mode: "text", origin: "web", answer: hit.answer, sources: hit.sources, model: hit.model };
    }
    return { ...archive, mode: "not_found", web: "searched" };
  }

  try {
    const story = await lookupStory();

    if (streaming && story) {
      const encoder = new TextEncoder();
      const stream = new ReadableStream({
        async start(controller) {
          const send = (obj: unknown) => controller.enqueue(encoder.encode(JSON.stringify(obj) + "\n"));
          try {
            const result = await aboutAnswer(story, (text) => send({ type: "status", text }));
            if (result.mode === "empty" || result.mode === "not_found") await giveBack();
            lap("total_ms", t0);
            send({ type: "result", ...payloadOf(result) });
          } catch (e) {
            await giveBack();
            send({ type: "result", ...payloadOf({ error: String(e).slice(0, 200) }) });
          }
          controller.close();
        },
      });
      return new Response(stream, {
        headers: { "Content-Type": "application/x-ndjson; charset=utf-8", "Cache-Control": "no-store", ...corsHeaders(req) },
      });
    }

    const result = await archiveAnswer(story);
    if (result.mode === "empty") await giveBack();
    lap("total_ms", t0);
    return json(req, payloadOf(result));
  } catch (e) {
    await giveBack();
    lap("total_ms", t0);
    return json(req, payloadOf({ error: String(e).slice(0, 200) }), 502);
  }
});
