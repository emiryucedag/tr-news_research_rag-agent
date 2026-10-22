"""Spike S1: embed articles with Gemini and load them into Supabase.

Throwaway script for the architecture spike. The helpers below are copied from
rag/build_index.py on purpose, so this script does not import torch/chromadb.

Run from backend/:
    ../.venv/bin/python spike/embed_articles.py --limit 50
"""
import argparse
import hashlib
import html
import json
import re
import time
from datetime import datetime, timezone

import httpx

ENV_FILE = ".env"
DATA_FILE = "ingestion/news_data.json"
EMBED_MODEL = "gemini-embedding-001"
EMBED_DIMS = 768
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models"

JUNK_LINK_PATTERNS = ["whatsapp.com", "wa.me", "/resmi-ilanlar/", "/advertorial/", "/sponsorlu/", "/marka-icerik/"]
JUNK_TEXT_PATTERNS = ["abone olmak için tıklayın", "başkanlığı.", "canlı izle", "şifresiz izle", "canlı yayın izle"]


def load_env(path):
    env = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            if "=" in line and not line.startswith("#"):
                name, value = line.split("=", 1)
                env[name.strip()] = value.strip()
    return env


def clean_text(text):
    text = html.unescape(text)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def turkish_lower(text):
    return text.replace("İ", "i").replace("I", "ı").lower()


def is_junk(item):
    link = turkish_lower(item.get("link", ""))
    title = turkish_lower(item.get("title", ""))
    summary = turkish_lower(item.get("summary", ""))
    return any(p in link for p in JUNK_LINK_PATTERNS) or any(p in title or p in summary for p in JUNK_TEXT_PATTERNS)


def make_id(link):
    return hashlib.md5(link.encode("utf-8")).hexdigest()


def pick_articles(limit):
    with open(DATA_FILE, encoding="utf-8") as f:
        news = json.load(f)
    news.sort(key=lambda a: a.get("published_ts", 0), reverse=True)
    rows = []
    for item in news:
        if is_junk(item):
            continue
        title = clean_text(item["title"])
        summary = clean_text(item["summary"])
        text = f"{title}. {summary}"
        if len(text) < 15:
            continue
        rows.append({
            "id": make_id(item["link"]),
            "title": title,
            "summary": summary,
            "link": item["link"],
            "source": item["source"],
            "feed": item["source"],  # the JSON keeps only one name per article
            "language": "tr",
            # DW has no dates in its feed; fetch_news.py stores the fetch time in
            # published_ts for those, so published_ts is always set.
            "published_at": datetime.fromtimestamp(item["published_ts"], tz=timezone.utc).isoformat(),
            "_text": text,
        })
        if len(rows) == limit:
            break
    return rows


def embed_batch(client, key, texts):
    """One batchEmbedContents request. Returns a list of 768-float vectors."""
    body = {"requests": [{
        "model": f"models/{EMBED_MODEL}",
        "content": {"parts": [{"text": t}]},
        "taskType": "RETRIEVAL_DOCUMENT",
        "outputDimensionality": EMBED_DIMS,
    } for t in texts]}
    r = client.post(f"{GEMINI_URL}/{EMBED_MODEL}:batchEmbedContents",
                    headers={"x-goog-api-key": key}, json=body, timeout=120)
    if r.status_code != 200:
        raise SystemExit(f"Gemini HTTP {r.status_code}: {r.json().get('error', {}).get('message', '')[:200]}")
    return [e["values"] for e in r.json()["embeddings"]]


def upsert(client, url, secret, rows):
    """Insert rows; if an id already exists, overwrite it (safe to re-run)."""
    r = client.post(
        f"{url}/rest/v1/articles",
        headers={"apikey": secret, "Content-Type": "application/json",
                 "Prefer": "resolution=merge-duplicates,return=minimal"},
        json=rows, timeout=120)
    if r.status_code not in (200, 201, 204):
        raise SystemExit(f"Supabase HTTP {r.status_code}: {r.text[:300]}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=200, help="how many of the newest articles to load")
    parser.add_argument("--batch", type=int, default=50, help="texts per embedding request (max 100)")
    args = parser.parse_args()

    env = load_env(ENV_FILE)
    gem_key, sb_url, sb_secret = env["GEMINI_API_KEY"], env["SUPABASE_URL"].rstrip("/"), env["SUPABASE_SECRET_KEY"]

    rows = pick_articles(args.limit)
    print(f"Articles selected: {len(rows)} (batch size {args.batch}, model {EMBED_MODEL}, {EMBED_DIMS} dims)")

    embed_requests = 0
    with httpx.Client() as client:
        for start in range(0, len(rows), args.batch):
            chunk = rows[start:start + args.batch]
            vectors = embed_batch(client, gem_key, [r["_text"] for r in chunk])
            embed_requests += 1
            records = []
            for r, vec in zip(chunk, vectors):
                rec = {k: v for k, v in r.items() if not k.startswith("_")}
                rec["embedding"] = "[" + ",".join(f"{x:.6f}" for x in vec) + "]"  # pgvector text format
                records.append(rec)
            upsert(client, sb_url, sb_secret, records)
            print(f"  batch {start // args.batch + 1}: embedded and stored {len(records)} articles")
            time.sleep(1)  # stay far below 100 requests per minute

    print(f"Done. Embedding requests sent: {embed_requests} (texts embedded: {len(rows)})")


if __name__ == "__main__":
    main()
