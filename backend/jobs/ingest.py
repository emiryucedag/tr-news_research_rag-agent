"""Hourly ingestion: fetch feeds -> filter -> store -> embed within budget -> delete old rows.

Run from backend/:
    ../.venv/bin/python jobs/ingest.py --dry-run     # fetch and report only, no writes
    ../.venv/bin/python jobs/ingest.py               # the real run
"""
import argparse
import calendar
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (RETENTION_DAYS, EmbedQuotaError, Supabase, clean_text, embed_documents,
                    embedding_text, is_junk, make_id, vector_literal)
from feeds import FEEDS
from robots import RobotsGate

USER_AGENT = "Mozilla/5.0 (compatible; news-agent/0.1; hobby project)"
GATE = RobotsGate(USER_AGENT)
MAX_SUMMARY_CHARS = 600


# --- 1. fetch ---------------------------------------------------------------

def fetch_feed(feed):
    """Returns (feed, entries or None, error text)."""
    allowed, reason = GATE.check(feed["url"])
    if not allowed:
        return feed, None, reason  # the site's robots.txt does not allow us (or could not be read)
    try:
        r = httpx.get(feed["url"], headers={"User-Agent": USER_AGENT}, timeout=20, follow_redirects=True)
        if r.status_code != 200:
            return feed, None, f"HTTP {r.status_code}"
        parsed = feedparser.parse(r.content)
        if not parsed.entries:
            return feed, None, "no entries"
        return feed, parsed.entries, ""
    except Exception as e:
        return feed, None, str(e)[:60]


def entry_time(entry, feed, now):
    """Publish time as an aware UTC datetime, corrected per feed and never in the future.
    Feeds without dates (DW) get the fetch time."""
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return now
    ts = calendar.timegm(parsed) + feed["tz_fix_hours"] * 3600
    return min(datetime.fromtimestamp(ts, tz=timezone.utc), now)


# --- 2. filter --------------------------------------------------------------

def feed_order(feeds):
    """Specific feeds first, the general 'gundem' feeds last. An article that shows up in
    both keeps the more specific category (first one wins)."""
    return sorted(feeds, key=lambda f: f["category"] == "gundem")


def build_rows(results, now, retention_days):
    cutoff = now - timedelta(days=retention_days)
    seen, rows, stats = set(), [], {}
    for feed, entries, error in results:
        kept = junk = old = dup = 0
        for e in entries or []:
            link = (e.get("link") or "").strip()
            title = clean_text(e.get("title"))
            summary = clean_text(e.get("summary") or e.get("description"))[:MAX_SUMMARY_CHARS]
            if not link.startswith("http") or not title:
                continue
            if is_junk(link, title, summary):
                junk += 1
                continue
            published = entry_time(e, feed, now)
            if published < cutoff:
                old += 1
                continue
            article_id = make_id(link)
            if article_id in seen:
                dup += 1
                continue
            seen.add(article_id)
            kept += 1
            rows.append({
                "id": article_id, "title": title, "summary": summary, "link": link,
                "source": feed["source"], "feed": feed["name"], "category": feed["category"],
                "language": feed["language"], "published_at": published.isoformat(),
            })
        stats[feed["name"]] = {"kept": kept, "junk": junk, "old": old, "dup": dup, "error": error,
                               "category": feed["category"]}
    return rows, stats


# --- 4. embed within budget ---------------------------------------------------

def round_robin_by_category(rows, n):
    """Newest first inside each category, then take one per category in turn so one busy
    category cannot use the whole budget."""
    groups = defaultdict(list)
    for r in sorted(rows, key=lambda r: r["published_at"], reverse=True):
        groups[r["category"]].append(r)
    picked = []
    while len(picked) < n and any(groups.values()):
        for cat in sorted(groups):
            if groups[cat] and len(picked) < n:
                picked.append(groups[cat].pop(0))
    return picked


def embed_pending(db, per_run, daily_cap, now):
    since = (now - timedelta(hours=24)).isoformat()
    used = db.count("articles", {"embedded_at": f"gte.{since}"})
    allowed = max(0, min(per_run, daily_cap - used, 100))
    print(f"embedding budget: used in last 24h = {used}/{daily_cap}, this run may embed {allowed}")
    if allowed == 0:
        return 0
    pending = db.select("articles", {
        "select": "id,title,summary,link,source,feed,category,language,published_at",
        "embedding": "is.null", "order": "published_at.desc", "limit": 500})
    batch = round_robin_by_category(pending, allowed)
    if not batch:
        return 0
    try:
        vectors = embed_documents([embedding_text(r) for r in batch])
    except EmbedQuotaError as e:
        print("embedding quota reached, leaving the rest for the next run:", e)
        return 0
    stamp = now.isoformat()
    db.upsert("articles", [{**r, "embedding": vector_literal(v), "embedded_at": stamp}
                           for r, v in zip(batch, vectors)])
    return len(batch)


# --- main -------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="fetch and report, write nothing")
    ap.add_argument("--embed-per-run", type=int, default=35)
    ap.add_argument("--daily-embed-cap", type=int, default=850,
                    help="texts per rolling 24h; the free tier allows about 1K and chat needs about 130")
    ap.add_argument("--retention-days", type=int, default=RETENTION_DAYS)
    args = ap.parse_args()

    started = time.time()
    now = datetime.now(timezone.utc)
    db = Supabase()

    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(fetch_feed, feed_order(FEEDS)))
    rows, stats = build_rows(results, now, args.retention_days)

    failed = {n: s["error"] for n, s in stats.items() if s["error"]}
    print(f"feeds: {len(FEEDS) - len(failed)} ok, {len(failed)} failed {failed if failed else ''}")
    existing = {r["id"]: r["category"] for r in db.select_all("articles", {"select": "id,category"})}
    new_rows = [r for r in rows if r["id"] not in existing]
    # A topical feed beats a general one: a story first stored as "gundem" moves to the more
    # specific category when a dedicated feed carries it too.
    reclassified = [r for r in rows
                    if existing.get(r["id"]) == "gundem" and r["category"] != "gundem"]
    by_cat = defaultdict(int)
    for r in new_rows:
        by_cat[r["category"]] += 1
    print(f"articles in window: {len(rows)} | already stored: {len(rows) - len(new_rows)} | new: {len(new_rows)} "
          f"| moved from gundem to a topical category: {len(reclassified)}")
    print("new by category:", dict(sorted(by_cat.items())))

    if args.dry_run:
        print("\nper feed (kept / junk / older than window / duplicate):")
        for name, s in stats.items():
            note = s["error"] or ""
            print(f"  {s['category']:15}{name:22}{s['kept']:4}{s['junk']:5}{s['old']:5}{s['dup']:5}  {note}")
        print("dry run: nothing written")
        return

    db.insert_ignore("articles", new_rows)
    if reclassified:
        db.upsert("articles", reclassified)  # embedding columns are not sent, so they stay as they are
    embedded = embed_pending(db, args.embed_per_run, args.daily_embed_cap, now)
    cutoff = (now - timedelta(days=args.retention_days)).isoformat()
    deleted = db.delete("articles", {"published_at": f"lt.{cutoff}"})
    total = db.count("articles", {})
    unembedded = db.count("articles", {"embedding": "is.null"})
    print(f"stored {len(new_rows)} new | embedded {embedded} | deleted {deleted} older than "
          f"{args.retention_days} days | table now {total} rows, {unembedded} without embedding "
          f"| {time.time() - started:.0f}s")


if __name__ == "__main__":
    main()
