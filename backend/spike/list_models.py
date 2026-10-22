"""Spike: validate the Gemini key and list available models.

Reads GEMINI_API_KEY from .env and never prints it. The key is sent in a
header, not in the URL, so it does not end up in error messages or logs.

Run from backend/:
    ../.venv/bin/python spike/list_models.py
"""
import json
import urllib.error
import urllib.parse
import urllib.request

ENV_PATH = ".env"
BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"
KEYWORDS = ("flash-lite", "gemma", "embedding")


def read_key():
    with open(ENV_PATH, encoding="utf-8") as f:
        for line in f:
            name, sep, value = line.strip().partition("=")
            if sep and name.strip() == "GEMINI_API_KEY":
                return value.strip().strip('"').strip("'")
    raise SystemExit("GEMINI_API_KEY not found in .env")


def fetch_models(key):
    models = []
    page_token = ""
    while True:
        url = BASE_URL + "?pageSize=1000"
        if page_token:
            url += "&pageToken=" + urllib.parse.quote(page_token)
        req = urllib.request.Request(url, headers={"x-goog-api-key": key})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.load(resp)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            try:
                message = json.loads(body)["error"]["message"]
            except (ValueError, KeyError, TypeError):
                message = body[:300]
            raise SystemExit(f"Key check failed. HTTP {e.code}: {message}")
        models.extend(data.get("models", []))
        page_token = data.get("nextPageToken", "")
        if not page_token:
            return models


def main():
    models = fetch_models(read_key())
    print(f"Key works. {len(models)} models available.\n")

    matches = [m for m in models if any(k in m["name"] for k in KEYWORDS)]
    for m in matches:
        print(m["name"])
        print("  display name:", m.get("displayName", "-"))
        print("  methods:     ", ", ".join(m.get("supportedGenerationMethods", [])))
        print("  input tokens:", m.get("inputTokenLimit", "-"),
              "| output tokens:", m.get("outputTokenLimit", "-"))
        print()

    others = [m["name"] for m in models if m not in matches]
    print(f"{len(others)} other models (not shown in detail):")
    print("  " + ", ".join(others))


if __name__ == "__main__":
    main()
