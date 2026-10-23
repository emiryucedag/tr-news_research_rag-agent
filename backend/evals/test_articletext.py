"""Offline tests for reading article text. Run from backend/: ../.venv/bin/python evals/test_articletext.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jobs"))
from articletext import extract

PARA = "Bu, bir haber metninin uzun ve anlamlı bir paragrafıdır ve altmış karakterden fazladır. "


def test_json_ld_body_wins():
    body = "Gövde cümlesi. " * 30
    page = f'<html><script type="application/ld+json">{{"@type":"NewsArticle","articleBody":"{body}"}}</script><p>{PARA * 5}</p></html>'
    assert extract(page).startswith("Gövde cümlesi.")


def test_article_paragraphs_preferred_over_page_paragraphs():
    page = f"<body><p>{PARA * 6}</p><article><p>{'Makale paragrafı, yeterince uzun bir cümle olarak yazıldı. ' * 8}</p></article></body>"
    assert extract(page).startswith("Makale paragrafı")


def test_navigation_footer_and_scripts_are_ignored():
    page = (f"<nav><p>{PARA * 6}</p></nav><footer><p>{PARA * 6}</p></footer><script>var x='<p>{PARA}</p>';</script>"
            f"<article><p>{'Gerçek haber metni burada, tam ve uzun bir paragraf olarak duruyor. ' * 8}</p></article>")
    text = extract(page)
    assert text.startswith("Gerçek haber metni") and "bir haber metninin" not in text


def test_short_pages_are_not_articles():
    assert extract(f"<article><p>{PARA}</p></article>") == ""


def test_entities_are_decoded_and_length_capped():
    page = "<article><p>" + ("Fiyat &amp; talep &#8217; üzerine uzun bir paragraf cümlesi yazıldı. " * 200) + "</p></article>"
    text = extract(page)
    assert "&amp;" not in text and "Fiyat & talep ’" in text and len(text) <= 6000


for name, fn in list(globals().items()):
    if name.startswith("test_"):
        fn()
print("all article text tests passed")
