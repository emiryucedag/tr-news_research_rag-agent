"""Offline tests for jobs/robots.py (the download is replaced by a fake).

Run from backend/:
    ../.venv/bin/python evals/test_robots.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jobs"))
from robots import RobotsGate  # noqa: E402

failures = []


def expect(name, got, want):
    if got != want:
        failures.append(f"{name}: got {got}, want {want}")


SITES = {
    "https://rules.test": (200, "User-agent: *\nDisallow: /private/\n\nUser-agent: news-agent\nDisallow: /feeds/blocked.xml\n"),
    "https://open.test": (200, "User-agent: *\nDisallow:\n"),
    "https://all-blocked.test": (200, "User-agent: *\nDisallow: /\n"),
    "https://other-bot.test": (200, "User-agent: GPTBot\nDisallow: /\n"),
    "https://missing.test": (404, "not found"),
    "https://forbidden.test": (403, "forbidden"),
    "https://broken.test": (503, "unavailable"),
    "https://down.test": (None, "connection refused"),
}
calls = []


def fake_download(origin, user_agent):
    calls.append(origin)
    return SITES[origin]


gate = RobotsGate("test-agent", download=fake_download)

expect("our agent's own rule blocks its path", gate.check("https://rules.test/feeds/blocked.xml")[0], False)
expect("a path our agent's rule does not mention is allowed", gate.check("https://rules.test/feeds/ok.xml")[0], True)
expect("open site", gate.check("https://open.test/rss")[0], True)
expect("disallow everything", gate.check("https://all-blocked.test/rss")[0], False)
expect("a rule for another bot does not bind us", gate.check("https://other-bot.test/rss")[0], True)
expect("missing robots.txt allows", gate.check("https://missing.test/rss")[0], True)
expect("403 on robots.txt counts as missing", gate.check("https://forbidden.test/rss")[0], True)
allowed, reason = gate.check("https://broken.test/rss")
expect("server error skips the feed", allowed, False)
expect("server error says why", "HTTP 503" in reason, True)
allowed, reason = gate.check("https://down.test/rss")
expect("unreachable skips the feed", allowed, False)
expect("unreachable says why", "unreachable" in reason, True)

before = len(calls)
gate.check("https://rules.test/another.xml")
gate.check("https://open.test/another.xml")
expect("robots.txt is downloaded once per site", len(calls) - before, 0)

if failures:
    print(f"{len(failures)} FAILED")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("all robots.txt tests passed")
