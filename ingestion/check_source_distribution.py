import json
from collections import Counter

DATA_FILE = "ingestion/news_data.json"

with open(DATA_FILE, "r", encoding="utf-8") as f:
    news = json.load(f)

source_counts = Counter(item["source"] for item in news)

print(f"Total articles: {len(news)}\n")
print(f"{'Source':<20} {'Count':<8} {'Share'}")
print("-" * 40)
for source, count in source_counts.most_common():
    share = count / len(news) * 100
    print(f"{source:<20} {count:<8} {share:.1f}%")