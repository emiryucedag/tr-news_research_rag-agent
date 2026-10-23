"""Offline tests for how jobs/summarize.py turns the model's picks into stored stories.

Run from backend/:
    ../.venv/bin/python evals/test_summarize.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jobs"))
from summarize import select_items, vague_headline  # noqa: E402

failures = []


def expect(name, got, want):
    if got != want:
        failures.append(f"{name}: got {got!r}, want {want!r}")


def candidate(i, title, summary, source, language="tr"):
    return {"id": f"id{i}", "title": title, "summary": summary, "link": f"https://example.test/{i}", "source": source,
            "language": language, "published_at": "2026-10-23T10:00:00+00:00"}


C = [
    candidate(0, "Beşiktaş 2-2 Crystal Palace Maç Özeti", "UEFA Avrupa Ligi'nde Beşiktaş, Crystal Palace ile 2-2 berabere kaldı.", "CNN Turk"),
    candidate(1, "Beşiktaş Crystal Palace maçı sonrası açıklama", "Teknik direktör Crystal Palace karşısındaki puanı değerlendirdi.", "BBC Turkce"),
    candidate(2, "Etsy is cracking down on AI slop listings", "Etsy is announcing stricter rules for sellers.", "TechCrunch", "en"),
    candidate(3, "Anadolu Efes Beşiktaş'ı 89-80 yendi", "EuroLeague'in 7. haftasında Anadolu Efes sahasında Beşiktaş'ı 89-80 mağlup etti.", "CNN Turk"),
]


def pick(n, **override):
    base = {"n": n, "title": C[n - 1]["title"], "headline": "Beşiktaş Avrupa'da berabere kaldı",
            "sentence": "Beşiktaş, Crystal Palace ile 2-2 berabere kaldı.",
            "detail": "Beşiktaş, UEFA Avrupa Ligi maçında Crystal Palace ile 2-2 berabere kaldı. Teknik direktör Crystal Palace karşısında aldıkları puanı değerlendirdi.",
            "topic": "futbol", "also": [2]}
    base.update(override)
    return base


def run(*picks):
    return select_items(list(picks), C, "spor")


# a complete, valid story keeps every field and lists the other outlet that covers the event
stories, dropped = run(pick(1))
expect("one story kept", len(stories), 1)
s = stories[0]
expect("headline kept", s["headline"], "Beşiktaş Avrupa'da berabere kaldı")
expect("detail kept", bool(s["detail"]), True)
expect("topic kept", s["topic"], "futbol")
expect("the other outlet is listed", [a["source"] for a in s["also"]], ["BBC Turkce"])
expect("link comes from the database", s["link"], "https://example.test/0")
expect("the original title is not stored", "title" in s, False)

# the copied title decides which article is meant, not the number the model wrote
stories, _ = run(pick(4, title=C[0]["title"], sentence="Beşiktaş, Crystal Palace ile 2-2 berabere kaldı."))
expect("title beats a wrong number", stories[0]["link"], "https://example.test/0")

# a title that matches nothing drops the story
stories, dropped = run(pick(1, title="Spotify bir şey duyurdu"))
expect("unknown title drops the story", (len(stories), dropped["title"]), (0, 1))

# an invented number in the detail removes the detail, not the story
stories, dropped = run(pick(1, detail="Beşiktaş, Crystal Palace ile 2-2 berabere kaldı. Maçı 48 bin kişi izledi."))
expect("story survives an unsupported detail", len(stories), 1)
expect("detail is dropped", stories[0]["detail"], "")
expect("and counted", dropped["detail"], 1)

# an invented number in the headline falls back to the sentence
stories, dropped = run(pick(1, headline="Beşiktaş 3 golle berabere kaldı"))
expect("headline falls back to the sentence", stories[0]["headline"], "Beşiktaş, Crystal Palace ile 2-2 berabere kaldı")

# an unknown topic becomes the catch-all of the section
stories, _ = run(pick(1, topic="uzay yolculuğu"))
expect("unknown topic becomes the catch-all", stories[0]["topic"], "diğer sporlar")

# `also` must point at real, different candidates from another outlet that share a name or number
stories, dropped = run(pick(1, also=[99, 1, 3, 4]))
expect("out of range, itself and same outlet are not listed", [a["link"] for a in stories[0]["also"]], [])
stories, _ = run(pick(1, also=[2, 3]))
expect("an unrelated English article is not listed as the same event", [a["source"] for a in stories[0]["also"]], ["BBC Turkce"])

# an English story whose sentence carries an invented number is left out; a Turkish one falls back to its title
stories, _ = run(pick(3, title=C[2]["title"], headline="Etsy yeni kurallar getirdi", sentence="Etsy, 500 satıcıyı yasakladı.", also=[]))
expect("English story with an invented number is dropped", len(stories), 0)
stories, _ = run(pick(4, title=C[3]["title"], headline="Efes kazandı", sentence="Anadolu Efes Beşiktaş'ı 91-80 yendi.", also=[]))
expect("Turkish story falls back to its title", stories[0]["sentence"], C[3]["title"])

# padding about reception is cut from the detail; the facts stay
stories, dropped = run(pick(1, detail="Beşiktaş, Crystal Palace ile 2-2 berabere kaldı. Maç basında geniş yankı buldu."))
expect("padding sentence removed", stories[0]["detail"], "Beşiktaş, Crystal Palace ile 2-2 berabere kaldı.")
expect("and counted", dropped["padding"], 1)

# a story the model itself marks as not belonging to the section is discarded
stories, dropped = run(pick(1, topic="bu bölüme uymuyor"))
expect("off-topic story is discarded", (len(stories), dropped["offtopic"]), (0, 1))

# the same article twice gives one story
stories, _ = run(pick(1), pick(1))
expect("no duplicate stories", len(stories), 1)

# a headline that only says something was said is recognised; one that says what was said is not
expect("vague: statement", vague_headline("Victor Osimhen'den Lille maçı sonrası açıklama"), True)
expect("vague: ministry", vague_headline("MSB'den askeri hastaneler ve sağlık hizmetleri açıklaması"), True)
expect("vague: spoke", vague_headline("‘Slow Horses’ Yönetmeni 6. Sezon Finali Hakkında Konuştu"), True)
expect("not vague: content", vague_headline("Osimhen: Lille'e karşı oynamak çok duygusaldı"), False)
expect("not vague: starts with the word", vague_headline("Açıklama yapılmadan operasyon başladı"), False)

if failures:
    print(f"{len(failures)} FAILED")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("all summarize tests passed")
