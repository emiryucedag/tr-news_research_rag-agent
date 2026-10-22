"""Spike: check which features the candidate models support.

Generation: system instruction and JSON output with a schema. If a level
fails, a simpler one is tried (JSON mode only, then a plain prompt).
Embedding: output dimensionality 768.
About one request per model; more only when a fallback is tried.

Run from backend/:
    ../.venv/bin/python spike/test_features.py
"""
import json
import time
import urllib.error
import urllib.request

from list_models import BASE_URL, read_key

GENERATION_MODELS = [
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemma-4-31b-it",
    "gemma-4-26b-a4b-it",
]
EMBEDDING_MODELS = ["gemini-embedding-001", "gemini-embedding-2"]
DIMENSIONS = 768

ARTICLES = [
    {"id": "a1", "title": "Merkez Bankası politika faizini sabit tuttu",
     "summary": "Para Politikası Kurulu politika faizini yüzde 35 seviyesinde sabit bıraktı."},
    {"id": "a2", "title": "Piyasalar faiz kararının ardından yükseldi",
     "summary": "Borsa İstanbul, Merkez Bankası kararının açıklanmasının ardından günü yüzde 1,2 artışla kapattı."},
    {"id": "a3", "title": "Yeni sezonda bilet fiyatları açıklandı",
     "summary": "Kulüp, yeni sezon kombine bilet fiyatlarını açıkladı."},
]
QUESTION = "Merkez Bankası'nın son faiz kararı ne oldu ve piyasalar nasıl tepki verdi?"

SYSTEM = (
    "You answer questions using only the articles given. Return a JSON array. "
    "Each item has the id of one article you use and one sentence in Turkish "
    "that says what that article reports about the question. Skip articles that "
    "are not relevant. Return [] if no article is relevant."
)
SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {"id": {"type": "STRING"}, "sentence": {"type": "STRING"}},
        "required": ["id", "sentence"],
    },
}


def post(key, model, method, body):
    req = urllib.request.Request(
        f"{BASE_URL}/{model}:{method}",
        data=json.dumps(body).encode("utf-8"),
        headers={"x-goog-api-key": key, "Content-Type": "application/json"},
    )
    start = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.load(resp), None, time.monotonic() - start
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            message = json.loads(body)["error"]["message"]
        except (ValueError, KeyError, TypeError):
            message = body[:300]
        return None, f"HTTP {e.code}: {message}", time.monotonic() - start


def user_text():
    lines = [f"[{a['id']}] {a['title']}. {a['summary']}" for a in ARTICLES]
    return "Articles:\n" + "\n".join(lines) + f"\n\nQuestion: {QUESTION}"


def generation_levels():
    contents = [{"role": "user", "parts": [{"text": user_text()}]}]
    system = {"parts": [{"text": SYSTEM}]}
    yield "system + schema", {
        "systemInstruction": system,
        "contents": contents,
        "generationConfig": {"temperature": 0, "responseMimeType": "application/json",
                             "responseSchema": SCHEMA},
    }
    yield "system + json mode", {
        "systemInstruction": system,
        "contents": contents,
        "generationConfig": {"temperature": 0, "responseMimeType": "application/json"},
    }
    yield "plain prompt", {
        "contents": [{"role": "user", "parts": [{"text": SYSTEM + "\n\n" + user_text()}]}],
        "generationConfig": {"temperature": 0},
    }


def response_text(data):
    parts = data["candidates"][0]["content"]["parts"]
    return "".join(p.get("text", "") for p in parts if not p.get("thought"))


def parse_findings(text):
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    findings = json.loads(text)
    known = {a["id"] for a in ARTICLES}
    if not isinstance(findings, list) or not all(
        isinstance(f, dict) and f.get("id") in known and isinstance(f.get("sentence"), str)
        for f in findings
    ):
        raise ValueError("output does not match [{id, sentence}]")
    return findings


def test_generation(key, model):
    print(f"== {model}")
    for level, body in generation_levels():
        data, error, seconds = post(key, model, "generateContent", body)
        if error:
            print(f"  {level}: failed ({seconds:.1f} s) {error}")
            continue
        try:
            findings = parse_findings(response_text(data))
        except (KeyError, IndexError, ValueError) as e:
            print(f"  {level}: bad output ({seconds:.1f} s) {e}")
            continue
        usage = data.get("usageMetadata", {})
        print(f"  {level}: ok ({seconds:.1f} s)")
        print(f"  tokens: prompt {usage.get('promptTokenCount', '-')}, "
              f"output {usage.get('candidatesTokenCount', '-')}, "
              f"thinking {usage.get('thoughtsTokenCount', 0)}")
        print(f"  ids used: {[f['id'] for f in findings]} (expected a1, a2; not a3)")
        for f in findings:
            print(f"    [{f['id']}] {f['sentence']}")
        return
    print("  no level worked")


def test_embedding(key, model):
    print(f"== {model}")
    content = {"parts": [{"text": f"{ARTICLES[0]['title']}. {ARTICLES[0]['summary']}"}]}
    bodies = [
        ("with taskType", {"content": content, "taskType": "RETRIEVAL_DOCUMENT",
                           "outputDimensionality": DIMENSIONS}),
        ("without taskType", {"content": content, "outputDimensionality": DIMENSIONS}),
    ]
    for label, body in bodies:
        data, error, seconds = post(key, model, "embedContent", body)
        if error:
            print(f"  {label}: failed ({seconds:.1f} s) {error}")
            continue
        values = data.get("embedding", {}).get("values", [])
        status = "ok" if len(values) == DIMENSIONS else "wrong size"
        print(f"  {label}: {status}, {len(values)} dims ({seconds:.1f} s)")
        return


def main():
    key = read_key()
    for model in GENERATION_MODELS:
        test_generation(key, model)
    for model in EMBEDDING_MODELS:
        test_embedding(key, model)


if __name__ == "__main__":
    main()
