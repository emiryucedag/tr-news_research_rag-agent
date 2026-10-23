"""Reading the text of one news article, only so that a summary can be written from what the article
says instead of from a feed teaser. The text is never stored: the summary job keeps only the
summaries it generates, and the link goes back to the publisher.

A page is fetched only when the site's robots.txt allows it (see robots.py) and the feed is not
marked as one whose terms restrict this use. Standard library only: no extraction package.

Extraction order:
  1. the `articleBody` of a JSON-LD NewsArticle (most Turkish and English outlets publish it)
  2. paragraphs inside <article>
  3. long paragraphs anywhere on the page
"""
import html
import json
import re
from html.parser import HTMLParser

import httpx

MIN_CHARS = 300           # less than this is a teaser, not an article
MAX_CHARS = 6000          # more than a summary needs
SKIP_TAGS = {"script", "style", "noscript", "nav", "header", "footer", "aside", "form", "figure", "button"}
JSON_LD = re.compile(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.S | re.I)


class _Paragraphs(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip = 0
        self.in_article = 0
        self.in_p = False
        self.buf = []
        self.in_article_paragraphs, self.other_paragraphs = [], []

    def handle_starttag(self, tag, attrs):
        if tag in SKIP_TAGS:
            self.skip += 1
        elif tag == "article":
            self.in_article += 1
        elif tag == "p" and not self.skip:
            self.in_p, self.buf = True, []

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS:
            self.skip = max(0, self.skip - 1)
        elif tag == "article":
            self.in_article = max(0, self.in_article - 1)
        elif tag == "p" and self.in_p:
            self.in_p = False
            text = re.sub(r"\s+", " ", "".join(self.buf)).strip()
            if len(text) >= 60:
                (self.in_article_paragraphs if self.in_article else self.other_paragraphs).append(text)

    def handle_data(self, data):
        if self.in_p and not self.skip:
            self.buf.append(data)


def _json_ld_body(page):
    for raw in JSON_LD.findall(page):
        try:
            data = json.loads(raw.strip())
        except ValueError:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                body = node.get("articleBody")
                if isinstance(body, str) and len(body) >= MIN_CHARS:
                    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", body))).strip()
                stack.extend(v for v in node.values() if isinstance(v, (list, dict)))
    return ""


def extract(page):
    """The article text found in an HTML page, or an empty string."""
    body = _json_ld_body(page)
    if body:
        return body[:MAX_CHARS]
    parser = _Paragraphs()
    try:
        parser.feed(page)
    except Exception:
        return ""
    paragraphs = parser.in_article_paragraphs or parser.other_paragraphs
    text = " ".join(paragraphs)
    return text[:MAX_CHARS] if len(text) >= MIN_CHARS else ""


def fetch_text(url, gate, user_agent):
    """(text, reason). Text is empty when the page may not be fetched or holds no article."""
    allowed, reason = gate.check(url)
    if not allowed:
        return "", reason
    try:
        r = httpx.get(url, headers={"User-Agent": user_agent}, timeout=15, follow_redirects=True)
    except httpx.HTTPError as e:
        return "", f"fetch failed ({type(e).__name__})"
    if r.status_code != 200 or "html" not in r.headers.get("content-type", ""):
        return "", f"HTTP {r.status_code}"
    text = extract(r.text)
    return text, "" if text else "no article text found"
