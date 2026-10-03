import json

DATA_FILE = "ingestion/news_data.json"
FLAGGED_LINK = "https://www.sabah.com.tr/spor/futbol/2026/10/02/besiktasa-sakat-oyuncularindan-mujde-milli-ara-ilac-gibi-geldi"
SLUG = "besiktasa-sakat-oyuncularindan-mujde"

with open(DATA_FILE, "r", encoding="utf-8") as f:
    news = json.load(f)

exact_match = [item for item in news if item["link"] == FLAGGED_LINK]
partial_matches = [item for item in news if SLUG in item["link"]]

print(f"Exact match found: {len(exact_match) > 0}")
print(f"\nPartial matches (same story slug) found: {len(partial_matches)}")
for item in partial_matches:
    print(f"  source={item['source']!r}")
    print(f"  link={item['link']!r}")
    print(f"  link length={len(item['link'])}")
    print()

print(f"Flagged link length: {len(FLAGGED_LINK)}")
print(f"Flagged link repr: {FLAGGED_LINK!r}")