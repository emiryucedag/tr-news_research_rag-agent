"""robots.txt gate: before a feed is fetched, check that the site allows our bot to fetch it.

Rules follow RFC 9309:
  - robots.txt found           obey its rules for our agent
  - robots.txt missing (4xx)   no restrictions
  - robots.txt unreachable     (server error or network failure) do not fetch this run

robots.txt is a convention, not a lock, but honouring it is the baseline of good behaviour
for an automated client. It says nothing about copyright or terms of use; for those see
jobs/check_publishers.py, which also reads the notes publishers leave in robots.txt.
"""
import threading
import urllib.parse
import urllib.robotparser

import httpx

AGENT = "news-agent"  # the token sites see in our User-Agent string


def _download(origin, user_agent):
    """Returns (status code, text); (None, reason) when the site could not be reached."""
    try:
        r = httpx.get(f"{origin}/robots.txt", headers={"User-Agent": user_agent}, timeout=10, follow_redirects=True)
        return r.status_code, r.text
    except Exception as e:
        return None, str(e)[:60]


class RobotsGate:
    def __init__(self, user_agent, download=_download):
        self.user_agent = user_agent
        self._download = download
        self._rules = {}  # origin -> RobotFileParser, or an error string, or None (no restrictions)
        self._lock = threading.Lock()

    def _load(self, origin):
        status, text = self._download(origin, self.user_agent)
        if status is None:
            return f"robots.txt unreachable ({text})"
        if status == 200:
            parser = urllib.robotparser.RobotFileParser()
            parser.parse(text.splitlines())
            return parser
        if 400 <= status < 500:
            return None  # no robots.txt, nothing is restricted
        return f"robots.txt returned HTTP {status}"

    def check(self, url):
        """Returns (allowed, reason); reason is empty when allowed."""
        parts = urllib.parse.urlparse(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        with self._lock:  # one download per site, even when feeds are fetched in parallel
            if origin not in self._rules:
                self._rules[origin] = self._load(origin)
            rules = self._rules[origin]
        if rules is None:
            return True, ""
        if isinstance(rules, str):
            return False, rules
        if rules.can_fetch(AGENT, url):
            return True, ""
        return False, "disallowed by robots.txt"
