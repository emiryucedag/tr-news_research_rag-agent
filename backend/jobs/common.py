"""Helpers shared by the scheduled jobs: config, text cleaning, Supabase REST, Gemini embeddings."""
import hashlib
import html
import json
import os
import re
from pathlib import Path

import httpx

RETENTION_DAYS = 3

EMBED_MODEL = "gemini-embedding-001"
EMBED_DIMS = 768
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models"

# Same junk patterns as the CLI version (rag/build_index.py).
JUNK_LINK_PATTERNS = ["whatsapp.com", "wa.me", "/resmi-ilanlar/", "/advertorial/", "/sponsorlu/", "/marka-icerik/"]
JUNK_TEXT_PATTERNS = ["abone olmak için tıklayın", "başkanlığı.", "canlı izle", "şifresiz izle", "canlı yayın izle"]


# --- config -----------------------------------------------------------------

_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
_file_env = None


def get_env(name):
    """Environment variable first (GitHub Actions), then backend/.env (local runs)."""
    global _file_env
    if name in os.environ:
        return os.environ[name]
    if _file_env is None:
        _file_env = {}
        if _ENV_FILE.exists():
            for line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
                if "=" in line and not line.lstrip().startswith("#"):
                    key, value = line.split("=", 1)
                    _file_env[key.strip()] = value.strip()
    if name not in _file_env:
        raise SystemExit(f"Missing setting {name} (set it as an environment variable or in backend/.env)")
    return _file_env[name]


# --- text -------------------------------------------------------------------

def clean_text(text):
    """Strip HTML entities and tags that some feeds leave in titles and summaries."""
    text = html.unescape(text or "")
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def turkish_lower(text):
    """str.lower() turns the dotted capital I into 'i' plus a combining dot, which
    breaks substring matching. Map the Turkish I pair by hand first."""
    return text.replace("İ", "i").replace("I", "ı").lower()


def is_junk(link, title, summary):
    link, title, summary = turkish_lower(link), turkish_lower(title), turkish_lower(summary)
    return (any(p in link for p in JUNK_LINK_PATTERNS)
            or any(p in title or p in summary for p in JUNK_TEXT_PATTERNS))


def make_id(link):
    return hashlib.md5(link.encode("utf-8")).hexdigest()


def embedding_text(row):
    return f"{row['title']}. {row.get('summary') or ''}"[:1000]


# --- Supabase REST ----------------------------------------------------------

class Supabase:
    """Tiny PostgREST client using the secret key (bypasses RLS; server side only)."""

    def __init__(self):
        self.url = get_env("SUPABASE_URL").rstrip("/") + "/rest/v1"
        self.headers = {"apikey": get_env("SUPABASE_SECRET_KEY"), "Content-Type": "application/json"}
        self.client = httpx.Client(timeout=60)

    def _check(self, r):
        if r.status_code >= 400:
            raise RuntimeError(f"Supabase HTTP {r.status_code}: {r.text[:300]}")
        return r

    def select(self, table, params):
        return self._check(self.client.get(f"{self.url}/{table}", headers=self.headers, params=params)).json()

    def select_all(self, table, params, page=1000):
        rows, offset = [], 0
        while True:
            chunk = self.select(table, {**params, "limit": page, "offset": offset})
            rows += chunk
            if len(chunk) < page:
                return rows
            offset += page

    def count(self, table, params):
        r = self._check(self.client.get(
            f"{self.url}/{table}", headers={**self.headers, "Prefer": "count=exact"},
            params={**params, "select": "id", "limit": 1}))
        return int(r.headers["content-range"].split("/")[1])

    def insert_ignore(self, table, rows, chunk=200, conflict="id"):
        for i in range(0, len(rows), chunk):
            self._check(self.client.post(
                f"{self.url}/{table}", params={"on_conflict": conflict},
                headers={**self.headers, "Prefer": "resolution=ignore-duplicates,return=minimal"},
                json=rows[i:i + chunk]))

    def upsert(self, table, rows, chunk=100, conflict="id"):
        for i in range(0, len(rows), chunk):
            self._check(self.client.post(
                f"{self.url}/{table}", params={"on_conflict": conflict},
                headers={**self.headers, "Prefer": "resolution=merge-duplicates,return=minimal"},
                json=rows[i:i + chunk]))

    def delete(self, table, params):
        r = self._check(self.client.delete(
            f"{self.url}/{table}", params=params,
            headers={**self.headers, "Prefer": "count=exact,return=minimal"}))
        return int(r.headers["content-range"].split("/")[1])


# --- Gemini embeddings ------------------------------------------------------

class EmbedQuotaError(Exception):
    pass


def embed_documents(texts):
    """One batchEmbedContents request. Every text counts as one request against the
    free-tier limits (100 per minute, about 1K per day), so callers must budget."""
    body = {"requests": [{
        "model": f"models/{EMBED_MODEL}",
        "content": {"parts": [{"text": t}]},
        "taskType": "RETRIEVAL_DOCUMENT",
        "outputDimensionality": EMBED_DIMS,
    } for t in texts]}
    r = httpx.post(f"{GEMINI_URL}/{EMBED_MODEL}:batchEmbedContents",
                   headers={"x-goog-api-key": get_env("GEMINI_API_KEY")}, json=body, timeout=120)
    if r.status_code == 429:
        raise EmbedQuotaError(r.json().get("error", {}).get("message", "")[:200])
    if r.status_code != 200:
        raise RuntimeError(f"Gemini HTTP {r.status_code}: {r.text[:200]}")
    return [e["values"] for e in r.json()["embeddings"]]


def vector_literal(vec):
    """pgvector text format."""
    return "[" + ",".join(f"{x:.6f}" for x in vec) + "]"


# --- Gemini generation ------------------------------------------------------

# Primary first, then a second model with its own free-tier quota.
GENERATION_MODELS = ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]


class GenerationError(Exception):
    pass


def generate_json(model, prompt, schema):
    """One generateContent request that must return JSON matching `schema`."""
    r = httpx.post(
        f"{GEMINI_URL}/{model}:generateContent",
        headers={"x-goog-api-key": get_env("GEMINI_API_KEY")},
        json={"contents": [{"parts": [{"text": prompt}]}],
              "generationConfig": {"responseMimeType": "application/json",
                                   "responseSchema": schema, "temperature": 0}},
        timeout=120)
    if r.status_code != 200:
        msg = r.json().get("error", {}).get("message", "")[:120] if r.headers.get("content-type", "").startswith("application/json") else ""
        raise GenerationError(f"{model} HTTP {r.status_code} {msg}")
    parts = r.json().get("candidates", [{}])[0].get("content", {}).get("parts", [])
    text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
    try:
        return json.loads(text)
    except ValueError:
        raise GenerationError(f"{model} returned invalid JSON")
