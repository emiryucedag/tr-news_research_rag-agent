"""Hourly category summaries: for each category, pick up to 8 stories from the last
48 hours and store one Turkish sentence per story, ready for the web app to read.

The model only chooses stories and writes sentences. Links, sources and dates come
from the database, never from the model. A category is skipped when its candidate
set has not changed since the last run, so quiet hours cost no model requests.

Run from backend/:
    ../.venv/bin/python jobs/summarize.py              # all categories
    ../.venv/bin/python jobs/summarize.py --only spor --force
"""
import argparse
import hashlib
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import GENERATION_MODELS, GenerationError, Supabase, generate_json
from feeds import CATEGORIES

WINDOW_HOURS = 48
MAX_CANDIDATES = 40
MAX_ITEMS = 8
MAX_PICKS = 10  # ask for more than we show, some may be dropped by the checks
MAX_SENTENCE_CHARS = 300

SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {"n": {"type": "INTEGER"}, "sentence": {"type": "STRING"}},
        "required": ["n", "sentence"],
    },
}


def build_prompt(candidates):
    listing = "\n\n".join(
        f"[{i + 1}] {c['title']}\n{(c.get('summary') or '')[:400]}" for i, c in enumerate(candidates))
    return "\n".join([
        "You write the news digest for one section of a news site. Below are numbered candidate",
        "articles from the last two days (titles and short summaries, in Turkish or English).",
        f"Select up to {MAX_PICKS} different stories, the most important and interesting first.",
        "If several candidates report the same event, choose only the most informative one.",
        "Skip advertisements, betting tips, broadcast guides and horoscopes.",
        "For each selected story write ONE sentence in Turkish, in your own words, based only on",
        "the candidate's title and summary. Do not add facts, names or numbers that are not in",
        "the candidate. Write correct Turkish using all diacritics (ç, ğ, ı, ö, ş, ü, İ).",
        "Always write numbers as digits (for example 30 bin, 249 dolar, 15 milyar), never as words.",
        "Return the stories in order of importance.",
        "",
        "Candidates:",
        listing,
    ])


NUMBER_WORDS = {
    1: ("one", "bir"), 2: ("two", "iki"), 3: ("three", "üç"), 4: ("four", "dört"),
    5: ("five", "beş"), 6: ("six", "altı"), 7: ("seven", "yedi"), 8: ("eight", "sekiz"),
    9: ("nine", "dokuz"), 10: ("ten", "on"), 11: ("eleven", "on bir"), 12: ("twelve", "on iki"),
    13: ("thirteen",), 14: ("fourteen",), 15: ("fifteen",), 16: ("sixteen",), 17: ("seventeen",),
    18: ("eighteen",), 19: ("nineteen",), 20: ("twenty", "yirmi"),
}


def numbers_supported(sentence, source_text):
    """Every number in the sentence must appear in the candidate's own text, as digits or
    as a number word (sources often write 'two weeks', the sentence says '2 hafta')."""
    text = source_text.lower()
    for num in re.findall(r"\d+", sentence):
        if num in text:
            continue
        words = NUMBER_WORDS.get(int(num), ()) if num.isdigit() and len(num) <= 2 else ()
        if not any(re.search(rf"\b{w}\b", text) for w in words):
            return False
    return True


def item_from(article, sentence):
    return {"sentence": sentence, "title": article["title"], "source": article["source"],
            "link": article["link"], "language": article["language"],
            "published_at": article["published_at"]}


def plain_items(candidates):
    return [item_from(a, a["title"]) for a in candidates[:MAX_ITEMS]]


def summarize(candidates):
    """Returns (mode, items, model, guarded). Never raises for model problems."""
    prompt = build_prompt(candidates)
    errors = []
    for model in GENERATION_MODELS:
        try:
            picks = generate_json(model, prompt, SCHEMA)
        except GenerationError as e:
            errors.append(str(e))
            continue
        items, seen, guarded = [], set(), 0
        for p in picks:
            n, sentence = p.get("n"), (p.get("sentence") or "").strip()
            if not isinstance(n, int) or not 1 <= n <= len(candidates) or n in seen:
                continue
            if not sentence or len(sentence) > MAX_SENTENCE_CHARS:
                continue
            seen.add(n)
            article = candidates[n - 1]
            source_text = f"{article['title']} {article.get('summary') or ''}"
            if not numbers_supported(sentence, source_text):
                guarded += 1
                if article["language"] != "tr":
                    continue  # an English title does not belong in a Turkish digest
                sentence = article["title"]
            items.append(item_from(article, sentence))
            if len(items) == MAX_ITEMS:
                break
        if items:
            return "summary", items, model, guarded
        errors.append(f"{model} selected nothing usable")
    print("    model path failed:", "; ".join(errors))
    return "fallback", plain_items(candidates), None, 0


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
        candidates = db.select("articles", {
            "select": "id,title,summary,link,source,language,published_at",
            "category": f"eq.{category}", "published_at": f"gte.{since}",
            "order": "published_at.desc", "limit": MAX_CANDIDATES})
        fingerprint = hashlib.md5("|".join(c["id"] for c in candidates).encode()).hexdigest()

        if not candidates:
            mode, items, model, guarded = "empty", [], None, 0
        elif not args.force and stored.get(category, {}).get("fingerprint") == fingerprint:
            print(f"{category:15} unchanged, skipped")
            continue
        else:
            mode, items, model, guarded = summarize(candidates)

        db.upsert("category_summaries", [{
            "category": category, "mode": mode, "items": items, "model": model,
            "article_count": len(candidates), "fingerprint": fingerprint,
            "newest_article_at": candidates[0]["published_at"] if candidates else None,
            "generated_at": now.isoformat(),
        }], conflict="category")
        print(f"{category:15} {mode:9} items={len(items)} candidates={len(candidates)} "
              f"model={model} number-guard={guarded} {time.time() - started:.1f}s")


if __name__ == "__main__":
    main()
