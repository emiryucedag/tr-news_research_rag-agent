// Invitation requests: a visitor leaves an email address and a short note. The owner reads the
// requests in the Supabase dashboard (table invite_requests) and invites people by hand;
// the invited person chooses their own password. No password is ever collected here.
import { corsHeaders, insertRow, json, rpc, visitorHash } from "../_shared/http.ts";

const MAX_EMAIL_CHARS = 254;
const MAX_NOTE_CHARS = 300;
const PER_VISITOR_DAILY = 3; // requests per visitor per day
const ALL_VISITORS_DAILY = 50; // requests per day in total, a brake against floods

const EMAIL_PATTERN = /^[^\s@]{1,64}@[^\s@]{1,190}\.[^\s@]{2,}$/;

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response(null, { status: 204, headers: corsHeaders(req) });
  if (req.method !== "POST") return json(req, { error: "POST only" }, 405);

  const body = await req.json().catch(() => ({}));

  // Bot trap: the form has a hidden field that people leave empty. Pretend it worked.
  if (typeof body.website === "string" && body.website.trim() !== "") return json(req, { ok: true });

  const email = String(body.email ?? "").trim().toLowerCase();
  if (email.length > MAX_EMAIL_CHARS || !EMAIL_PATTERN.test(email)) {
    return json(req, { error: "invalid_email" }, 400);
  }
  const note = String(body.note ?? "").trim().slice(0, MAX_NOTE_CHARS);

  const visitor = await visitorHash(req);
  try {
    const quota = await rpc("consume_quota", {
      p_subject: `invite:${visitor}`, p_limit: PER_VISITOR_DAILY,
      p_pool: "pool:invites", p_pool_limit: ALL_VISITORS_DAILY,
    });
    if (quota.status !== "ok") return json(req, { error: "too_many_requests" }, 429);

    // "created" and "exists" look the same to the caller, so the form cannot be used to
    // find out which addresses have already asked.
    await insertRow("invite_requests", { email, note: note || null, ip_hash: visitor });
    return json(req, { ok: true });
  } catch (e) {
    return json(req, { error: `could not save the request: ${String(e).slice(0, 100)}` }, 503);
  }
});
