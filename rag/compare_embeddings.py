# =============================================================================
# EMBEDDING MODEL COMPARISON — paraphrase-multilingual-MiniLM-L12-v2 vs all-MiniLM-L6-v2
# =============================================================================
#
# Same corpus (Turkish news), same query ("teknoloji ile ilgili haberler" / "spor ile
# ilgili haberler"), same cosine similarity search — only the embedding model changed.
#
# RESULTS:
#   Multilingual model : similarity scores 0.32-0.41, 4/5 results genuinely on-topic
#   English-only model : similarity scores 0.17-0.25, top result was a false positive
#
# KEY FINDING:
#   The English-only model was never trained on Turkish, so it can't capture real
#   semantic meaning in Turkish text. Its "hits" were mostly coincidental token/
#   substring overlaps rather than true understanding — e.g. it ranked an Audi
#   "Sportback" review as the #1 result for a query about "spor" (sports), because
#   "Sportback" and "spor" share surface characters, not because the model understood
#   the query. Where it did return genuinely relevant results, they were driven by
#   language-agnostic proper nouns (UEFA, Manchester City) already in Latin script,
#   not by comprehension of the surrounding Turkish sentence.
#
#   The multilingual model, trained across 50+ languages, produced both higher
#   similarity scores AND a higher proportion of genuinely relevant results —
#   confirming that embedding model choice has a direct, measurable impact on
#   retrieval quality, especially for non-English corpora.
#
# TAKEAWAY:
#   For any RAG system working with non-English text, using a model with explicit
#   multilingual training is not optional — it's a hard requirement, not just a
#   nice-to-have. This was verified empirically here, not just assumed.
# =============================================================================



import json
import re
import html
import numpy as np
from sentence_transformers import SentenceTransformer

DATA_FILE = "ingestion/news_data.json"

# 2 models to compare: one multilingual (supports Turkish), one English-only
MODEL_A_NAME = "paraphrase-multilingual-MiniLM-L12-v2"  
MODEL_B_NAME = "all-MiniLM-L6-v2"                        

JUNK_LINK_PATTERNS = ["whatsapp.com", "wa.me", "/resmi-ilanlar/"]
JUNK_TEXT_PATTERNS = ["abone olmak için tıklayın", "başkanlığı."]


def clean_text(text):
    text = html.unescape(text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def is_junk(link, title, summary):
    link, title, summary = link.lower(), title.lower(), summary.lower()
    for p in JUNK_LINK_PATTERNS:
        if p in link:
            return True
    for p in JUNK_TEXT_PATTERNS:
        if p in title or p in summary:
            return True
    return False


def load_corpus():
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        news = json.load(f)

    texts, metas = [], []
    for item in news:
        if is_junk(item["link"], item["title"], item["summary"]):
            continue
        title = clean_text(item["title"])
        summary = clean_text(item["summary"])
        text = f"{title}. {summary}"
        if len(text) < 15:
            continue
        texts.append(text)
        metas.append({"source": item["source"], "title": title, "link": item["link"]})
    return texts, metas


def cosine_top_k(query_emb, corpus_embs, k=5):
    query_norm = query_emb / np.linalg.norm(query_emb)
    # basic brute-force cosine similarity (with numpy) -> (no need Chrome, just for comparison)
    corpus_norm = corpus_embs / np.linalg.norm(corpus_embs, axis=1, keepdims=True)
    similarities = corpus_norm @ query_norm
    top_indices = np.argsort(similarities)[::-1][:k]
    return [(i, similarities[i]) for i in top_indices]


def compare(query, k=5):
    texts, metas = load_corpus()
    print(f"Corpus size: {len(texts)} news\n")

    for model_name in [MODEL_A_NAME, MODEL_B_NAME]:
        print(f"{'='*70}")
        print(f"MODEL: {model_name}")
        print(f"{'='*70}")

        model = SentenceTransformer(model_name)
        corpus_embs = model.encode(texts, show_progress_bar=False)
        query_emb = model.encode([query])[0]

        results = cosine_top_k(query_emb, corpus_embs, k=k)
        for rank, (idx, sim) in enumerate(results, 1):
            print(f"[{rank}] (similarity: {sim:.4f}) Source: {metas[idx]['source']}")
            print(f"    {texts[idx][:120]}")
        print()


if __name__ == "__main__":
    compare("sports related news")