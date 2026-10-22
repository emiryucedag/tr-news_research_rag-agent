"""Spike S2: embed a question and search with match_articles.

Run from backend/:
    ../.venv/bin/python spike/query_articles.py "Galatasaray son durum"
"""
import sys

import httpx

from embed_articles import EMBED_DIMS, EMBED_MODEL, GEMINI_URL, ENV_FILE, load_env


def embed_query(client, key, text):
    body = {
        "content": {"parts": [{"text": text}]},
        "taskType": "RETRIEVAL_QUERY",
        "outputDimensionality": EMBED_DIMS,
    }
    r = client.post(f"{GEMINI_URL}/{EMBED_MODEL}:embedContent",
                    headers={"x-goog-api-key": key}, json=body, timeout=60)
    if r.status_code != 200:
        raise SystemExit(f"Gemini HTTP {r.status_code}: {r.json().get('error', {}).get('message', '')[:200]}")
    return r.json()["embedding"]["values"]


def main():
    question = " ".join(sys.argv[1:]) or "spor haberleri"
    env = load_env(ENV_FILE)
    url, secret = env["SUPABASE_URL"].rstrip("/"), env["SUPABASE_SECRET_KEY"]

    with httpx.Client() as client:
        vec = embed_query(client, env["GEMINI_API_KEY"], question)
        r = client.post(
            f"{url}/rest/v1/rpc/match_articles",
            headers={"apikey": secret, "Content-Type": "application/json"},
            json={"query_embedding": "[" + ",".join(f"{x:.6f}" for x in vec) + "]", "match_count": 5},
            timeout=60)
    if r.status_code != 200:
        raise SystemExit(f"Supabase HTTP {r.status_code}: {r.text[:300]}")

    print(f"Question: {question}")
    for row in r.json():
        print(f"  {row['similarity']:.3f}  [{row['source']}] {row['title'][:90]}")


if __name__ == "__main__":
    main()
