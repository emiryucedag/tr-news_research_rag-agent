import time
import chromadb
from sentence_transformers import SentenceTransformer

CHROMA_DIR = "rag/chroma_db"
COLLECTION_NAME = "news"
MODEL_NAME = "paraphrase-multilingual-MiniLM-L12-v2"

RECENCY_WINDOWS_HOURS = [72, 168, 720, None]
MIN_ACCEPTABLE_RESULTS = 3

# Keyword -> dedicated source feeds. When the query mentions one of these
# keywords, we don't just hope semantic similarity surfaces the right
# articles - we directly pull the most recent ones from these known-category
# sources via a metadata filter. This is deterministic (code-guaranteed),
# not dependent on the model's judgment or on embedding similarity quirks
# (e.g. a match preview may not contain the literal word "spor" at all).
CATEGORY_SOURCES = {
    "spor": ["Hurriyet Spor", "Sabah Spor", "CNN Turk Spor"],
    "futbol": ["Hurriyet Spor", "Sabah Spor", "CNN Turk Spor"],
    "basketbol": ["Hurriyet Spor", "Sabah Spor", "CNN Turk Spor"],
    "maç": ["Hurriyet Spor", "Sabah Spor", "CNN Turk Spor"],
    "teknoloji": ["NTV Teknoloji"],
    "yapay zeka": ["NTV Teknoloji"],
}

_model = None
_collection = None


def _get_model():
    global _model
    if _model is None:
        _model = SentenceTransformer(MODEL_NAME)
    return _model


def _get_collection():
    global _collection
    if _collection is None:
        client = chromadb.PersistentClient(path=CHROMA_DIR)
        _collection = client.get_collection(COLLECTION_NAME)
    return _collection


def _query_with_cutoff(query_embedding, n_results, cutoff_ts):
    collection = _get_collection()
    where = {"published_ts": {"$gte": cutoff_ts}} if cutoff_ts is not None else None
    return collection.query(
        query_embeddings=query_embedding,
        n_results=n_results,
        where=where,
    )


def _semantic_search(query, n_results):
    model = _get_model()
    now_ts = int(time.time())
    query_embedding = model.encode([query]).tolist()

    for window_hours in RECENCY_WINDOWS_HOURS:
        cutoff_ts = now_ts - window_hours * 3600 if window_hours is not None else None
        candidate = _query_with_cutoff(query_embedding, n_results, cutoff_ts)
        if len(candidate["ids"][0]) >= MIN_ACCEPTABLE_RESULTS or window_hours is None:
            return candidate, window_hours
    return candidate, None


def _get_recent_by_sources(sources, limit=20, max_age_hours=72):
    """Deterministic pull: the most recent articles from specific known-category
    sources, regardless of how well they match the query text semantically."""
    collection = _get_collection()
    now_ts = int(time.time())
    cutoff_ts = now_ts - max_age_hours * 3600

    result = collection.get(
        where={
            "$and": [
                {"source": {"$in": sources}},
                {"published_ts": {"$gte": cutoff_ts}},
            ]
        },
    )

    rows = list(zip(result["ids"], result["metadatas"], result["documents"]))
    rows.sort(key=lambda r: r[1].get("published_ts", 0), reverse=True)
    return rows[:limit]


def search_news(query: str, n_results: int = 15) -> list[dict]:
    """
    Combines two retrieval strategies:
    1. Semantic search (recency-biased) - good for specific/niche queries.
    2. Deterministic category pull - if the query matches a known category
       keyword (sports, tech...), guarantees recent articles from that
       category's dedicated feeds are included, even if their wording
       doesn't closely match the query semantically.
    Results are merged and deduped by link.
    """
    items_by_link = {}

    query_lower = query.lower()
    matched_sources = set()
    for keyword, sources in CATEGORY_SOURCES.items():
        if keyword in query_lower:
            matched_sources.update(sources)

    if matched_sources:
        category_rows = _get_recent_by_sources(list(matched_sources), limit=n_results)
        for _id, meta, doc in category_rows:
            items_by_link[meta["link"]] = {
                "source": meta["source"],
                "title": meta["title"],
                "link": meta["link"],
                "published": meta["published"],
                "published_ts": meta.get("published_ts", 0),
                "text": doc,
                "distance": None,  # not applicable - this came from a direct category pull
            }
        print(f"[retrieval] category match on sources={sorted(matched_sources)} -> {len(category_rows)} result(s)")

    semantic_results, used_window = _semantic_search(query, n_results)
    for i in range(len(semantic_results["ids"][0])):
        meta = semantic_results["metadatas"][0][i]
        if meta["link"] not in items_by_link:  # category pull takes priority if duplicate
            items_by_link[meta["link"]] = {
                "source": meta["source"],
                "title": meta["title"],
                "link": meta["link"],
                "published": meta["published"],
                "published_ts": meta.get("published_ts", 0),
                "text": semantic_results["documents"][0][i],
                "distance": semantic_results["distances"][0][i],
            }

    window_label = f"last {used_window}h" if used_window is not None else "no limit"
    items = list(items_by_link.values())
    print(f"[retrieval] query='{query}' semantic_window={window_label} -> combined total {len(items)} result(s)")
    return items