import json
import re
import html
import hashlib
import chromadb
from sentence_transformers import SentenceTransformer

DATA_FILE = "ingestion/news_data.json"
CHROMA_DIR = "rag/chroma_db"
COLLECTION_NAME = "news"

MODEL_NAME = "paraphrase-multilingual-MiniLM-L12-v2"

JUNK_LINK_PATTERNS = ["whatsapp.com", "wa.me", "/resmi-ilanlar/", "/advertorial/", "/sponsorlu/", "/marka-icerik/"]
JUNK_TEXT_PATTERNS = ["abone olmak için tıklayın", "başkanlığı.", "canlı izle", "şifresiz izle", "canlı yayın izle"]

_model = None


def _get_model():
    global _model
    if _model is None:
        _model = SentenceTransformer(MODEL_NAME)
    return _model


def clean_text(text):
    """Strip HTML entities/tags (leftovers like &nbsp; from some RSS feeds)."""
    text = html.unescape(text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def turkish_lower(text):
    """Python's default str.lower() mishandles Turkish 'İ' (dotted capital I) -
    it turns it into 'i' + a combining dot character, not a plain 'i', which
    silently breaks substring matching against patterns like "canlı izle".
    Normalize the Turkish dotted/dotless I pair manually before lowering."""
    return text.replace("İ", "i").replace("I", "ı").lower()


def is_junk(item):
    link = turkish_lower(item.get("link", ""))
    title = turkish_lower(item.get("title", ""))
    summary = turkish_lower(item.get("summary", ""))
    for pattern in JUNK_LINK_PATTERNS:
        if pattern in link:
            return True
    for pattern in JUNK_TEXT_PATTERNS:
        if pattern in title or pattern in summary:
            return True
    return False


def make_id(link):
    """Stable ID derived from the article link (not a sequential index) -
    this lets us add new items later without ID collisions."""
    return hashlib.md5(link.encode("utf-8")).hexdigest()


def load_news():
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def prepare_batch(news_items):
    """Turns raw news dicts into (ids, texts, metadatas), skipping junk/empty ones."""
    ids, texts, metadatas = [], [], []
    for item in news_items:
        if is_junk(item):
            continue
        title = clean_text(item["title"])
        summary = clean_text(item["summary"])
        text = f"{title}. {summary}"
        if len(text) < 15:
            continue
        ids.append(make_id(item["link"]))
        texts.append(text)
        metadatas.append({
            "source": item["source"],
            "title": title,
            "link": item["link"],
            "published": item["published"],
            "published_ts": item.get("published_ts", 0),  # epoch int, 0 = unknown/very old
        })
    return ids, texts, metadatas


def get_or_create_collection(client):
    try:
        return client.get_collection(COLLECTION_NAME)
    except Exception:
        return client.create_collection(
            COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
        )


def build_index_full():
    """Full rebuild from scratch - use this after changing the junk filters
    or the embedding model, so old entries get re-evaluated too."""
    news = load_news()
    print(f"Total news (before filter): {len(news)}")

    client = chromadb.PersistentClient(path=CHROMA_DIR)
    try:
        client.delete_collection(COLLECTION_NAME)
    except Exception:
        pass
    collection = client.create_collection(
        COLLECTION_NAME,
        metadata={"hnsw:space": "cosine"},
    )

    ids, texts, metadatas = prepare_batch(news)
    print(f"Total news (after filter): {len(ids)}")

    model = _get_model()
    print("Calculating embeddings...")
    embeddings = model.encode(texts, show_progress_bar=True).tolist()

    collection.add(ids=ids, embeddings=embeddings, documents=texts, metadatas=metadatas)
    print(f"Number of news added to Vector DB: {len(ids)}")
    print(f"Save location: {CHROMA_DIR}")


def add_items_to_index(new_items):
    """Incremental update - embeds and upserts only the given (new) items,
    without touching or re-embedding what's already in the collection.
    Returns the number of items actually added."""
    if not new_items:
        return 0

    client = chromadb.PersistentClient(path=CHROMA_DIR)
    collection = get_or_create_collection(client)

    ids, texts, metadatas = prepare_batch(new_items)
    if not ids:
        return 0

    model = _get_model()
    embeddings = model.encode(texts, show_progress_bar=False).tolist()

    # upsert = insert if new, overwrite if the same id already exists (safe to call repeatedly)
    collection.upsert(ids=ids, embeddings=embeddings, documents=texts, metadatas=metadatas)
    return len(ids)


if __name__ == "__main__":
    build_index_full()