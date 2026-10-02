import json
from datetime import datetime

DATA_FILE = "ingestion/news_data.json"
CATEGORY_SOURCES = ["Hurriyet Spor", "Sabah Spor", "CNN Turk Spor"]

with open(DATA_FILE, "r", encoding="utf-8") as f:
    news = json.load(f)

category_items = [item for item in news if item["source"] in CATEGORY_SOURCES]

print(f"Total articles in these sources: {len(category_items)}\n")

now_ts = int(datetime.now().timestamp())

# Sort by recency, newest first
category_items.sort(key=lambda x: x.get("published_ts", 0), reverse=True)

print(f"{'Source':<18} {'Age (hours)':<14} {'Published (raw)':<35} Title")
print("-" * 110)
for item in category_items[:25]:
    ts = item.get("published_ts", 0)
    age_hours = (now_ts - ts) / 3600 if ts else -1
    print(f"{item['source']:<18} {age_hours:<14.1f} {item['published'][:33]:<35} {item['title'][:50]}")