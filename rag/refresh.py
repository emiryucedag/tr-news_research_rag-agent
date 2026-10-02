import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ingestion.fetch_news import fetch_all
from rag.build_index import add_items_to_index


def refresh_index():
    """
    Checks the RSS feeds for new articles, saves them to news_data.json (fetch_all
    already handles dedup), and embeds + upserts only the new ones into the
    vector DB. Fast, because it skips re-embedding everything that's already indexed.
    """
    print("Checking RSS feeds for new articles...")
    new_items = fetch_all()

    if not new_items:
        print("No new articles found, index is already up to date.")
        return 0

    added_count = add_items_to_index(new_items)
    print(f"Added {added_count} new article(s) to the vector DB.")
    return added_count


if __name__ == "__main__":
    refresh_index()