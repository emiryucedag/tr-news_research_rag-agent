// Spike S3: question -> embedding -> match_articles -> ONE generation call -> answer.
//
// The model only sees numbered candidates and returns [{n, sentence}]. Links,
// dates and source names are rendered here in code, so the model cannot corrupt
// or invent them. If anything goes wrong we fall back to a plain title + link list.

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
const SPIKE_TOKEN = Deno.env.get("SPIKE_TOKEN")!;

type Article = {
  id: string;
  title: string;
  summary: string | null;
  link: string;
  source: string;
  published_at: string;
  similarity: number;
};

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8" },
  });
}

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
  const r = await fetch(`${SUPABASE_URL}/rest/v1/rpc/match_articles`, {
    method: "POST",
    headers: {
      apikey: SERVICE_KEY,
      Authorization: `Bearer ${SERVICE_KEY}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      query_embedding: `[${vec.join(",")}]`,
      match_count: CANDIDATES,
      since,
    }),
  });
  if (!r.ok) throw new Error(`match_articles ${r.status}: ${(await r.text()).slice(0, 160)}`);
  return await r.json();
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
    generationConfig: {
      responseMimeType: "application/json",
      responseSchema: RESPONSE_SCHEMA,
      temperature: 0,
    },
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

Deno.serve(async (req) => {
  if (req.method !== "POST") return json({ error: "POST only" }, 405);
  if (req.headers.get("x-spike-token") !== SPIKE_TOKEN) return json({ error: "unauthorized" }, 401);

  const body = await req.json().catch(() => ({}));
  const question = String(body.question ?? "").trim();
  if (!question || question.length > MAX_QUESTION_CHARS) {
    return json({ error: `question must be 1-${MAX_QUESTION_CHARS} characters` }, 400);
  }
  const sinceHours = typeof body.since_hours === "number" ? body.since_hours : null;

  const t0 = performance.now();
  const timings: Record<string, number> = {};
  const lap = (name: string, from: number) => (timings[name] = Math.round(performance.now() - from));
  let geminiRequests = 0;

  try {
    let t = performance.now();
    const vec = await embedQuestion(question);
    geminiRequests++;
    lap("embed_ms", t);

    t = performance.now();
    const articles = await matchArticles(vec, sinceHours);
    lap("search_ms", t);
    if (articles.length === 0) {
      return json({ mode: "empty", items: [], gemini_requests: geminiRequests, timings });
    }

    t = performance.now();
    let result;
    try {
      geminiRequests++;
      result = await generate(question, articles);
    } catch (e) {
      lap("generate_ms", t);
      lap("total_ms", t0);
      return json({
        mode: "fallback",
        reason: String(e).slice(0, 200),
        items: plainList(articles),
        gemini_requests: geminiRequests,
        timings,
      });
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
        similarity: Number(a.similarity.toFixed(3)),
      });
    }
    lap("total_ms", t0);
    return json({
      mode: items.length ? "answer" : "none_relevant",
      model: GEN_MODEL,
      items,
      candidates: articles.length,
      dropped_invalid: result.picks.length - items.length,
      usage: result.usage,
      gemini_requests: geminiRequests,
      timings,
    });
  } catch (e) {
    lap("total_ms", t0);
    return json({ error: String(e).slice(0, 200), gemini_requests: geminiRequests, timings }, 502);
  }
});
