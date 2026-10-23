// Reading the text of one news article so a question about it can be answered from what the article
// says. Same rules as backend/jobs/articletext.py and robots.py: the page is fetched only when the
// site's robots.txt allows it and the outlet is not one whose terms restrict this use. The text is
// used for the answer and never stored.

export const AGENT = "news-agent";
export const USER_AGENT = "Mozilla/5.0 (compatible; news-agent/0.1; hobby project)";

// Outlets whose terms restrict this use (keep in step with `terms` in backend/jobs/feeds.py), and
// publishers that forbid it outright. Their pages are never fetched.
const NO_BODY_SOURCES = new Set(["TRT Haber", "NPR"]);
const DENIED_DOMAINS = ["theguardian.com", "ntv.com.tr", "collider.com", "screenrant.com", "trthaber.com", "npr.org"];

const MIN_CHARS = 300;
const MAX_CHARS = 6000;

export function mayReadSource(source: string, url: string): boolean {
  if (NO_BODY_SOURCES.has(source)) return false;
  try {
    const host = new URL(url).hostname;
    return !DENIED_DOMAINS.some((d) => host === d || host.endsWith(`.${d}`));
  } catch {
    return false;
  }
}

// --- robots.txt (RFC 9309, the parts that matter here) ------------------------------------

type Rule = { allow: boolean; pattern: RegExp; length: number };
const robotsCache = new Map<string, Rule[] | null | "unreachable">();

function ruleToRegExp(path: string): RegExp {
  const escaped = path.replace(/[.+?^${}()|[\]\\]/g, "\\$&").replace(/\*/g, ".*").replace(/\\\$$/, "$");
  return new RegExp(`^${escaped}`);
}

export function parseRobots(text: string, agent = AGENT): Rule[] {
  const groups: { agents: string[]; rules: Rule[] }[] = [];
  let current: { agents: string[]; rules: Rule[] } | null = null;
  let lastWasAgent = false;
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.replace(/#.*/, "").trim();
    const m = line.match(/^([A-Za-z-]+)\s*:\s*(.*)$/);
    if (!m) continue;
    const field = m[1].toLowerCase();
    const value = m[2].trim();
    if (field === "user-agent") {
      if (!lastWasAgent || !current) { current = { agents: [], rules: [] }; groups.push(current); }
      current.agents.push(value.toLowerCase());
      lastWasAgent = true;
    } else if ((field === "allow" || field === "disallow") && current) {
      lastWasAgent = false;
      if (value) current.rules.push({ allow: field === "allow", pattern: ruleToRegExp(value), length: value.length });
    } else {
      lastWasAgent = false;
    }
  }
  const mine = groups.filter((g) => g.agents.some((a) => a !== "*" && agent.toLowerCase().includes(a)));
  const chosen = mine.length ? mine : groups.filter((g) => g.agents.includes("*"));
  return chosen.flatMap((g) => g.rules);
}

export function allowedByRules(rules: Rule[], path: string): boolean {
  let best: Rule | null = null;
  for (const r of rules) {
    if (r.pattern.test(path) && (!best || r.length > best.length || (r.length === best.length && r.allow))) best = r;
  }
  return best ? best.allow : true;
}

export async function robotsAllows(url: string): Promise<boolean> {
  const u = new URL(url);
  const origin = u.origin;
  if (!robotsCache.has(origin)) {
    try {
      const r = await fetch(`${origin}/robots.txt`, { headers: { "User-Agent": USER_AGENT }, signal: AbortSignal.timeout(5000) });
      if (r.status === 200) robotsCache.set(origin, parseRobots(await r.text()));
      else if (r.status >= 400 && r.status < 500) robotsCache.set(origin, null); // no robots.txt: no restrictions
      else robotsCache.set(origin, "unreachable");
    } catch {
      robotsCache.set(origin, "unreachable");
    }
  }
  const rules = robotsCache.get(origin);
  if (rules === null) return true;
  if (rules === "unreachable" || rules === undefined) return false;
  return allowedByRules(rules, u.pathname + u.search);
}

// --- text extraction -----------------------------------------------------------------------

const decode = (s: string) =>
  s.replace(/&nbsp;/g, " ").replace(/&amp;/g, "&").replace(/&quot;/g, '"').replace(/&#0?39;|&apos;/g, "'")
    .replace(/&lt;/g, "<").replace(/&gt;/g, ">")
    .replace(/&#(\d+);/g, (_, n) => String.fromCodePoint(Number(n)))
    .replace(/&#x([0-9a-f]+);/gi, (_, n) => String.fromCodePoint(parseInt(n, 16)));
const squash = (s: string) => s.replace(/\s+/g, " ").trim();

function jsonLdBody(page: string): string {
  const re = /<script[^>]+type=["']application\/ld\+json["'][^>]*>([\s\S]*?)<\/script>/gi;
  for (const m of page.matchAll(re)) {
    let data: unknown;
    try { data = JSON.parse(m[1].trim()); } catch { continue; }
    const stack: unknown[] = Array.isArray(data) ? [...data] : [data];
    while (stack.length) {
      const node = stack.pop();
      if (Array.isArray(node)) stack.push(...node);
      else if (node && typeof node === "object") {
        const body = (node as Record<string, unknown>).articleBody;
        if (typeof body === "string" && body.length >= MIN_CHARS) return squash(decode(body.replace(/<[^>]+>/g, " ")));
        stack.push(...Object.values(node as object).filter((v) => v && typeof v === "object"));
      }
    }
  }
  return "";
}

function paragraphsOf(html: string): string[] {
  return [...html.matchAll(/<p[\s>][\s\S]*?<\/p>/gi)]
    .map((m) => squash(decode(m[0].replace(/<[^>]+>/g, " "))))
    .filter((t) => t.length >= 60);
}

export function extractArticleText(page: string): string {
  const fromLd = jsonLdBody(page);
  if (fromLd) return fromLd.slice(0, MAX_CHARS);
  const clean = page
    .replace(/<(script|style|noscript|nav|header|footer|aside|form|figure|button)[\s\S]*?<\/\1>/gi, " ");
  const articles = [...clean.matchAll(/<article[\s>][\s\S]*?<\/article>/gi)].map((m) => m[0]).join(" ");
  const inArticle = articles ? paragraphsOf(articles) : [];
  const text = (inArticle.length ? inArticle : paragraphsOf(clean)).join(" ");
  return text.length >= MIN_CHARS ? text.slice(0, MAX_CHARS) : "";
}

/** The article text of a page, or "" when it may not be read, cannot be fetched or has no article. */
export async function fetchArticleText(url: string, source = ""): Promise<string> {
  if (!mayReadSource(source, url)) return "";
  try {
    if (!(await robotsAllows(url))) return "";
    const r = await fetch(url, { headers: { "User-Agent": USER_AGENT }, redirect: "follow", signal: AbortSignal.timeout(8000) });
    if (!r.ok || !(r.headers.get("content-type") ?? "").includes("html")) return "";
    return extractArticleText(await r.text());
  } catch {
    return "";
  }
}
