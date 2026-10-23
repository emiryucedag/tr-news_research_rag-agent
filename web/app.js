import { SUPABASE_URL, SUPABASE_PUBLISHABLE_KEY } from "./config.js";

// supabase-js (vendor/) keeps the session in the browser and refreshes it by itself, so a user
// signs in once and stays signed in until they log out or clear site data.
const supabase = window.supabase.createClient(SUPABASE_URL, SUPABASE_PUBLISHABLE_KEY);
const ASK_URL = `${SUPABASE_URL}/functions/v1/ask`;
const INVITE_URL = `${SUPABASE_URL}/functions/v1/request-invite`;

const SECTIONS = [
  { id: "gundem", label: "Gündem", blurb: "Türkiye’den ve çevreden günün öne çıkanları" },
  { id: "siyaset", label: "Siyaset", blurb: "Hükümet, meclis, partiler ve yargı" },
  { id: "spor", label: "Spor", blurb: "Maç sonuçları, transferler ve sporun gündemi" },
  { id: "ekonomi", label: "Ekonomi", blurb: "Piyasalar, faiz, döviz ve şirketler" },
  { id: "teknoloji", label: "Teknoloji", blurb: "Cihazlar, yazılım ve teknoloji dünyası" },
  { id: "dunya", label: "Dünya", blurb: "Sınırların ötesinden gelişmeler" },
  { id: "yapay-zeka", label: "Yapay Zekâ", blurb: "Modeller, şirketler ve araştırmalar" },
  { id: "populer-kultur", label: "Popüler Kültür", blurb: "Dizi, film, müzik ve ünlüler" },
];
const SECTION = Object.fromEntries(SECTIONS.map((s) => [s.id, s]));

// Section marks, drawn for this site: simple geometric shapes, one per section, no emoji.
const GLYPHS = {
  gundem: '<circle cx="12" cy="12" r="8.5"/><circle cx="12" cy="12" r="2.6" fill="currentColor" stroke="none"/>',
  siyaset: '<path d="M3.5 20.5h17M6 20.5V10.5M10 20.5V10.5M14 20.5V10.5M18 20.5V10.5M3.5 10.5 12 4l8.5 6.5"/>',
  spor: '<circle cx="12" cy="13.5" r="7.5"/><path d="M12 13.5V9M9.5 3h5M18 6.5l1.6-1.6"/>',
  ekonomi: '<path d="M5.5 20.5v-6M12 20.5V8.5M18.5 20.5V4M3 20.5h18"/>',
  teknoloji: '<rect x="7" y="7" width="10" height="10" rx="1.6"/><path d="M10 3.5v3.5M14 3.5v3.5M10 17v3.5M14 17v3.5M3.5 10H7M3.5 14H7M17 10h3.5M17 14h3.5"/>',
  dunya: '<circle cx="12" cy="12" r="8.5"/><ellipse cx="12" cy="12" rx="3.8" ry="8.5"/><path d="M3.5 12h17"/>',
  "yapay-zeka": '<circle cx="6" cy="7" r="2.2"/><circle cx="18" cy="7" r="2.2"/><circle cx="12" cy="18" r="2.2"/><path d="M8.2 7h7.6M7.2 9l3.8 7M16.8 9l-3.8 7"/>',
  "populer-kultur": '<rect x="3.5" y="5" width="17" height="14" rx="2.4"/><path d="M10.2 9.4v5.2l4.4-2.6z" fill="currentColor"/>',
  close: '<path d="M6 6l12 12M18 6 6 18"/>',
  external: '<path d="M14 4h6v6M20 4l-9 9M18 14v4.5a1.5 1.5 0 0 1-1.5 1.5h-11A1.5 1.5 0 0 1 4 18.5v-11A1.5 1.5 0 0 1 5.5 6H10"/>',
  sliders: '<path d="M4 7h9M17 7h3M4 17h3M11 17h9"/><circle cx="15" cy="7" r="2"/><circle cx="9" cy="17" r="2"/>',
};

const $ = (id) => document.getElementById(id);
const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

// --- small helpers ----------------------------------------------------------

/** Build an element. Children that are strings become text nodes, never HTML. */
function h(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) node.setAttribute(key, value);
  for (const child of children) node.append(child);
  return node;
}

/** Put one of our own constant glyphs into an <svg>. The markup is fixed above, never user data. */
function setGlyph(svg, name) {
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.classList.add("ico");
  svg.innerHTML = GLYPHS[name];
}

function glyph(name, className = "") {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  setGlyph(svg, name);
  svg.setAttribute("aria-hidden", "true");
  if (className) svg.setAttribute("class", `ico ${className}`);
  return svg;
}

/** Only http(s) links are allowed; anything else becomes an inert "#". */
function safeUrl(value) {
  try {
    const url = new URL(value);
    return url.protocol === "https:" || url.protocol === "http:" ? url.href : "#";
  } catch {
    return "#";
  }
}

const istanbul = { timeZone: "Europe/Istanbul" };
const dateTimeFormat = new Intl.DateTimeFormat("tr-TR", { ...istanbul, day: "numeric", month: "long", hour: "2-digit", minute: "2-digit" });
const relative = new Intl.RelativeTimeFormat("tr", { numeric: "auto" });
const formatDateTime = (iso) => dateTimeFormat.format(new Date(iso));

/** "2 saat önce", "dün": the exact time stays available as a tooltip. */
function relativeTime(iso) {
  const minutes = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
  if (minutes < 2) return "az önce";
  if (minutes < 60) return relative.format(-minutes, "minute");
  const hours = Math.round(minutes / 60);
  if (hours < 24) return relative.format(-hours, "hour");
  return relative.format(-Math.round(hours / 24), "day");
}

const cap = (text) => text.charAt(0).toLocaleUpperCase("tr") + text.slice(1);
const isCatchAll = (topic) => topic.startsWith("diğer");

/** A short, stable id for a story, taken from its link, so a story can be linked to. */
function storyId(link) {
  let hash = 0x811c9dc5;
  for (let i = 0; i < link.length; i++) hash = Math.imul(hash ^ link.charCodeAt(i), 0x01000193) >>> 0;
  return hash.toString(36);
}

function setContent(container, ...nodes) {
  container.replaceChildren(...nodes);
}

async function accessToken() {
  const { data } = await supabase.auth.getSession();
  return data.session?.access_token ?? null;
}

// --- what the reader opens: kept in this browser only --------------------------------------

const PREFS_KEY = "prefs-v1";
const HALF_LIFE_MS = 14 * 24 * 3600 * 1000;   // an old interest counts for half after two weeks
const ENOUGH_EVENTS = 6;                       // before this, nothing is reordered or suggested
const DECLINE_MEMORY_MS = 30 * 24 * 3600 * 1000;

function freshPrefs() {
  return { events: [], pinned: {}, declined: {}, reorder: true };
}

function loadPrefs() {
  try {
    return { ...freshPrefs(), ...JSON.parse(localStorage.getItem(PREFS_KEY) ?? "{}") };
  } catch {
    return freshPrefs();
  }
}

let prefs = loadPrefs();

function savePrefs() {
  try {
    localStorage.setItem(PREFS_KEY, JSON.stringify(prefs));
  } catch {
    // Storage can be blocked (private windows); personalisation is a convenience only.
  }
}

/** Remember that something in a section (and, if known, a sub-topic) was opened. */
function record(section, topic, weight = 1) {
  prefs.events.push({ t: Date.now(), s: section, k: topic ?? null, w: weight });
  prefs.events = prefs.events.slice(-300);
  savePrefs();
}

const decayed = (event) => event.w * 0.5 ** ((Date.now() - event.t) / HALF_LIFE_MS);

function sectionScores() {
  const scores = Object.fromEntries(SECTIONS.map((s) => [s.id, 0]));
  for (const e of prefs.events) if (e.s in scores) scores[e.s] += decayed(e);
  return scores;
}

const hasEnoughData = () => Object.values(sectionScores()).reduce((a, b) => a + b, 0) >= ENOUGH_EVENTS;

/** Sections in display order: by how much they are read once there is enough to go on. */
function orderedSections() {
  if (!prefs.reorder || !hasEnoughData()) return SECTIONS;
  const scores = sectionScores();
  return [...SECTIONS].sort((a, b) => scores[b.id] - scores[a.id]);   // stable: ties keep the default order
}

/** A sub-topic that clearly dominates what the reader opens in a section, and is not yet a heading. */
function suggestionFor(section) {
  const mine = prefs.events.filter((e) => e.s === section && e.k && !isCatchAll(e.k));
  if (mine.length < 5) return null;
  const totals = {};
  for (const e of mine) totals[e.k] = (totals[e.k] ?? 0) + decayed(e);
  const sum = Object.values(totals).reduce((a, b) => a + b, 0);
  const [topic, score] = Object.entries(totals).sort((a, b) => b[1] - a[1])[0];
  if (score / sum < 0.6) return null;
  if ((prefs.pinned[section] ?? []).includes(topic)) return null;
  if (Date.now() - (prefs.declined[`${section}:${topic}`] ?? 0) < DECLINE_MEMORY_MS) return null;
  return topic;
}

// --- reading the data ------------------------------------------------------------------

const cache = new Map();   // section id -> { row, at }
const CACHE_MS = 10 * 60 * 1000;

function normalise(item) {
  return {
    ...item,
    headline: item.headline || item.sentence || item.title || "",
    sentence: item.sentence || item.title || "",
    detail: item.detail || "",
    topic: item.topic || "diğer",
    also: item.also || [],
    id: storyId(item.link),
  };
}

async function loadSection(id, { force = false } = {}) {
  const hit = cache.get(id);
  if (hit && !force && Date.now() - hit.at < CACHE_MS) return hit.row;
  const { data, error } = await supabase
    .from("category_summaries")
    .select("mode, items, generated_at, newest_article_at")
    .eq("category", id)
    .maybeSingle();
  if (error) throw error;
  const row = data && { ...data, items: (data.items ?? []).map(normalise) };
  if (row) cache.set(id, { row, at: Date.now() });
  return row;
}

// --- the pulse: how long ago the job last checked ---------------------------------------

async function refreshPulse() {
  const pulse = $("pulse");
  const { data, error } = await supabase.from("pipeline_status").select("ingested_at, summarized_at").eq("id", "main").maybeSingle();
  if (error || !data) {
    pulse.textContent = "Güncelleme durumu bilinmiyor";
    return;
  }
  const last = [data.ingested_at, data.summarized_at].filter(Boolean).sort().pop();
  if (!last) return;
  const minutes = (Date.now() - new Date(last).getTime()) / 60000;
  const late = minutes > 90;
  pulse.dataset.state = late ? "late" : "ok";
  pulse.title = `Son kontrol: ${formatDateTime(last)}`;
  pulse.textContent = late ? `Güncelleme gecikmiş, son kontrol ${relativeTime(last)}` : `Haberler ${relativeTime(last)} kontrol edildi`;
}

// --- the section rail ---------------------------------------------------------------------

let current = SECTIONS[0].id;

function renderRail() {
  const rail = $("rail");
  const buttons = orderedSections().map((s) => {
    const button = h("button", { type: "button", class: "rail-tab", "aria-current": String(s.id === current),
      style: `--tab: var(--${s.id}-accent); --tab-on: var(--${s.id}-on)` }, s.label);
    button.prepend(glyph(s.id));
    button.addEventListener("click", () => goSection(s.id));
    return button;
  });
  const nodes = [...buttons];
  if (prefs.reorder && hasEnoughData()) {
    const note = h("p", { class: "rail-note" }, "Bölümler okuma alışkanlığına göre sıralı. ");
    const open = h("button", { type: "button", class: "text-btn" }, "Ayarlar");
    open.addEventListener("click", openPrefs);
    note.append(open);
    nodes.push(note);
  }
  setContent(rail, ...nodes);
}

// --- a section's page ---------------------------------------------------------------------

let activeTopic = null;
let currentRow = null;

function skeleton() {
  const rows = h("div", { "aria-hidden": "true" });
  for (let i = 0; i < 3; i++) rows.append(h("div", { class: "skeleton" }, h("span"), h("span"), h("span")));
  return rows;
}

function storyNode(item, index, { lead = false, compact = false } = {}) {
  const story = h("article", { class: ["story", lead && "lead", compact && "compact"].filter(Boolean).join(" "), style: `--i: ${index}` });
  if (readSet().has(item.link)) story.classList.add("is-read");

  const open = h("button", { type: "button", class: "story-open", "aria-haspopup": "dialog" }, h("h3", { class: "headline" }, item.headline));
  open.addEventListener("click", () => openStory(item, current));

  const meta = h("p", { class: "meta" }, h("span", { class: "src" }, item.source));
  meta.append(h("time", { datetime: item.published_at, title: formatDateTime(item.published_at) }, relativeTime(item.published_at)));
  if (item.language === "en") meta.append(h("span", { class: "chip", title: "İngilizce kaynak, Türkçeye özetlendi" }, "EN"));
  if (!isCatchAll(item.topic)) meta.append(h("span", { class: "chip topic-chip" }, cap(item.topic)));
  meta.append(sourceLink(item, "Kaynağa git"));

  story.append(open);
  if (!compact && item.sentence && item.sentence !== item.headline) story.append(h("p", { class: "dek" }, item.sentence));
  story.append(meta);
  return story;
}

/** The outlet link. Opening it counts as interest in the story's topic, and fades the story. */
function sourceLink(item, label) {
  const link = h("a", { class: "ext", href: safeUrl(item.link), target: "_blank", rel: "noopener noreferrer",
    "aria-label": `${label}: ${item.source}` }, label);
  link.append(glyph("external"));
  link.addEventListener("click", () => {
    markRead(item.link);
    record(current, item.topic, 1.5);
    link.closest(".story")?.classList.add("is-read");
  });
  return link;
}

/** One lead story, then the rest of the front page, then a plainer list under "more". */
function storiesGrid(items, { withLead = true } = {}) {
  const grid = h("div", { class: "stories reveal" });
  items.forEach((item, i) => {
    if (i === 8) grid.append(h("h3", { class: "more-title" }, "Daha fazla haber"));
    grid.append(storyNode(item, i, { lead: withLead && i === 0, compact: i >= 8 }));
  });
  return grid;
}

function renderTopics(items) {
  const bar = $("topics");
  const counts = {};
  for (const i of items) counts[i.topic] = (counts[i.topic] ?? 0) + 1;
  const topics = Object.entries(counts).sort((a, b) => b[1] - a[1]);
  if (topics.length < 2) {
    bar.hidden = true;
    return;
  }
  const chip = (label, count, value) => {
    const button = h("button", { type: "button", class: "topic", "aria-pressed": String(activeTopic === value) }, label);
    button.append(h("span", { class: "n" }, String(count)));
    button.addEventListener("click", () => {
      activeTopic = activeTopic === value ? null : value;
      if (activeTopic) record(current, activeTopic, 0.5);
      renderSection();
    });
    return button;
  };
  setContent(bar, chip("Tümü", items.length, null), ...topics.map(([t, n]) => chip(cap(t), n, t)));
  bar.hidden = false;
}

function renderSuggestion() {
  const box = $("suggestion");
  const topic = suggestionFor(current);
  if (!topic) {
    box.hidden = true;
    box.replaceChildren();
    return;
  }
  const yes = h("button", { type: "button", class: "btn light" }, `${cap(topic)} haberlerini üste al`);
  const no = h("button", { type: "button", class: "btn ghost" }, "Şimdilik değil");
  yes.addEventListener("click", () => {
    prefs.pinned[current] = [...(prefs.pinned[current] ?? []), topic];
    savePrefs();
    renderSection();
  });
  no.addEventListener("click", () => {
    prefs.declined[`${current}:${topic}`] = Date.now();
    savePrefs();
    renderSuggestion();
  });
  box.className = "suggest";
  setContent(box,
    h("p", {}, `${SECTION[current].label} bölümünde en çok ${topic} haberlerini açıyorsun. ${cap(topic)} haberlerini ayrı bir başlık olarak en üste alalım mı?`),
    h("div", { class: "row" }, yes, no));
  box.hidden = false;
}

/** Draws the current section from currentRow, honouring the topic filter and pinned topics. */
function renderSection() {
  const box = $("summary");
  const row = currentRow;
  if (!row || row.mode === "empty" || !row.items.length) {
    $("topics").hidden = true;
    $("suggestion").hidden = true;
    setContent(box, h("p", { class: "note" }, "Bu bölümde şu an gösterilecek haber yok. Haberler saat başı yenileniyor."));
    return;
  }
  const items = row.items;
  renderTopics(items);
  renderSuggestion();

  const nodes = [];
  if (row.mode === "fallback") nodes.push(h("p", { class: "note" }, "Özet hazırlanamadı, son başlıklar gösteriliyor."));

  const pinned = (prefs.pinned[current] ?? []).filter((t) => items.some((i) => i.topic === t));
  if (activeTopic) {
    nodes.push(storiesGrid(items.filter((i) => i.topic === activeTopic)));
  } else if (pinned.length) {
    const used = new Set();
    pinned.forEach((topic, n) => {
      const group = items.filter((i) => i.topic === topic);
      group.forEach((i) => used.add(i.id));
      const title = h("h3", { class: "group-title" }, cap(topic), h("span", { class: "count" }, `${group.length} haber`));
      const remove = h("button", { type: "button", class: "text-btn" }, "Başlığı kaldır");
      remove.addEventListener("click", () => {
        prefs.pinned[current] = (prefs.pinned[current] ?? []).filter((t) => t !== topic);
        savePrefs();
        renderSection();
      });
      title.append(remove);
      nodes.push(title, storiesGrid(group, { withLead: n === 0 }));
    });
    const rest = items.filter((i) => !used.has(i.id));
    if (rest.length) nodes.push(h("h3", { class: "group-title" }, "Diğer haberler"), storiesGrid(rest, { withLead: false }));
  } else {
    nodes.push(storiesGrid(items));
  }
  setContent(box, ...nodes);
}

async function goSection(id, { openId = null, push = false } = {}) {
  current = id;
  activeTopic = null;
  document.body.dataset.section = id;
  const paper = getComputedStyle(document.body).getPropertyValue("--paper").trim();
  document.querySelector('meta[name="theme-color"]').setAttribute("content", paper);

  setGlyph($("section-glyph"), id);
  $("section-title").textContent = SECTION[id].label;
  $("section-blurb").textContent = SECTION[id].blurb;
  renderRail();
  $("suggestion").hidden = true;
  $("topics").hidden = true;
  setContent($("summary"), skeleton());
  if (!openId) history[push ? "pushState" : "replaceState"](null, "", `#${id}`);
  record(id, null, 0.3);

  try {
    const row = await loadSection(id);
    if (id !== current) return;   // the reader already moved on
    currentRow = row;
    renderSection();
    if (openId) {
      const item = row?.items.find((i) => i.id === openId);
      if (item) openStory(item, id, { push: false });
    }
  } catch {
    if (id === current) setContent($("summary"), h("p", { class: "error", role: "alert" }, "Haberler yüklenemedi. Biraz sonra tekrar dene."));
  }
}

// --- read state: stories you opened fade, kept only in this browser ---------------------------

const READ_KEY = "read-links";

function readSet() {
  try {
    return new Set(JSON.parse(localStorage.getItem(READ_KEY) ?? "[]"));
  } catch {
    return new Set();
  }
}

function markRead(link) {
  try {
    localStorage.setItem(READ_KEY, JSON.stringify([...readSet(), link].slice(-500)));
  } catch {
    // Fading is a convenience only.
  }
}

// --- asking the server ---------------------------------------------------------------------

/** Sends a question and returns { status, data }. From a story's pop-up the story's link goes along and
 *  the server streams what it is doing (one JSON object per line); `onStatus` receives each step. */
async function askServer(question, about = null, onStatus = () => {}) {
  const headers = { "Content-Type": "application/json" };
  const token = await accessToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  const controller = new AbortController();
  const giveUp = setTimeout(() => controller.abort(), about ? 70000 : 30000);
  try {
    const response = await fetch(ASK_URL, { method: "POST", headers, signal: controller.signal,
      body: JSON.stringify(about ? { question, about, stream: true } : { question }) });
    if (!(response.headers.get("content-type") ?? "").includes("ndjson")) {
      return { status: response.status, data: await response.json().catch(() => ({})) };
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let result = {};
    for (;;) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value ?? new Uint8Array(), { stream: !done });
      const lines = buffer.split("\n");
      buffer = done ? "" : lines.pop();
      for (const line of done ? lines : lines.filter((l) => l)) {
        if (!line.trim()) continue;
        let message;
        try { message = JSON.parse(line); } catch { continue; }
        if (message.type === "status") onStatus(message.text);
        else if (message.type === "result") result = message;
      }
      if (done) break;
    }
    return { status: result.error ? 502 : 200, data: result };
  } finally {
    clearTimeout(giveUp);
  }
}

function quotaText(quota, scope) {
  if (!quota) return "";
  return scope === "guest" ? `Misafir hakkı: ${quota.remaining}/${quota.limit}` : `Bugünkü kalan hakkın: ${quota.remaining}/${quota.limit}`;
}

function quotaMessage(data) {
  if (data.reason === "pool_limit") return "Bugünkü toplam kullanım sınırına ulaşıldı. Yarın tekrar dene.";
  return data.scope === "guest"
    ? "Misafir deneme hakkını kullandın. Devam etmek için giriş yapman gerekiyor."
    : "Bugünkü soru hakkın doldu. Yarın tekrar dene; bölümler açık kalıyor.";
}

function sourceItem(item, n, showTitle = false) {
  const when = item.published_at ? formatDateTime(item.published_at) : item.date;
  const li = h("li", { id: `fn-${n}` });
  li.append(h("span", { class: "src" }, item.source));
  if (item.language === "en") li.append(" ", h("span", { class: "chip" }, "EN"));
  li.append(`, ${when}: `, h("a", { href: safeUrl(item.link), target: "_blank", rel: "noopener noreferrer" }, showTitle ? item.title : "habere git"));
  return li;
}

/** A paragraph in which each sentence carries a footnote number, with the sources underneath. */
const ORIGINS = { story: "Haberin metninden", archive: "Son 3 günün haberlerinden", web: "İnternet aramasından" };

function answerNode(data) {
  if (data.mode === "not_found") {
    const web = data.web === "searched"
      ? "İnternette bulduğum sayfalarda da yanıt yoktu."
      : "İnternet aramasından da sonuç alamadım (arama hizmeti yanıt vermemiş olabilir).";
    return h("p", { class: "note" }, `Bu ayrıntıyı haberin metninde ve son 3 günün haberlerinde bulamadım. ${web} Haberin tamamı için kaynağa gidebilirsin.`);
  }
  if (data.mode !== "answer" && data.mode !== "fallback" && data.mode !== "text") {
    return h("p", { class: "note" }, data.not_covered || "Son 3 günün haberlerinde bu konuyla ilgili bir şey bulamadım.");
  }
  const wrapper = h("div", { class: "answer" });
  const sources = h("ol", { class: "sources" });
  if (data.mode === "text") {
    wrapper.append(h("span", { class: "origin" }, ORIGINS[data.origin] ?? ORIGINS.story));
    wrapper.append(h("p", { class: "answer-text" }, data.answer));
    (data.sources ?? []).forEach((src, i) => {
      const li = h("li", { id: `fn-${i + 1}` }, h("span", { class: "src" }, src.source), ": ",
        h("a", { href: safeUrl(src.link), target: "_blank", rel: "noopener noreferrer" }, src.title));
      sources.append(li);
    });
  } else if (data.mode === "answer") {
    if (data.origin === "archive") wrapper.append(h("span", { class: "origin" }, ORIGINS.archive));
    const text = h("p", { class: "answer-text" });
    data.items.forEach((item, i) => {
      text.append(item.sentence, h("sup", {}, h("a", { href: `#fn-${i + 1}`, "aria-label": `Kaynak ${i + 1}` }, String(i + 1))), " ");
      sources.append(sourceItem(item, i + 1));
    });
    wrapper.append(text);
  } else {
    wrapper.append(h("p", { class: "note" }, "Özet hazırlanamadı. En yakın haberler:"));
    data.items.forEach((item, i) => sources.append(sourceItem(item, i + 1, true)));
  }
  if (data.not_covered) wrapper.append(h("p", { class: "not-covered" }, data.not_covered));
  if (sources.children.length) wrapper.append(sources);
  return wrapper;
}

/** Shared by the chat box and the story pop-up. While the server works, each step it reports is listed. */
async function runAsk({ question, about, button, out, quotaEl }) {
  button.disabled = true;
  const steps = h("ol", { class: "steps", "aria-live": "polite" });
  const addStep = (text) => steps.append(h("li", {}, text));
  addStep(about ? "Sorun alındı…" : "Haberlerde aranıyor…");
  setContent(out, steps);
  try {
    const { status, data } = await askServer(question, about, addStep);
    if (data.quota) quotaEl.textContent = quotaText(data.quota, data.scope);
    if (status === 200) {
      setContent(out, answerNode(data));
    } else if (status === 429) {
      setContent(out, h("p", { class: "error", role: "alert" }, quotaMessage(data)));
    } else if (status === 401) {
      setContent(out, h("p", { class: "error", role: "alert" }, "Oturum süresi dolmuş. Lütfen yeniden giriş yap."));
      await supabase.auth.signOut();
    } else {
      setContent(out, h("p", { class: "error", role: "alert" }, "Şu an cevap veremiyorum. Biraz sonra tekrar dene."));
    }
  } catch (error) {
    setContent(out, h("p", { class: "error", role: "alert" }, error?.name === "AbortError"
      ? "Cevap çok gecikti. Biraz sonra tekrar dene."
      : "Bağlantı kurulamadı. İnternetini kontrol edip tekrar dene."));
  } finally {
    button.disabled = false;
  }
}

$("chat-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const question = $("chat-input").value.trim();
  if (question) runAsk({ question, about: null, button: $("chat-submit"), out: $("chat-answer"), quotaEl: $("chat-quota") });
});

// --- the story pop-up ----------------------------------------------------------------------

const storyDialog = $("story-dialog");
let openItem = null;

function detailParagraphs(text) {
  const sentences = text.split(/(?<=[.!?])\s+/).filter(Boolean);
  const paragraphs = [];
  for (let i = 0; i < sentences.length; i += 2) paragraphs.push(h("p", {}, sentences.slice(i, i + 2).join(" ")));
  return paragraphs;
}

function openStory(item, section, { push = true } = {}) {
  openItem = item;
  setGlyph($("sd-glyph"), section);
  $("sd-section").textContent = SECTION[section].label;
  $("sd-headline").textContent = item.headline;
  const dek = item.sentence && item.sentence !== item.headline ? item.sentence : "";
  $("sd-dek").textContent = dek;
  $("sd-dek").hidden = !dek;

  const meta = $("sd-meta");
  meta.replaceChildren(h("span", { class: "src" }, item.source),
    h("time", { datetime: item.published_at, title: formatDateTime(item.published_at) }, relativeTime(item.published_at)));
  if (item.language === "en") meta.append(h("span", { class: "chip", title: "İngilizce kaynak, Türkçeye özetlendi" }, "EN"));
  if (!isCatchAll(item.topic)) meta.append(h("span", { class: "chip topic-chip" }, cap(item.topic)));

  // The detail is only shown when it says more than the one-line summary; when the sources hold no more
  // than that, say so instead of repeating the same sentence.
  const richer = item.detail && item.detail.length > item.sentence.length * 1.4;
  $("sd-dek").hidden = !dek;
  setContent($("sd-detail"), ...(richer
    ? detailParagraphs(item.detail)
    : [h("p", { class: "note" }, item.detail
        ? "Kaynakların bu haberle ilgili verdiği ayrıntı bu kadar. Tamamı için habere git."
        : "Bu haber için ayrıntılı özet henüz hazır değil. Bir sonraki güncellemede eklenecek.")]));

  const also = item.also.filter((a) => a.link);
  $("sd-also").hidden = !also.length;
  setContent($("sd-also-list"), ...also.map((a) => {
    const link = h("a", { href: safeUrl(a.link), target: "_blank", rel: "noopener noreferrer" }, a.source);
    if (a.language === "en") link.append(" ", h("span", { class: "chip" }, "EN"));
    link.append(glyph("external"));
    return h("li", {}, link);
  }));

  const source = $("sd-source");
  source.href = safeUrl(item.link);
  source.replaceChildren(`Haberin tamamını ${item.source} sitesinde oku`, glyph("external"));
  source.onclick = () => {
    markRead(item.link);
    record(section, item.topic, 1.5);
  };

  $("sd-ask-input").value = "";
  $("sd-ask-quota").textContent = "";
  $("sd-ask-answer").replaceChildren();
  storyDialog.querySelector(".sheet").scrollTop = 0;

  record(section, item.topic, 1);
  markRead(item.link);
  document.querySelectorAll(".story").forEach((node) => {
    if (node.querySelector(".headline")?.textContent === item.headline) node.classList.add("is-read");
  });
  if (!storyDialog.open) storyDialog.showModal();
  if (push) history.pushState({ story: item.id }, "", `#${section}/${item.id}`);
  renderRail();   // opening a story may be what tips the order
}

$("sd-ask").addEventListener("submit", (event) => {
  event.preventDefault();
  const question = $("sd-ask-input").value.trim();
  if (question && openItem) {
    runAsk({ question, about: openItem.link, button: $("sd-ask-submit"), out: $("sd-ask-answer"), quotaEl: $("sd-ask-quota") });
  }
});

$("sd-close").addEventListener("click", () => storyDialog.close());
storyDialog.addEventListener("click", (event) => {
  if (event.target === storyDialog) storyDialog.close();   // a click on the dimmed area
});
storyDialog.addEventListener("close", () => {
  openItem = null;
  if (history.state?.story) history.back();   // drop the entry that opening pushed
  renderSection();                              // fades the story just read
});

// --- the settings pop-up ----------------------------------------------------------------------

const prefsDialog = $("prefs-dialog");

function openPrefs() {
  $("pd-reorder").checked = prefs.reorder;
  const scores = sectionScores();
  const max = Math.max(...Object.values(scores), 0);
  const rows = SECTIONS.filter((s) => scores[s.id] > 0.05).sort((a, b) => scores[b.id] - scores[a.id]).map((s) => {
    const opened = prefs.events.filter((e) => e.s === s.id && e.w >= 1).length;
    return h("li", { style: `--tab: var(--${s.id}-accent); --w: ${Math.round((scores[s.id] / max) * 100)}` },
      h("div", { class: "row" }, h("span", {}, s.label), h("span", {}, `${opened} haber açtın`)),
      h("span", { class: "bar" }));
  });
  setContent($("pd-interest"), ...(rows.length ? rows : [h("li", { class: "note" }, "Henüz yeterli bilgi yok. Birkaç habere dokundukça burada görünür.")]));

  const pinnedRows = Object.entries(prefs.pinned).flatMap(([section, topics]) => topics.map((topic) => {
    const remove = h("button", { type: "button", class: "text-btn" }, "Kaldır");
    remove.addEventListener("click", () => {
      prefs.pinned[section] = prefs.pinned[section].filter((t) => t !== topic);
      savePrefs();
      openPrefs();
      if (section === current) renderSection();
    });
    return h("li", {}, h("span", {}, `${SECTION[section].label}: ${cap(topic)}`), remove);
  }));
  setContent($("pd-pinned"), ...(pinnedRows.length ? pinnedRows : [h("li", { class: "note" }, "Üste alınan konu yok. Bir konuyu çok açarsan sana sorarız.")]));
  if (!prefsDialog.open) prefsDialog.showModal();
}

$("prefs-open").addEventListener("click", openPrefs);
$("pd-close").addEventListener("click", () => prefsDialog.close());
prefsDialog.addEventListener("click", (event) => {
  if (event.target === prefsDialog) prefsDialog.close();
});
prefsDialog.addEventListener("close", () => {
  renderRail();
  renderSection();
});
$("pd-reorder").addEventListener("change", (event) => {
  prefs.reorder = event.target.checked;
  savePrefs();
  renderRail();
});

let clearArmed = null;
$("pd-clear").addEventListener("click", () => {
  const button = $("pd-clear");
  if (!clearArmed) {
    button.textContent = "Silmek için tekrar dokun";
    clearArmed = setTimeout(() => { clearArmed = null; button.textContent = "Okuma geçmişimi sil"; }, 4000);
    return;
  }
  clearTimeout(clearArmed);
  clearArmed = null;
  prefs = freshPrefs();
  savePrefs();
  try { localStorage.removeItem(READ_KEY); } catch { /* nothing to remove */ }
  button.textContent = "Okuma geçmişimi sil";
  openPrefs();
});

// --- login panel, sign in / out, invitation requests ----------------------------------------------

$("login-toggle").addEventListener("click", () => {
  const panel = $("login-panel");
  panel.hidden = !panel.hidden;
  $("login-toggle").setAttribute("aria-expanded", String(!panel.hidden));
  if (!panel.hidden) $("login-email").focus();
});

$("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = $("login-submit");
  const error = $("login-error");
  button.disabled = true;
  error.hidden = true;
  const { error: failure } = await supabase.auth.signInWithPassword({
    email: $("login-email").value.trim(),
    password: $("login-password").value,
  });
  button.disabled = false;
  if (failure) {
    error.textContent = "E-posta veya şifre hatalı.";
    error.hidden = false;
  }
  // On success onAuthStateChange fires and render() closes the panel.
});

$("logout").addEventListener("click", () => supabase.auth.signOut());

// The form sends an email address and a short note. No password is ever asked for.
$("invite-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = $("invite-submit");
  const message = $("invite-message");
  button.disabled = true;
  message.hidden = true;
  let text = "Şu an gönderemedim. Biraz sonra tekrar dene.";
  try {
    const response = await fetch(INVITE_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        email: $("invite-email").value.trim(),
        note: $("invite-note").value.trim(),
        website: $("invite-website").value, // the bot trap
      }),
    });
    if (response.ok) {
      text = "İsteğin alındı. Uygun görülürse e-postana davet gelecek.";
      $("invite-form").reset();
    } else if (response.status === 400) {
      text = "Geçerli bir e-posta adresi yaz.";
    } else if (response.status === 429) {
      text = "Bugün çok fazla istek gönderdin. Yarın tekrar dene.";
    }
  } catch {
    text = "Bağlantı kurulamadı. İnternetini kontrol edip tekrar dene.";
  }
  message.textContent = text;
  message.hidden = false;
  button.disabled = false;
});

let signedIn = false;

/** The sections are the same for everyone; only the account area and the chat quota change. */
function renderAccount(session) {
  signedIn = Boolean(session);
  $("login-toggle").hidden = signedIn;
  $("logout").hidden = !signedIn;
  $("account-email").hidden = !signedIn;
  $("account-email").textContent = session?.user?.email ?? "";
  if (signedIn) {
    $("login-panel").hidden = true;
    $("login-toggle").setAttribute("aria-expanded", "false");
  }
  $("login-password").value = "";
  $("chat-quota").textContent = "";
  setContent($("chat-answer"));
  $("chat-scope").textContent = signedIn
    ? "Her gün 10 soru sorabilirsin."
    : "Misafir olarak günde 1 soru sorabilirsin. Daha fazlası için giriş yap.";
}

// Token refreshes also fire auth events; only redraw when the signed-in user changes,
// otherwise an hourly refresh would wipe the answer on screen.
let lastUserId;
function onSession(session) {
  const userId = session?.user?.id ?? null;
  if (userId === lastUserId) return;
  lastUserId = userId;
  renderAccount(session);
}

// --- start-up and navigation --------------------------------------------------------------

function parseHash() {
  const [section, story] = location.hash.slice(1).split("/");
  return { section: section in SECTION ? section : null, story: story || null };
}

window.addEventListener("popstate", () => {
  if (storyDialog.open) storyDialog.close();
  const { section, story } = parseHash();
  if (section && section !== current) goSection(section, { openId: story });
  else if (section && story && currentRow) {
    const item = currentRow.items.find((i) => i.id === story);
    if (item) openStory(item, section, { push: false });
  }
});

// When the tab comes back to the front after a while, look for newer summaries.
document.addEventListener("visibilitychange", async () => {
  if (document.visibilityState !== "visible") return;
  refreshPulse();
  const hit = cache.get(current);
  if (hit && Date.now() - hit.at > CACHE_MS) {
    const row = await loadSection(current, { force: true }).catch(() => null);
    if (row && row.generated_at !== currentRow?.generated_at && !storyDialog.open) {
      currentRow = row;
      renderSection();
    }
  }
});

$("sd-close").replaceChildren(glyph("close"));
$("pd-close").replaceChildren(glyph("close"));
$("prefs-open").replaceChildren(glyph("sliders"), "Sana göre");

$("sd-glyph").classList.add("ico");
$("section-glyph").classList.add("ico");

supabase.auth.onAuthStateChange((_event, session) => onSession(session));
supabase.auth.getSession().then(({ data }) => onSession(data.session));
refreshPulse();
setInterval(refreshPulse, 5 * 60 * 1000);

const start = parseHash();
goSection(start.section ?? orderedSections()[0].id, { openId: start.story });
