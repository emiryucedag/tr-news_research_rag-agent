import feedparser

#some rss feed to test
FEEDS_TO_TEST = [
    "https://www.ntv.com.tr/gundem.rss",
    "https://www.ntv.com.tr/teknoloji.rss",
    "https://www.aa.com.tr/tr/rss/default?cat=guncel",
    "http://www.hurriyet.com.tr/rss/anasayfa",
    "https://www.sabah.com.tr/rss/gundem.xml",
    "https://www.cnnturk.com/feed/rss/all/news",
    "http://feeds.bbci.co.uk/turkce/rss.xml",
    "http://rss.dw.com/rdf/rss-tur-all",
]

print(f"{'STATUS':<8} {'NEWS COUNT':<14} {'TITLE':<40} URL")
print("-" * 110)

for url in FEEDS_TO_TEST:
    try:
        f = feedparser.parse(url)
        n = len(f.entries)
        title = f.feed.get("title", "??")[:38]
        status = "OK" if n > 0 else "EMPTY"
        print(f"{status:<8} {n:<14} {title:<40} {url}")
    except Exception as e:
        print(f"ERROR     {'-':<14} {str(e)[:38]:<40} {url}")