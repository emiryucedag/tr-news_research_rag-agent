"""Hourly section summaries: for each section, pick up to 12 stories from the last 48 hours and
write, for every story, a short headline, a one-sentence summary, a longer summary for the pop-up,
a sub-topic tag and the other outlets that report the same event. Ready for the site to read.

The model only chooses stories and writes text. Links, outlets and dates come from the database,
never from the model. Every piece of text must pass the checks in guards.py before it is kept.
A section is skipped when its candidate set has not changed since the last run, so quiet hours
cost no model requests. One model request per changed section, as before.

Run from backend/:
    ../.venv/bin/python jobs/summarize.py              # all sections
    ../.venv/bin/python jobs/summarize.py --only spor --force
"""
import argparse
import hashlib
import re
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from concurrent.futures import ThreadPoolExecutor

from articletext import fetch_text
from common import GENERATION_MODELS, GenerationError, Supabase, generate_json
from feeds import CATEGORIES, CATEGORY_EXTRA_SOURCES, CATEGORY_SUBTOPICS, CATEGORY_TOPICS, FEEDS
from ingest import GATE, USER_AGENT
from guards import grounded, match_title, numbers_supported, strip_unsupported_reactions, title_index

WINDOW_HOURS = 48
MAX_CANDIDATES = 40
OWN_POOL, OWN_PER_SOURCE = 28, 10      # newest stories of the section's own feeds, at most 10 per outlet
EXTRA_POOL, EXTRA_PER_SOURCE = 14, 4   # extra candidates from general feeds, at most 4 per outlet
MAX_ITEMS = 12      # stories stored per section: 8 on the front page, the rest under "more"
MAX_PICKS = 14      # ask for more than we keep, some are dropped by the checks
MAX_ALSO = 3        # other outlets listed under a story
MAX_HEADLINE_CHARS = 90
MAX_SENTENCE_CHARS = 300
MAX_DETAIL_CHARS = 900
OFF_TOPIC = "bu bölüme uymuyor"  # the model must say so when a story does not belong; the code discards it
# Outlets whose terms restrict this use: their text is never fetched, only the feed teaser is used.
NO_BODY_SOURCES = {f["source"] for f in FEEDS if f["terms"] in ("restrictive", "personal-only")}
MAX_BODY_FOR_PROMPT = 2500   # characters of one article given to the model
MIN_GAIN = 150               # a fetched text must be this much longer than the teaser to count
CANDIDATE_COLUMNS = "id,title,summary,link,source,language,published_at"


def schema_for(category):
    """The response schema. `topic` is limited to the section's fixed sub-topics."""
    return {
        "type": "ARRAY",
        "items": {
            "type": "OBJECT",
            "properties": {
                "n": {"type": "INTEGER"},
                "title": {"type": "STRING"},
                "headline": {"type": "STRING"},
                "sentence": {"type": "STRING"},
                "detail": {"type": "STRING"},
                "topic": {"type": "STRING", "enum": CATEGORY_SUBTOPICS[category] + [OFF_TOPIC]},
                "also": {"type": "ARRAY", "items": {"type": "INTEGER"}},
            },
            "required": ["n", "title", "headline", "sentence", "detail", "topic", "also"],
        },
    }


# --- choosing the candidates ----------------------------------------------------------------

def newest_in(db, category, since):
    return db.select("articles", {
        "select": CANDIDATE_COLUMNS, "category": f"eq.{category}", "published_at": f"gte.{since}",
        "order": "published_at.desc", "limit": 200})


def balanced(rows, total, per_source):
    """Newest first, but no outlet may supply more than `per_source` of the candidates."""
    counts, picked = Counter(), []
    for row in rows:
        if counts[row["source"]] < per_source:
            picked.append(row)
            counts[row["source"]] += 1
        if len(picked) == total:
            break
    return picked


def pick_candidates(db, category, since):
    """The section's own newest stories plus, for sections listed in CATEGORY_EXTRA_SOURCES,
    some from the general feeds, mixed across outlets."""
    extras = CATEGORY_EXTRA_SOURCES.get(category, [])
    own_total = OWN_POOL if extras else MAX_CANDIDATES  # sections without extras use the whole budget
    chosen = balanced(newest_in(db, category, since), own_total, OWN_PER_SOURCE)
    for other in extras:
        chosen += balanced(newest_in(db, other, since), EXTRA_POOL, EXTRA_PER_SOURCE)
    unique = {row["id"]: row for row in chosen}.values()
    return sorted(unique, key=lambda r: r["published_at"], reverse=True)[:MAX_CANDIDATES]


# --- the model request ----------------------------------------------------------------------

def build_prompt(candidates, category):
    listing = "\n\n".join(
        f"[{i + 1}] {c['title']}\n{(c.get('summary') or '')[:400]}" for i, c in enumerate(candidates))
    topics = ", ".join(f'"{t}"' for t in CATEGORY_SUBTOPICS[category])
    return "\n".join([
        "You write the news digest for one section of a news site. Below are numbered candidate",
        "articles from the last two days (titles and short summaries, in Turkish or English).",
        f"This section is about: {CATEGORY_TOPICS[category]}.",
        "Skip candidates that are not about this section's subject, even if they are important.",
        f"Select between 10 and {MAX_PICKS} different stories, the most important and interesting first.",
        "Include smaller stories to reach at least 10 whenever the candidates allow it.",
        "If several candidates report the same event, make ONE story of it (the most informative candidate",
        "as n) and list the other candidates that report the same event in `also`.",
        "Skip advertisements, betting tips, broadcast guides and horoscopes.",
        "When several outlets cover different stories of similar importance, prefer a mix of outlets.",
        "",
        "Return each story as an object with these fields:",
        "  n         the candidate's number",
        "  title     that candidate's exact title, copied unchanged",
        f"  headline  a short Turkish newspaper headline, at most {MAX_HEADLINE_CHARS} characters, no final period",
        "  sentence  ONE Turkish sentence summarising the story",
        "  detail    a more detailed Turkish summary for readers who want more, as many sentences as the",
        "            candidates support (1 to 5, up to about 110 words); it may combine candidate n with the",
        "            candidates in `also`. If they hold little, a single sentence is right: never pad",
        f"  topic     exactly one of: {topics}, or \"{OFF_TOPIC}\". Use \"{OFF_TOPIC}\" for every story that is not",
        "            about this section's subject (for example an accident report in a politics section, or a",
        "            stock-market notice in a sports section): such stories are discarded. \"diğer\" means only a",
        "            story that IS about the section's subject but fits none of the listed sub-topics",
        "  also      numbers of other candidates that report the SAME event (at most 3), or an empty list",
        "",
        "Facts, names and numbers in headline, sentence and detail must come from candidate n or from the",
        "candidates in `also`. Never add anything from other candidates or from your own knowledge.",
        "Report what the candidates state and nothing more: no commentary, no speculation, no remarks about",
        "reactions, resonance or consequences unless a candidate states them.",
        "Write correct Turkish using all diacritics (ç, ğ, ı, ö, ş, ü, İ).",
        "Always write numbers as digits (for example 30 bin, 249 dolar, 15 milyar), never as words.",
        "Return the stories in order of importance.",
        "",
        "Candidates:",
        listing,
    ])


# --- turning the picks into stored stories -----------------------------------------------------

def story_dict(article, headline, sentence, detail, topic, also):
    """The shape stored in category_summaries.items and read by the site."""
    return {
        "headline": headline, "sentence": sentence, "detail": detail, "topic": topic,
        "source": article["source"], "language": article["language"], "link": article["link"],
        "published_at": article["published_at"], "from_article": False,
        "also": [{"source": a["source"], "link": a["link"], "language": a["language"]} for a in also],
    }


def plain_items(candidates):
    """Last resort when the model path fails: plain titles, nothing generated. Titles are kept in
    this mode only, because they are all there is to show."""
    items = []
    for a in candidates[:8]:
        story = story_dict(a, a["title"], a["title"], "", "diğer", [])
        story["title"] = a["title"]
        items.append(story)
    return items


def clean(text, limit):
    return (text or "").strip()[:limit].strip()


def accepted_also(pick, position, candidates, sentence):
    """The outlets listed under a story must really be about the same event: each one has to share a
    name or number with the story's sentence."""
    seen, accepted = {position}, []
    for number in pick.get("also") or []:
        if not isinstance(number, int) or not 1 <= number <= len(candidates):
            continue
        other = candidates[number - 1]
        if number - 1 in seen or other["source"] == candidates[position]["source"]:
            continue
        text = f"{other['title']} {other.get('summary') or ''}"
        if grounded(sentence, text, other["language"]):
            seen.add(number - 1)
            accepted.append(other)
        if len(accepted) == MAX_ALSO:
            break
    return accepted


def select_items(picks, candidates, category):
    """Returns (stories, dropped): dropped counts what each check removed."""
    index = title_index(candidates)
    allowed_topics = set(CATEGORY_SUBTOPICS[category])
    stories, seen = [], set()
    dropped = {"title": 0, "number": 0, "grounding": 0, "other": 0, "detail": 0, "headline": 0, "also": 0, "offtopic": 0, "padding": 0}
    for p in picks:
        position = match_title(p.get("title"), index)
        if position is None:
            dropped["title"] += 1  # the copied title matches no candidate
            continue
        sentence = clean(p.get("sentence"), MAX_SENTENCE_CHARS + 1)
        if position in seen or not sentence or len(sentence) > MAX_SENTENCE_CHARS:
            dropped["other"] += 1
            continue
        article = candidates[position]
        own_text = f"{article['title']} {article.get('summary') or ''}"

        # 1. the sentence must be supported by the article it is linked to
        ok = True
        if not numbers_supported(sentence, own_text):
            dropped["number"] += 1
            ok = False
        elif not grounded(sentence, own_text, article["language"]):
            dropped["grounding"] += 1
            ok = False
        if not ok:
            # A Turkish title is a safe plain sentence; an English one does not belong in a Turkish digest.
            if article["language"] != "tr":
                continue
            sentence = article["title"]

        # 2. other outlets, then the text that may draw on them
        also = accepted_also(p, position, candidates, sentence)
        dropped["also"] += len([n for n in (p.get("also") or []) if isinstance(n, int)]) - len(also)
        union_text = own_text + " " + " ".join(f"{a['title']} {a.get('summary') or ''}" for a in also)

        # 3. headline: short, and not carrying numbers the sources lack
        headline = clean(p.get("headline"), MAX_HEADLINE_CHARS + 1).rstrip(".")
        if (not headline or len(headline) > MAX_HEADLINE_CHARS or not numbers_supported(headline, union_text)
                or not grounded(headline, union_text, article["language"])):
            dropped["headline"] += 1
            headline = sentence.rstrip(".")[:MAX_HEADLINE_CHARS]

        # 4. detail: dropped (the story stays) if it carries unsupported numbers or loses the thread
        detail = clean(p.get("detail"), MAX_DETAIL_CHARS + 1)
        if detail and (len(detail) > MAX_DETAIL_CHARS or not numbers_supported(detail, union_text)
                       or not grounded(detail, union_text, article["language"])):
            dropped["detail"] += 1
            detail = ""
        if detail:
            kept = strip_unsupported_reactions(detail, union_text)  # padding such as "drew wide resonance"
            if kept != detail:
                dropped["padding"] += 1
            detail = kept

        topic = (p.get("topic") or "").strip().lower()
        if topic == OFF_TOPIC:
            dropped["offtopic"] += 1  # the model itself says this story does not belong in the section
            continue
        topic = topic if topic in allowed_topics else CATEGORY_SUBTOPICS[category][-1]

        seen.add(position)
        stories.append(story_dict(article, headline, sentence, detail, topic, also))
        if len(stories) == MAX_ITEMS:
            break
    return stories, dropped


# --- second step: write from the article itself --------------------------------------------------

def enrich_schema():
    return {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
        "n": {"type": "INTEGER"}, "headline": {"type": "STRING"},
        "sentence": {"type": "STRING"}, "detail": {"type": "STRING"}},
        "required": ["n", "headline", "sentence", "detail"]}}


def build_enrich_prompt(stories_with_text):
    blocks = "\n\n".join(f"[{i + 1}] {title}\n{text[:MAX_BODY_FOR_PROMPT]}"
                         for i, (title, text) in enumerate(stories_with_text))
    return "\n".join([
        "You write the news digest of a site whose readers want to learn what happened without opening",
        "the article. Below are numbered articles (Turkish or English): the title and the article text.",
        "For each article return an object with:",
        "  n         the article's number",
        f"  headline  a short Turkish headline (about 60 characters, never over {MAX_HEADLINE_CHARS}, no final period) that says what",
        "            happened or what was said. Never a teaser: do not write that someone made a statement,",
        "            spoke or commented without saying what. Put the substance in the headline",
        "  sentence  ONE Turkish sentence with the core of the story: who, what, the main figure or claim",
        "  detail    a Turkish summary of 2 to 5 sentences (up to about 110 words) with the main points the article",
        "            makes: the statements and what they were, the figures, the reasons, what happens next.",
        "            For a statement or interview, give what was actually said, in your own words",
        "Use only what the article text states. Add nothing from other sources or your own knowledge, no commentary,",
        "no remarks about reactions or resonance unless the text states them. If the text holds little,",
        "write less. Write correct Turkish with all diacritics. Write numbers as digits.",
        "",
        "Articles:",
        blocks,
    ])


def read_bodies(stories, candidates_by_link):
    """{link: text} for the stories whose article page may be read and holds more than the teaser."""
    def one(story):
        article = candidates_by_link[story["link"]]
        if article["source"] in NO_BODY_SOURCES:
            return story["link"], ""
        text, _ = fetch_text(story["link"], GATE, USER_AGENT)
        teaser = len(article.get("summary") or "")
        return story["link"], text if len(text) >= teaser + MIN_GAIN else ""
    with ThreadPoolExecutor(max_workers=6) as pool:
        return {link: text for link, text in pool.map(one, stories) if text}


def enrich(stories, candidates, _unused=None):
    """Rewrites headline, sentence and detail of stories that are new this run, from the article text.
    A rewrite replaces the first-step text only when it passes the same checks against the article text;
    otherwise the story keeps what it had. Returns (stories, counts)."""
    by_link = {c["link"]: c for c in candidates}
    bodies = read_bodies(stories, by_link)
    counts = {"read": len(bodies), "rewritten": 0, "rejected": 0}
    if not bodies:
        return stories, counts
    order = [st for st in stories if st["link"] in bodies]
    prompt = build_enrich_prompt([(by_link[st["link"]]["title"], bodies[st["link"]]) for st in order])
    for model in GENERATION_MODELS:
        try:
            rewrites = generate_json(model, prompt, enrich_schema())
            break
        except GenerationError as e:
            print("    enrichment failed:", str(e)[:80])
    else:
        return stories, counts
    for r in rewrites:
        n = r.get("n")
        if not isinstance(n, int) or not 1 <= n <= len(order):
            continue
        story = order[n - 1]
        article = by_link[story["link"]]
        source_text = f"{article['title']} {bodies[story['link']]}"
        lang = article["language"]
        sentence = clean(r.get("sentence"), MAX_SENTENCE_CHARS + 1)
        headline = clean(r.get("headline"), MAX_HEADLINE_CHARS + 1).rstrip(".")
        detail = clean(r.get("detail"), MAX_DETAIL_CHARS + 1)
        good = (sentence and len(sentence) <= MAX_SENTENCE_CHARS and numbers_supported(sentence, source_text)
                and grounded(sentence, source_text, lang))
        if not good:
            counts["rejected"] += 1
            continue
        story["sentence"] = sentence
        if headline and len(headline) <= MAX_HEADLINE_CHARS and numbers_supported(headline, source_text) \
                and grounded(headline, source_text, lang):
            story["headline"] = headline
        if detail and len(detail) <= MAX_DETAIL_CHARS and numbers_supported(detail, source_text) \
                and grounded(detail, source_text, lang):
            story["detail"] = strip_unsupported_reactions(detail, source_text)
        counts["rewritten"] += 1
    return stories, counts


def summarize(candidates, category):
    """Returns (mode, stories, model, dropped). Never raises for model problems."""
    prompt = build_prompt(candidates, category)
    schema = schema_for(category)
    errors = []
    for model in GENERATION_MODELS:
        try:
            picks = generate_json(model, prompt, schema)
        except GenerationError as e:
            errors.append(str(e))
            continue
        stories, dropped = select_items(picks, candidates, category)
        if stories:
            return "summary", stories, model, dropped
        errors.append(f"{model} selected nothing usable")
    print("    model path failed:", "; ".join(errors))
    return "fallback", plain_items(candidates), None, {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=CATEGORIES, help="run a single section")
    ap.add_argument("--force", action="store_true", help="regenerate even if nothing changed")
    args = ap.parse_args()

    db = Supabase()
    now = datetime.now(timezone.utc)
    since = (now - timedelta(hours=WINDOW_HOURS)).isoformat()
    stored = {r["category"]: r for r in db.select("category_summaries", {"select": "category,fingerprint,items"})}

    failed = []
    for category in ([args.only] if args.only else CATEGORIES):
        try:
            process_category(db, category, since, now, stored, args.force)
        except Exception as e:   # one section failing must not stop the others
            failed.append(category)
            print(f"{category:15} FAILED: {type(e).__name__}: {str(e)[:120]}")

    # The heartbeat is written on every run, even when every section was skipped.
    db.upsert("pipeline_status", [{"id": "main", "summarized_at": datetime.now(timezone.utc).isoformat()}])
    if failed:
        sys.exit(f"sections that failed: {failed}")


# --- headlines that only say that something was said ---------------------------------------------

# "Osimhen'den Lille maçı açıklaması": a headline that ends in the kind of word that hides the content.
VAGUE_ENDING = re.compile(r"(açıklama(sı|lar|ları|sına|ya)?|yorum(u|lar|ları)?|mesaj(ı|lar|ları)?|"
                          r"konuş(tu|ması|ma)|değerlendirme(si|ler)?)\s*$", re.I)


def vague_headline(headline):
    return bool(VAGUE_ENDING.search(headline.strip()))


def fix_vague_headlines(stories):
    """One request for the stories whose headline hides what was said. The new headline must say what was
    said and pass the same checks against the story's own sentence and detail, else the old one stays.
    Returns the number of headlines replaced."""
    todo = [st for st in stories if not st.get("headline_checked") and vague_headline(st["headline"])]
    if not todo:
        return 0
    listing = "\n\n".join(f"[{i + 1}] {st['headline']}\n{st['sentence']}\n{st.get('detail') or ''}" for i, st in enumerate(todo))
    prompt = "\n".join([
        "Each item below has a headline that only says that someone made a statement, gave a comment or sent a",
        "message, followed by the story's sentence and details. Write a new Turkish headline for each (about 60",
        f"characters, never over {MAX_HEADLINE_CHARS}, no final period) that says WHAT was said or announced, using",
        "only the sentence and details. Do not use the words açıklama, yorum, mesaj or konuştu as the point of the",
        "headline. If the text does not say what was said, return the old headline unchanged.",
        "Return objects {n, headline}.", "", listing])
    schema = {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {"n": {"type": "INTEGER"}, "headline": {"type": "STRING"}},
                                         "required": ["n", "headline"]}}
    for st in todo:
        st["headline_checked"] = True    # one attempt per story, whatever the outcome
    for model in GENERATION_MODELS:
        try:
            rewrites = generate_json(model, prompt, schema)
            break
        except GenerationError as e:
            print("    headline fix failed:", str(e)[:80])
    else:
        return 0
    replaced = 0
    for r in rewrites:
        n = r.get("n")
        if not isinstance(n, int) or not 1 <= n <= len(todo):
            continue
        story = todo[n - 1]
        new = clean(r.get("headline"), MAX_HEADLINE_CHARS + 1).rstrip(".")
        text = f"{story['sentence']} {story.get('detail') or ''}"
        if (new and len(new) <= MAX_HEADLINE_CHARS and not vague_headline(new) and numbers_supported(new, text)
                and grounded(new, text, story["language"])):
            story["headline"] = new
            replaced += 1
    return replaced


def write_from_articles(items, candidates, previous):
    """Stories already written from their article in an earlier run keep that text; new ones are read
    and rewritten. Returns (items, number read, number rewritten)."""
    done = {st["link"]: st for st in previous if st.get("from_article")}
    fresh = []
    for st in items:
        old = done.get(st["link"])
        if old:
            for key in ("headline", "sentence", "detail", "from_article", "headline_checked"):
                if key in old:
                    st[key] = old[key]
        else:
            fresh.append(st)
    if not fresh:
        return items, 0, 0
    rewritten_links = set()
    before = {st["link"]: (st["headline"], st["sentence"], st["detail"]) for st in fresh}
    _, counts = enrich(fresh, candidates, None)
    for st in fresh:
        if (st["headline"], st["sentence"], st["detail"]) != before[st["link"]]:
            st["from_article"] = True
    return items, counts["read"], counts["rewritten"]


def process_category(db, category, since, now, stored, force):
    started = time.time()
    candidates = pick_candidates(db, category, since)
    fingerprint = hashlib.md5("|".join(c["id"] for c in candidates).encode()).hexdigest()

    if not candidates:
        mode, items, model, dropped = "empty", [], None, {}
    elif not force and stored.get(category, {}).get("fingerprint") == fingerprint:
        print(f"{category:15} unchanged, skipped")
        return
    else:
        mode, items, model, dropped = summarize(candidates, category)
        if mode == "summary":
            items, dropped["read"], dropped["rewritten"] = write_from_articles(
                items, candidates, stored.get(category, {}).get("items") or [])
            dropped["vague"] = fix_vague_headlines(items)

    db.upsert("category_summaries", [{
        "category": category, "mode": mode, "items": items, "model": model,
        "article_count": len(candidates), "fingerprint": fingerprint,
        "newest_article_at": candidates[0]["published_at"] if candidates else None,
        "generated_at": now.isoformat(),
    }], conflict="category")
    drops = ",".join(f"{k}={v}" for k, v in dropped.items() if v) or "none"
    with_detail = sum(1 for i in items if i.get("detail"))
    print(f"{category:15} {mode:9} stories={len(items)} detail={with_detail} candidates={len(candidates)} "
          f"model={model} dropped({drops}) {time.time() - started:.1f}s")


if __name__ == "__main__":
    main()
