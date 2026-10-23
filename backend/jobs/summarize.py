"""Hourly category summaries: for each category, pick up to 8 stories from the last
48 hours and store one Turkish sentence per story, ready for the web app to read.

The model only chooses stories and writes sentences. Links, sources and dates come
from the database, never from the model. Each sentence must pass the checks in
guards.py before it is kept. A category is skipped when its candidate set has not
changed since the last run, so quiet hours cost no model requests.

Run from backend/:
    ../.venv/bin/python jobs/summarize.py              # all categories
    ../.venv/bin/python jobs/summarize.py --only spor --force
"""
import argparse
import hashlib
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import GENERATION_MODELS, GenerationError, Supabase, generate_json
from collections import Counter

from feeds import CATEGORIES, CATEGORY_EXTRA_SOURCES, CATEGORY_TOPICS
from guards import grounded, match_title, numbers_supported, title_index

WINDOW_HOURS = 48
MAX_CANDIDATES = 40
OWN_POOL, OWN_PER_SOURCE = 28, 10      # newest stories of the section's own feeds, at most 10 per outlet
EXTRA_POOL, EXTRA_PER_SOURCE = 14, 4   # extra candidates from general feeds, at most 4 per outlet
MAX_ITEMS = 8
MAX_PICKS = 10  # ask for more than we show, some may be dropped by the checks
MAX_SENTENCE_CHARS = 300

SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "n": {"type": "INTEGER"},
            "title": {"type": "STRING"},
            "sentence": {"type": "STRING"},
        },
        "required": ["n", "title", "sentence"],
    },
}



CANDIDATE_COLUMNS = "id,title,summary,link,source,language,published_at"


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


def build_prompt(candidates, category):
    listing = "\n\n".join(
        f"[{i + 1}] {c['title']}\n{(c.get('summary') or '')[:400]}" for i, c in enumerate(candidates))
    return "\n".join([
        "You write the news digest for one section of a news site. Below are numbered candidate",
        "articles from the last two days (titles and short summaries, in Turkish or English).",
        f"This section is about: {CATEGORY_TOPICS[category]}.",
        "Skip candidates that are not about this section's subject, even if they are important.",
        f"Select up to {MAX_PICKS} different stories, the most important and interesting first.",
        "If several candidates report the same event, choose only the most informative one.",
        "Skip advertisements, betting tips, broadcast guides and horoscopes.",
        "When several outlets cover different stories of similar importance, prefer a mix of outlets.",
        "Return each story as {n, title, sentence}: n is the candidate's number, title is that",
        "candidate's exact title copied unchanged, and sentence is ONE sentence in Turkish, in",
        "your own words, based only on that candidate's title and summary. Do not add facts, names",
        "or numbers that are not in that candidate, and do not use information from other candidates.",
        "Write correct Turkish using all diacritics (ç, ğ, ı, ö, ş, ü, İ).",
        "Always write numbers as digits (for example 30 bin, 249 dolar, 15 milyar), never as words.",
        "Return the stories in order of importance.",
        "",
        "Candidates:",
        listing,
    ])


def item_from(article, sentence):
    return {"sentence": sentence, "title": article["title"], "source": article["source"],
            "link": article["link"], "language": article["language"],
            "published_at": article["published_at"]}


def plain_items(candidates):
    return [item_from(a, a["title"]) for a in candidates[:MAX_ITEMS]]


def select_items(picks, candidates):
    """Turn the model's picks into stored items. Returns (items, dropped) where dropped counts
    the picks removed by each check."""
    index = title_index(candidates)
    items, seen = [], set()
    dropped = {"title": 0, "number": 0, "grounding": 0, "other": 0}
    for p in picks:
        sentence = (p.get("sentence") or "").strip()
        position = match_title(p.get("title"), index)
        if position is None:
            dropped["title"] += 1  # the copied title matches no candidate
            continue
        if position in seen or not sentence or len(sentence) > MAX_SENTENCE_CHARS:
            dropped["other"] += 1
            continue
        article = candidates[position]
        source_text = f"{article['title']} {article.get('summary') or ''}"
        ok = True
        if not numbers_supported(sentence, source_text):
            dropped["number"] += 1
            ok = False
        elif not grounded(sentence, source_text, article["language"]):
            dropped["grounding"] += 1
            ok = False
        if not ok:
            # A Turkish title is a safe plain sentence; an English one does not belong in a
            # Turkish digest, so that story is left out.
            if article["language"] != "tr":
                continue
            sentence = article["title"]
        seen.add(position)
        items.append(item_from(article, sentence))
        if len(items) == MAX_ITEMS:
            break
    return items, dropped


def summarize(candidates, category):
    """Returns (mode, items, model, dropped). Never raises for model problems."""
    prompt = build_prompt(candidates, category)
    errors = []
    for model in GENERATION_MODELS:
        try:
            picks = generate_json(model, prompt, SCHEMA)
        except GenerationError as e:
            errors.append(str(e))
            continue
        items, dropped = select_items(picks, candidates)
        if items:
            return "summary", items, model, dropped
        errors.append(f"{model} selected nothing usable")
    print("    model path failed:", "; ".join(errors))
    return "fallback", plain_items(candidates), None, {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=CATEGORIES, help="run a single category")
    ap.add_argument("--force", action="store_true", help="regenerate even if nothing changed")
    args = ap.parse_args()

    db = Supabase()
    now = datetime.now(timezone.utc)
    since = (now - timedelta(hours=WINDOW_HOURS)).isoformat()
    stored = {r["category"]: r for r in db.select("category_summaries", {"select": "category,fingerprint"})}

    for category in ([args.only] if args.only else CATEGORIES):
        started = time.time()
        candidates = pick_candidates(db, category, since)
        fingerprint = hashlib.md5("|".join(c["id"] for c in candidates).encode()).hexdigest()

        if not candidates:
            mode, items, model, dropped = "empty", [], None, {}
        elif not args.force and stored.get(category, {}).get("fingerprint") == fingerprint:
            print(f"{category:15} unchanged, skipped")
            continue
        else:
            mode, items, model, dropped = summarize(candidates, category)

        db.upsert("category_summaries", [{
            "category": category, "mode": mode, "items": items, "model": model,
            "article_count": len(candidates), "fingerprint": fingerprint,
            "newest_article_at": candidates[0]["published_at"] if candidates else None,
            "generated_at": now.isoformat(),
        }], conflict="category")
        drops = ",".join(f"{k}={v}" for k, v in dropped.items() if v) or "none"
        print(f"{category:15} {mode:9} items={len(items)} candidates={len(candidates)} "
              f"model={model} dropped({drops}) {time.time() - started:.1f}s")


if __name__ == "__main__":
    main()
