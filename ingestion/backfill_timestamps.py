import json
from datetime import datetime
from dateutil import parser as date_parser

DATA_FILE = "ingestion/news_data.json"


def backfill():
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        news = json.load(f)

    updated = 0
    for item in news:
        if "published_ts" in item:
            continue  # already has it, skip

        try:
            dt = date_parser.parse(item["published"])
            item["published_ts"] = int(dt.timestamp())
        except (ValueError, TypeError, KeyError):
            # couldn't parse the date string - fall back to fetch time
            item["published_ts"] = int(datetime.fromisoformat(item["fetched_at"]).timestamp())

        updated += 1

    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(news, f, ensure_ascii=False, indent=2)

    print(f"Backfilled published_ts for {updated} article(s).")


if __name__ == "__main__":
    backfill()