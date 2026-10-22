import feedparser
import json
import os
import calendar
from datetime import datetime

# Add the feeds you tested and confirmed working
FEEDS = {
    "NTV Gundem": "https://www.ntv.com.tr/gundem.rss",
    "NTV Teknoloji": "https://www.ntv.com.tr/teknoloji.rss",
    "AA": "https://www.aa.com.tr/tr/rss/default?cat=guncel",
    "Hurriyet": "http://www.hurriyet.com.tr/rss/anasayfa",
    "Hurriyet Spor": "http://www.hurriyet.com.tr/rss/spor",
    "Sabah": "https://www.sabah.com.tr/rss/gundem.xml",
    "Sabah Spor": "https://www.sabah.com.tr/rss/spor.xml",
    "CNN Turk": "https://www.cnnturk.com/feed/rss/all/news",
    "CNN Turk Spor": "https://www.cnnturk.com/feed/rss/spor/news",
    "BBC Turkce": "http://feeds.bbci.co.uk/turkce/rss.xml",
    "DW Turkce": "http://rss.dw.com/rdf/rss-tur-all",
}

DATA_FILE = "ingestion/news_data.json"

# Patterns to filter out non-article junk entries (official announcements, channel promos, etc.)
JUNK_LINK_PATTERNS = ["whatsapp.com", "wa.me", "/resmi-ilanlar/"]
JUNK_TEXT_PATTERNS = ["abone olmak için tıklayın", "başkanlığı.", "canlı izle", "şifresiz izle", "canlı yayın izle"]


def parse_timestamp(entry, fallback_iso):
    """Converts the RSS entry's publish date into a UTC epoch int, so recency
    can be compared numerically later. Falls back to fetch time if the feed
    doesn't provide a parseable date."""
    parsed = entry.get("published_parsed")
    if parsed:
        return calendar.timegm(parsed)
    # Fallback: treat it as fetched-now (better than crashing / leaving it undated)
    return int(datetime.fromisoformat(fallback_iso).timestamp())


def turkish_lower(text):
    """Python's default str.lower() mishandles Turkish 'İ' (dotted capital I) -
    it turns it into 'i' + a combining dot character, not a plain 'i', which
    silently breaks substring matching against patterns like "canlı izle".
    Normalize the Turkish dotted/dotless I pair manually before lowering."""
    return text.replace("İ", "i").replace("I", "ı").lower()


def is_junk(link, title, summary):
    link, title, summary = turkish_lower(link), turkish_lower(title), turkish_lower(summary)
    for pattern in JUNK_LINK_PATTERNS:
        if pattern in link:
            return True
    for pattern in JUNK_TEXT_PATTERNS:
        if pattern in title or pattern in summary:
            return True
    return False


def load_existing():
    """Load previously saved articles, if any."""
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def fetch_all():
    existing = load_existing()
    existing_links = {item["link"] for item in existing}  # used for duplicate checking

    new_items = []
    for source_name, url in FEEDS.items():
        parsed = feedparser.parse(url)
        for entry in parsed.entries:
            link = entry.get("link", "")
            title = entry.get("title", "")
            summary = entry.get("summary", entry.get("description", ""))

            if not link or link in existing_links:
                continue  # already exists, skip
            if is_junk(link, title, summary):
                continue  # not a real article, skip

            fetched_at = datetime.now().isoformat()
            item = {
                "source": source_name,
                "title": title,
                "summary": summary,
                "link": link,
                "published": entry.get("published", ""),
                "published_ts": parse_timestamp(entry, fetched_at),
                "fetched_at": fetched_at,
            }
            new_items.append(item)
            existing_links.add(link)

    all_items = existing + new_items
    os.makedirs("ingestion", exist_ok=True)
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(all_items, f, ensure_ascii=False, indent=2)

    print(f"Newly added article count: {len(new_items)}")
    print(f"Total article count: {len(all_items)}")
    return new_items  # only the newly added ones - the index update only needs these


if __name__ == "__main__":
    fetch_all()