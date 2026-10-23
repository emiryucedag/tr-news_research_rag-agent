import { createClient } from "https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2/+esm";
import { SUPABASE_URL, SUPABASE_PUBLISHABLE_KEY } from "./config.js";

// supabase-js keeps the session in the browser and refreshes it by itself, so a user
// signs in once and stays signed in until they log out or clear site data.
const supabase = createClient(SUPABASE_URL, SUPABASE_PUBLISHABLE_KEY);
const ASK_URL = `${SUPABASE_URL}/functions/v1/ask`;
const INVITE_URL = `${SUPABASE_URL}/functions/v1/request-invite`;

const CATEGORIES = [
  ["gundem", "Gündem"],
  ["siyaset", "Siyaset"],
  ["spor", "Spor"],
  ["ekonomi", "Ekonomi"],
  ["teknoloji", "Teknoloji"],
  ["dunya", "Dünya"],
  ["yapay-zeka", "Yapay Zeka"],
  ["populer-kultur", "Popüler Kültür"],
];
const LABELS = Object.fromEntries(CATEGORIES);

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
const timeFormat = new Intl.DateTimeFormat("tr-TR", { ...istanbul, hour: "2-digit", minute: "2-digit" });
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

/** "Cuma, 23 Ekim 2026" for the masthead. */
function todayLine() {
  const parts = new Intl.DateTimeFormat("tr-TR", { ...istanbul, weekday: "long", day: "numeric", month: "long", year: "numeric" })
    .formatToParts(new Date());
  const get = (type) => parts.find((p) => p.type === type)?.value ?? "";
  return `${get("weekday")}, ${get("day")} ${get("month")} ${get("year")}`;
}

function setContent(container, ...nodes) {
  container.replaceChildren(...nodes);
}

async function accessToken() {
  const { data } = await supabase.auth.getSession();
  return data.session?.access_token ?? null;
}

// --- read state: stories you opened fade, kept only in this browser ---------------

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
    const links = [...readSet(), link].slice(-500); // keep the list short
    localStorage.setItem(READ_KEY, JSON.stringify(links));
  } catch {
    // Storage can be blocked (private windows); fading is a convenience only.
  }
}

// --- stories ---------------------------------------------------------------

function askAbout(item) {
  const input = $("chat-input");
  input.value = `Bu haber hakkında ayrıntı var mı: ${item.sentence ?? item.title}`.slice(0, 300);
  $("chat").scrollIntoView({ behavior: reducedMotion ? "auto" : "smooth" });
  input.focus();
}

function storyNode(item, extraClass = "") {
  // Inside a section the section name would repeat on every story, so the kicker is just the outlet.
  const kicker = h("p", { class: "kicker" }, item.source);
  if (item.language === "en") kicker.append(h("span", { class: "lang", title: "İngilizce kaynak, Türkçeye özetlendi" }, "EN"));

  const time = h("time", { datetime: item.published_at, title: formatDateTime(item.published_at) }, relativeTime(item.published_at));
  const link = h("a", { href: safeUrl(item.link), target: "_blank", rel: "noopener noreferrer" }, "Habere git →");
  const story = h("article", { class: `story ${extraClass}`.trim() });

  link.addEventListener("click", () => {
    markRead(item.link);
    story.classList.add("is-read");
  });

  const ask = h("button", { type: "button", class: "link-button" }, "Bu haber hakkında sor");
  ask.addEventListener("click", () => askAbout(item));

  if (readSet().has(item.link)) story.classList.add("is-read");
  story.append(
    kicker,
    h("h3", { class: "headline" }, item.sentence ?? item.title),
    h("p", { class: "story-meta" }, time, " · ", link, " · ", ask),
  );
  return story;
}

/** A front page: one lead story, up to four secondary ones in two columns, then a plain list. */
function renderFrontPage(items) {
  const page = h("div", { class: "front-page fade-in" });
  page.append(storyNode(items[0], "lead"));
  const secondary = items.slice(1, 5);
  if (secondary.length) {
    const grid = h("div", { class: "story-grid" });
    for (const item of secondary) grid.append(storyNode(item));
    page.append(grid);
  }
  const rest = items.slice(5);
  if (rest.length) {
    const list = h("div", { class: "story-rest" });
    for (const item of rest) list.append(storyNode(item));
    page.append(list);
  }
  return page;
}

function skeleton() {
  const rows = h("div", { "aria-hidden": "true" });
  for (let i = 0; i < 4; i++) rows.append(h("div", { class: "skeleton" }, h("span"), h("span"), h("span")));
  return rows;
}

// --- login panel, sign in / out, invitation requests ----------------------------

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

// --- sections (tabs) ----------------------------------------------------------

const summaryCache = new Map();
let activeCategory = null;

function renderTabs() {
  const nav = $("tabs");
  nav.replaceChildren();
  for (const [slug, label] of CATEGORIES) {
    const button = h("button", { type: "button", "aria-current": String(slug === activeCategory) }, label);
    button.addEventListener("click", () => selectCategory(slug));
    nav.append(button);
  }
}

async function selectCategory(slug) {
  activeCategory = slug;
  renderTabs();
  $("section-title").textContent = LABELS[slug];
  $("section-updated").textContent = "";
  const box = $("summary");
  setContent(box, skeleton());

  let row = summaryCache.get(slug);
  if (!row) {
    const { data, error } = await supabase
      .from("category_summaries")
      .select("mode, items, generated_at")
      .eq("category", slug)
      .maybeSingle();
    if (slug !== activeCategory) return; // the user already switched sections
    if (error) {
      setContent(box, h("p", { class: "error" }, "Özet yüklenemedi. Biraz sonra tekrar dene."));
      return;
    }
    row = data;
    if (row) summaryCache.set(slug, row);
  }

  if (!row || row.mode === "empty" || !row.items.length) {
    setContent(box, h("p", { class: "note" }, "Bu bölümde şu an gösterilecek haber yok. Saat başı yenileniyor."));
    return;
  }
  $("section-updated").textContent = `Son güncelleme ${timeFormat.format(new Date(row.generated_at))}`;
  const nodes = [];
  if (row.mode === "fallback") {
    nodes.push(h("p", { class: "note" }, "Özet hazırlanamadı, son başlıklar gösteriliyor."));
  }
  nodes.push(renderFrontPage(row.items));
  setContent(box, ...nodes);
}

// --- chat ---------------------------------------------------------------------

let signedIn = false;

/** The server says whose quota it is (guest or user); do not guess from the browser's state. */
function showQuota(quota, scope) {
  if (!quota) return;
  $("chat-quota").textContent = scope === "guest"
    ? `Misafir hakkı: ${quota.remaining}/${quota.limit}`
    : `Bugünkü kalan hakkın: ${quota.remaining}/${quota.limit}`;
}

function chatMessage(text, isError = false) {
  setContent($("chat-answer"), h("p", { class: isError ? "error" : "note" }, text));
}

/** A paragraph in which each sentence carries a footnote number, and the sources underneath. */
function renderAnswer(data) {
  const answer = $("chat-answer");
  if (data.mode !== "answer" && data.mode !== "fallback") {
    chatMessage(data.not_covered || "Son 3 günün haberlerinde bu konuyla ilgili bir şey bulamadım.");
    return;
  }
  const sources = h("ol", { class: "sources" });
  const wrapper = h("div", { class: "answer fade-in" });

  if (data.mode === "answer") {
    const text = h("p", { class: "answer-text" });
    data.items.forEach((item, i) => {
      const n = i + 1;
      text.append(item.sentence, h("sup", {}, h("a", { href: `#fn-${n}`, "aria-label": `Kaynak ${n}` }, String(n))), " ");
      sources.append(sourceItem(item, n));
    });
    wrapper.append(text);
  } else {
    wrapper.append(h("p", { class: "note" }, "Özet hazırlanamadı. En yakın haberler:"));
    data.items.forEach((item, i) => sources.append(sourceItem(item, i + 1, true)));
  }
  if (data.not_covered) {
    // The part of the question the news does not answer, said plainly instead of being skipped.
    wrapper.append(h("p", { class: "note not-covered" }, data.not_covered));
  }
  wrapper.append(sources);
  setContent(answer, wrapper);
}

function sourceItem(item, n, showTitle = false) {
  const when = item.published_at ? formatDateTime(item.published_at) : item.date;
  const li = h("li", { id: `fn-${n}` });
  li.append(
    h("span", { class: "src-name" }, item.source),
    ...(item.language === "en" ? [" ", h("span", { class: "lang" }, "EN")] : []),
    ` · ${when} · `,
    h("a", { href: safeUrl(item.link), target: "_blank", rel: "noopener noreferrer" }, showTitle ? item.title : "Habere git →"),
  );
  return li;
}

function quotaMessage(data) {
  if (data.reason === "pool_limit") {
    return "Bugünkü toplam kullanım sınırına ulaşıldı. Yarın tekrar dene.";
  }
  return data.scope === "guest"
    ? "Misafir deneme hakkını kullandın. Devam etmek için giriş yapman gerekiyor."
    : "Bugünkü soru hakkın doldu. Yarın tekrar dene; bölüm özetleri açık kalıyor.";
}

$("chat-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const question = $("chat-input").value.trim();
  if (!question) return;
  const button = $("chat-submit");
  button.disabled = true;
  chatMessage("Haberlerde aranıyor…");
  const slowNotice = setTimeout(() => chatMessage("Biraz uzun sürüyor, hâlâ arıyorum…"), 4000);
  const controller = new AbortController();
  const giveUp = setTimeout(() => controller.abort(), 30000);

  try {
    const headers = { "Content-Type": "application/json" };
    const token = await accessToken();
    if (token) headers.Authorization = `Bearer ${token}`;

    const response = await fetch(ASK_URL, { method: "POST", headers, body: JSON.stringify({ question }), signal: controller.signal });
    const data = await response.json().catch(() => ({}));
    showQuota(data.quota, data.scope);

    if (response.ok) {
      renderAnswer(data);
    } else if (response.status === 429) {
      chatMessage(quotaMessage(data), true);
    } else if (response.status === 401) {
      chatMessage("Oturum süresi dolmuş. Lütfen yeniden giriş yap.", true);
      await supabase.auth.signOut();
    } else {
      chatMessage("Şu an cevap veremiyorum. Biraz sonra tekrar dene.", true);
    }
  } catch (error) {
    chatMessage(error?.name === "AbortError"
      ? "Cevap çok gecikti. Biraz sonra tekrar dene."
      : "Bağlantı kurulamadı. İnternetini kontrol edip tekrar dene.", true);
  } finally {
    clearTimeout(slowNotice);
    clearTimeout(giveUp);
    button.disabled = false;
  }
});

// --- view switching -----------------------------------------------------------

/** The sections are the same for everyone; only the account area and the chat quota change. */
function render(session) {
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

$("dateline").textContent = todayLine();
selectCategory(CATEGORIES[0][0]);

// Token refreshes also fire auth events; only redraw when the signed-in user changes,
// otherwise an hourly refresh would wipe the chat answer on screen.
let lastUserId;
function onSession(session) {
  const userId = session?.user?.id ?? null;
  if (userId === lastUserId) return;
  lastUserId = userId;
  render(session);
}

supabase.auth.onAuthStateChange((_event, session) => onSession(session));
supabase.auth.getSession().then(({ data }) => onSession(data.session));
