"""Read-only check of every feed's site before it is trusted: does robots.txt allow our bot,
which AI crawlers does the site block entirely, and which comments in robots.txt talk about AI,
scraping or permitted use? Run it when adding a feed, and read what it prints: the comments
are the publisher's own words and can forbid what robots.txt rules cannot express.

Run from backend/:
    ../.venv/bin/python jobs/check_publishers.py
"""
import re
import sys
import urllib.parse
import urllib.robotparser
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from feeds import FEEDS
from robots import AGENT

UA = "Mozilla/5.0 (compatible; news-agent/0.1; hobby project)"
AI_BOTS = ["GPTBot", "ClaudeBot", "anthropic-ai", "Google-Extended", "CCBot", "PerplexityBot", "Bytespider", "Applebot-Extended"]
KEYWORDS = re.compile(r"\b(AI|LLMs?|large language|machine learning|artificial intelligence|text and data mining|TDM|scrap\w*|not permitted|prohibited)\b", re.I)


def check(host, feeds):
    row = {"host": host, "status": None, "blocked": [], "ai_bots": [], "notes": []}
    try:
        r = httpx.get(f"https://{host}/robots.txt", headers={"User-Agent": UA}, timeout=15, follow_redirects=True)
    except Exception as e:
        row["status"] = f"unreachable ({str(e)[:30]})"
        return row
    row["status"] = r.status_code
    if r.status_code != 200:
        return row
    parser = urllib.robotparser.RobotFileParser()
    parser.parse(r.text.splitlines())
    row["blocked"] = [f["name"] for f in feeds if not parser.can_fetch(AGENT, f["url"])]
    for bot in AI_BOTS:
        if re.search(rf"user-agent:\s*{re.escape(bot)}\s*\n(?:[^\n]*\n)*?\s*disallow:\s*/\s*$", r.text, re.I | re.M):
            row["ai_bots"].append(bot)
    row["notes"] = [ln.strip()[:120] for ln in r.text.splitlines() if ln.strip().startswith("#") and KEYWORDS.search(ln)]
    return row


def main():
    by_host = {}
    for f in FEEDS:
        by_host.setdefault(urllib.parse.urlparse(f["url"]).netloc, []).append(f)
    with ThreadPoolExecutor(8) as pool:
        rows = list(pool.map(lambda h: check(h, by_host[h]), sorted(by_host)))

    print(f"{'host':30}{'robots':>9}  {'feeds our bot may NOT fetch':30} AI bots blocked entirely")
    for r in rows:
        print(f"{r['host']:30}{str(r['status']):>9}  {', '.join(r['blocked']) or '-':30} {', '.join(r['ai_bots']) or '-'}")
    print("\nNotes in robots.txt that mention AI, scraping or permitted use (read these):")
    for r in rows:
        for note in r["notes"][:4]:
            print(f"  [{r['host']}] {note}")


if __name__ == "__main__":
    main()
