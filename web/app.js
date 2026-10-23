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

const $ = (id) => document.getElementById(id);

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

const dateFormat = new Intl.DateTimeFormat("tr-TR", {
  day: "numeric", month: "long", hour: "2-digit", minute: "2-digit", timeZone: "Europe/Istanbul",
});
const formatDate = (iso) => dateFormat.format(new Date(iso));

/** The chat function sends a ready-made date string; stored summaries send an ISO timestamp. */
const itemDate = (item) => item.date ?? (item.published_at ? formatDate(item.published_at) : "");

function renderItems(items) {
  const list = h("ul", { class: "items" });
  for (const item of items) {
    const meta = h("p", { class: "meta" }, [item.source, itemDate(item)].filter(Boolean).join(" · ") + " · ");
    meta.append(h("a", { href: safeUrl(item.link), target: "_blank", rel: "noopener noreferrer" }, "Habere git"));
    const li = h("li");
    li.append(h("p", { class: "sentence" }, item.sentence ?? item.title), meta);
    list.append(li);
  }
  return list;
}

function setContent(container, ...nodes) {
  container.replaceChildren(...nodes);
}

async function accessToken() {
  const { data } = await supabase.auth.getSession();
  return data.session?.access_token ?? null;
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

// --- category tabs ------------------------------------------------------------

const summaryCache = new Map();
let activeCategory = null;

function renderTabs() {
  const nav = $("tabs");
  nav.replaceChildren();
  for (const [slug, label] of CATEGORIES) {
    const button = h("button", { type: "button", "aria-selected": String(slug === activeCategory) }, label);
    button.addEventListener("click", () => selectCategory(slug));
    nav.append(button);
  }
}

async function selectCategory(slug) {
  activeCategory = slug;
  renderTabs();
  const box = $("summary");
  setContent(box, h("p", { class: "note" }, "Yükleniyor…"));

  let row = summaryCache.get(slug);
  if (!row) {
    const { data, error } = await supabase
      .from("category_summaries")
      .select("mode, items, generated_at")
      .eq("category", slug)
      .maybeSingle();
    if (slug !== activeCategory) return; // the user already switched tabs
    if (error) {
      setContent(box, h("p", { class: "error" }, "Özet yüklenemedi. Biraz sonra tekrar dene."));
      return;
    }
    row = data;
    if (row) summaryCache.set(slug, row);
  }

  if (!row || row.mode === "empty" || !row.items.length) {
    setContent(box, h("p", { class: "note" }, "Bu kategoride şu an gösterilecek haber yok."));
    return;
  }
  const nodes = [];
  if (row.mode === "fallback") {
    nodes.push(h("p", { class: "note" }, "Özet hazırlanamadı, son başlıklar gösteriliyor."));
  }
  nodes.push(renderItems(row.items));
  nodes.push(h("p", { class: "note" }, `Son güncelleme: ${formatDate(row.generated_at)}`));
  setContent(box, ...nodes);
}

// --- chat ---------------------------------------------------------------------

let signedIn = false;

function showQuota(quota) {
  if (!quota) return;
  $("chat-quota").textContent = signedIn
    ? `Bugünkü kalan hakkın: ${quota.remaining}/${quota.limit}`
    : `Misafir hakkı: ${quota.remaining}/${quota.limit}`;
}

function chatMessage(text, isError = false) {
  setContent($("chat-answer"), h("p", { class: isError ? "error" : "note" }, text));
}

function renderAnswer(data) {
  const answer = $("chat-answer");
  if (data.mode === "answer") {
    setContent(answer, renderItems(data.items));
  } else if (data.mode === "fallback") {
    setContent(answer, h("p", { class: "note" }, "Özet hazırlanamadı. En yakın haberler:"), renderItems(data.items));
  } else {
    chatMessage("Son 3 günün haberlerinde bu konuyla ilgili bir şey bulamadım.");
  }
}

function quotaMessage(data) {
  if (data.reason === "pool_limit") {
    return "Bugünkü toplam kullanım sınırına ulaşıldı. Yarın tekrar dene.";
  }
  return data.scope === "guest"
    ? "Misafir deneme hakkını kullandın. Devam etmek için giriş yapman gerekiyor."
    : "Bugünkü soru hakkın doldu. Yarın tekrar dene; kategori özetleri açık kalıyor.";
}

$("chat-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const question = $("chat-input").value.trim();
  if (!question) return;
  const button = $("chat-submit");
  button.disabled = true;
  chatMessage("Haberlerde aranıyor…");

  try {
    const headers = { "Content-Type": "application/json" };
    const token = await accessToken();
    if (token) headers.Authorization = `Bearer ${token}`;

    const response = await fetch(ASK_URL, { method: "POST", headers, body: JSON.stringify({ question }) });
    const data = await response.json().catch(() => ({}));
    showQuota(data.quota);

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
  } catch {
    chatMessage("Bağlantı kurulamadı. İnternetini kontrol edip tekrar dene.", true);
  } finally {
    button.disabled = false;
  }
});

// --- view switching -----------------------------------------------------------

/** The tabs are the same for everyone; only the account area and the chat quota change. */
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

selectCategory(CATEGORIES[0][0]);

supabase.auth.onAuthStateChange((_event, session) => onSession(session));
supabase.auth.getSession().then(({ data }) => onSession(data.session));
