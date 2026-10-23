"""Unit tests for jobs/guards.py using real cases found in the stored summaries.

Run from backend/:
    ../.venv/bin/python evals/test_guards.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jobs"))
from guards import (grounded, match_title, normalise_title, numbers_supported, strip_unsupported_reactions,  # noqa: E402
                    title_index)

failures = []


def expect(name, got, want):
    if got != want:
        failures.append(f"{name}: got {got}, want {want}")


# --- numbers_supported ----------------------------------------------------------------
expect("two weeks -> 2 hafta", numbers_supported("2 hafta", "Netflix hit 50 million in two weeks"), True)
expect("four times -> 4 kat", numbers_supported("4 kat", "up to four times"), True)
expect("invented 37", numbers_supported("yüzde 37 arttı", "prices rose sharply in Q3"), False)
expect("digit present", numbers_supported("15 milyar dolar", "raises $15B"), True)
expect("no number in sentence", numbers_supported("Bir şey oldu", "whatever"), True)
expect("Turkish word iki", numbers_supported("2 gün", "iki gün sürdü"), True)
expect("üçüncü does not count as 3", numbers_supported("3 maç", "üçüncü maçında"), False)

# --- grounded: real wrong links found in stored summaries -----------------------------
MISMATCHES = [
    ("OpenAI, halka arz öncesinde 1 trilyon dolar değerleme üzerinden 30 milyar dolar yatırım toplamak için çalışmalara başladı.",
     "Etsy is cracking down on AI slop listings Etsy is announcing stricter rules about how AI can be used in listings"),
    ("Spotify, aynı evde yaşamayan kullanıcıların aile planı paylaşımını engellemek amacıyla yeni bir uygulama başlattı.",
     "Mark Zuckerberg’s creative plan to pay California’s proposed billionaire tax without losing control of Meta"),
    ("Canva, görsel üretmeden önce bir tasarım planlayan yeni yapay zeka görsel modelini kullanıma sundu.",
     "Warhound raises $200M to engineer war dogs and arm them with Anduril gear"),
    ("PHP'deki iki kritik güvenlik açığının resmi yamalar yayımlanmadan önce saldırganlarca kullanıldığı belirtildi.",
     "Moderna and Merck say their mRNA cancer vaccine cut death, recurrence by 42%"),
    ("ABD Federal İletişim Komisyonu, ulusal güvenlik tehdidi listesindeki şirketlere ait bilgisayarları yasakladı.",
     "As blockchain investing falls out of vogue, a16z Crypto rebrands as a16z Frontier"),
    # found by the offline self test: a name with an inner capital at the start of the sentence
    ("OpenAI yeni modelini duyurdu.", "Etsy is cracking down on AI slop listings"),
]
for sentence, source in MISMATCHES:
    expect(f"mismatch is flagged: {sentence[:30]}", grounded(sentence, source, "en"), False)

# --- grounded: correct pairs that an earlier version wrongly flagged -------------------
CORRECT = [
    ("Etsy, platformdaki yapay zeka içerikli listelemelerin hacmini azaltmak için daha katı kurallar getirdi.",
     "Etsy is cracking down on AI slop listings Etsy is announcing stricter rules", "en"),
    ("Microsoft, yapay zeka aracının hastalıkları teşhis etmede doktorlardan 4 kat daha başarılı olduğunu iddia ediyor.",
     "Microsoft says its AI tool is better than doctors at diagnosing conditions up to four times", "en"),
    ("Yapay zeka hisselerindeki sert düşüşler küresel piyasaları olumsuz etkilerken petrol fiyatları yükseldi.",
     "Sharp drops for AI stocks overwhelm markets worldwide as oil prices jump", "en"),
    ("Beşiktaş, Crystal Palace ile berabere kaldı.",
     "Vincenzo Italiano: Önemli bir puan aldık Beşiktaş Teknik Direktörü Crystal Palace maçı ardından", "tr"),
]
for sentence, source, lang in CORRECT:
    expect(f"correct pair passes: {sentence[:30]}", grounded(sentence, source, lang), True)

# --- title matching --------------------------------------------------------------------
candidates = [{"title": "Beşiktaş 2-2 Crystal Palace Maç Özeti"}, {"title": "Etsy is cracking down on ‘AI slop’"}]
index = title_index(candidates)
expect("exact title", match_title("Beşiktaş 2-2 Crystal Palace Maç Özeti", index), 0)
expect("quotes and case ignored", match_title("etsy is cracking down on 'AI slop'", index), 1)
expect("unknown title", match_title("Spotify bir şey yaptı", index), None)
expect("normalise", normalise_title("  Hello,  World! "), "hello world")

# --- reactions nobody stated (found in a real summary: "...basında geniş yankı buldu") -----------------
SRC = ("Manchester United'ın yıldızı Bruno Fernandes'e hırsızlık şoku İngiltere Premier Lig ekibi Manchester United'ın "
       "kaptanı Bruno Fernandes'in evine hırsız girdi.")
DETAIL = ("Bruno Fernandes'in evine hırsız girdi. Manchester United'da yaşanan bu hırsızlık şoku basında geniş yankı buldu.")
expect("padding sentence is dropped, the factual one stays",
       strip_unsupported_reactions(DETAIL, SRC), "Bruno Fernandes'in evine hırsız girdi.")
expect("a reaction the source does state is kept",
       strip_unsupported_reactions("Karar geniş tepki çekti.", "Karar sonrası muhalefetten tepki geldi."), "Karar geniş tepki çekti.")
expect("'ilgili' (related) is not mistaken for 'ilgi'",
       strip_unsupported_reactions("Konuyla ilgili açıklama yapıldı.", "Bir açıklama yapıldı."), "Konuyla ilgili açıklama yapıldı.")
expect("nothing left gives an empty string", strip_unsupported_reactions("Büyük bir şok yaşandı.", "Bir olay oldu."), "")
expect("Turkish capital I is handled", strip_unsupported_reactions("Kararın ardından ENDİŞE arttı.", "Karar açıklandı."), "")

if failures:
    print(f"{len(failures)} FAILED")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("all guard tests passed")
