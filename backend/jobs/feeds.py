"""Feed list for the hourly ingestion job.

Each feed belongs to exactly one category; the category is stored on every
article so the web app never has to guess it from the source name.

Fields
  name           feed name, stored in articles.feed
  source         outlet name shown to users, stored in articles.source
  url            RSS/Atom address
  category       one of CATEGORIES
  language       "tr" or "en"
  tz_fix_hours   added to the parsed publish time. cnnturk.com labels local
                 Istanbul time (+03:00) as GMT, so its times are 3 hours ahead.
  terms          "explicit"   the publisher offers the feed for use in apps
                 "unverified" terms not read or no feed clause; show publicly
                              only after the owner has checked them
"""

CATEGORIES = [
    "gundem", "siyaset", "spor", "ekonomi",
    "teknoloji", "dunya", "yapay-zeka", "populer-kultur",
]


def _feed(name, source, url, category, language="tr", tz_fix_hours=0, terms="unverified"):
    return {
        "name": name, "source": source, "url": url, "category": category,
        "language": language, "tz_fix_hours": tz_fix_hours, "terms": terms,
    }


FEEDS = [
    # gundem
    _feed("NTV Gundem", "NTV", "https://www.ntv.com.tr/gundem.rss", "gundem"),
    _feed("AA", "Anadolu Ajansi", "https://www.aa.com.tr/tr/rss/default?cat=guncel", "gundem"),
    _feed("Hurriyet", "Hurriyet", "https://www.hurriyet.com.tr/rss/anasayfa", "gundem"),
    _feed("Sabah Gundem", "Sabah", "https://www.sabah.com.tr/rss/gundem.xml", "gundem"),
    _feed("CNN Turk", "CNN Turk", "https://www.cnnturk.com/feed/rss/all/news", "gundem", tz_fix_hours=-3),

    # siyaset
    _feed("TRT Haber", "TRT Haber", "https://www.trthaber.com/sondakika.rss", "siyaset"),
    _feed("Sozcu", "Sozcu", "https://www.sozcu.com.tr/feeds-rss-category-gundem", "siyaset"),
    _feed("Cumhuriyet", "Cumhuriyet", "https://www.cumhuriyet.com.tr/rss/son_dakika.xml", "siyaset"),
    _feed("Hurriyet Siyaset", "Hurriyet", "https://www.hurriyet.com.tr/rss/siyaset", "siyaset"),
    _feed("CNN Turk Turkiye", "CNN Turk", "https://www.cnnturk.com/feed/rss/turkiye/news", "siyaset", tz_fix_hours=-3),

    # spor
    _feed("Hurriyet Spor", "Hurriyet", "https://www.hurriyet.com.tr/rss/spor", "spor"),
    _feed("Sabah Spor", "Sabah", "https://www.sabah.com.tr/rss/spor.xml", "spor"),
    _feed("CNN Turk Spor", "CNN Turk", "https://www.cnnturk.com/feed/rss/spor/news", "spor", tz_fix_hours=-3),

    # ekonomi
    _feed("CNN Turk Ekonomi", "CNN Turk", "https://www.cnnturk.com/feed/rss/ekonomi/news", "ekonomi", tz_fix_hours=-3),
    _feed("Hurriyet Ekonomi", "Hurriyet", "https://www.hurriyet.com.tr/rss/ekonomi", "ekonomi"),
    _feed("Sabah Ekonomi", "Sabah", "https://www.sabah.com.tr/rss/ekonomi.xml", "ekonomi"),
    _feed("Bloomberg HT", "Bloomberg HT", "https://www.bloomberght.com/rss", "ekonomi"),

    # teknoloji
    _feed("NTV Teknoloji", "NTV", "https://www.ntv.com.tr/teknoloji.rss", "teknoloji"),
    _feed("Webtekno", "Webtekno", "https://www.webtekno.com/rss.xml", "teknoloji"),
    _feed("ShiftDelete", "ShiftDelete.Net", "https://shiftdelete.net/feed", "teknoloji"),
    _feed("DonanimHaber", "DonanimHaber", "https://www.donanimhaber.com/rss/tum/", "teknoloji"),
    _feed("TechCrunch", "TechCrunch", "https://techcrunch.com/feed/", "teknoloji", "en", terms="explicit"),
    _feed("The Verge", "The Verge", "https://www.theverge.com/rss/index.xml", "teknoloji", "en"),
    _feed("Ars Technica", "Ars Technica", "https://feeds.arstechnica.com/arstechnica/index", "teknoloji", "en"),

    # dunya
    _feed("BBC Turkce", "BBC Turkce", "https://feeds.bbci.co.uk/turkce/rss.xml", "dunya"),
    _feed("DW Turkce", "DW Turkce", "http://rss.dw.com/rdf/rss-tur-all", "dunya"),
    _feed("NTV Dunya", "NTV", "https://www.ntv.com.tr/dunya.rss", "dunya"),
    _feed("CNN Turk Dunya", "CNN Turk", "https://www.cnnturk.com/feed/rss/dunya/news", "dunya", tz_fix_hours=-3),
    _feed("BBC World", "BBC News", "http://feeds.bbci.co.uk/news/world/rss.xml", "dunya", "en"),
    _feed("Guardian World", "The Guardian", "https://www.theguardian.com/world/rss", "dunya", "en"),
    _feed("NPR World", "NPR", "https://feeds.npr.org/1004/rss.xml", "dunya", "en"),

    # yapay-zeka
    _feed("TechCrunch AI", "TechCrunch", "https://techcrunch.com/category/artificial-intelligence/feed/", "yapay-zeka", "en", terms="explicit"),
    _feed("Verge AI", "The Verge", "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml", "yapay-zeka", "en"),
    _feed("MIT Tech Review AI", "MIT Technology Review", "https://www.technologyreview.com/topic/artificial-intelligence/feed", "yapay-zeka", "en"),
    _feed("OpenAI News", "OpenAI", "https://openai.com/news/rss.xml", "yapay-zeka", "en"),
    _feed("Google AI Blog", "Google", "https://blog.google/technology/ai/rss/", "yapay-zeka", "en"),

    # populer-kultur
    _feed("Variety", "Variety", "https://variety.com/feed/", "populer-kultur", "en"),
    _feed("Hollywood Reporter", "The Hollywood Reporter", "https://www.hollywoodreporter.com/feed/", "populer-kultur", "en"),
    _feed("Deadline", "Deadline", "https://deadline.com/feed/", "populer-kultur", "en"),
    _feed("Collider", "Collider", "https://collider.com/feed/", "populer-kultur", "en"),
    _feed("Screen Rant", "Screen Rant", "https://screenrant.com/feed/", "populer-kultur", "en"),
]

# Checked on 2026-10-22 and left out on purpose:
#   NTV Spor (no entries), TRT Spor (404), Sabah Siyaset (no entries), T24 (no response),
#   NTV Ekonomi (dead), VentureBeat (429), IGN (501), Hugging Face blog (no summaries),
#   Al Jazeera (terms forbid automated access).
