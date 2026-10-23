"""Checks applied to model-written sentences before they are shown to anyone.

The model only picks a candidate article and writes one sentence about it. These checks
make sure the sentence really belongs to the article it is linked to:

  numbers_supported  every number in the sentence appears in the article text
  grounded           the sentence shares at least one name, number or word stem with it
  match_title        the title the model copied identifies exactly one candidate

The same rules exist in TypeScript in supabase/functions/ask/index.ts; keep them in step.
"""
import re

NUMBER_WORDS = {
    1: ("one", "bir"), 2: ("two", "iki"), 3: ("three", "üç"), 4: ("four", "dört"),
    5: ("five", "beş"), 6: ("six", "altı"), 7: ("seven", "yedi"), 8: ("eight", "sekiz"),
    9: ("nine", "dokuz"), 10: ("ten", "on"), 11: ("eleven", "on bir"), 12: ("twelve", "on iki"),
    13: ("thirteen",), 14: ("fourteen",), 15: ("fifteen",), 16: ("sixteen",), 17: ("seventeen",),
    18: ("eighteen",), 19: ("nineteen",), 20: ("twenty", "yirmi"),
}

STEM_LENGTH = 5


def numbers_supported(sentence, source_text):
    """Every number in the sentence must appear in the article's own text, as digits or as a
    number word (sources often write 'two weeks', the sentence says '2 hafta')."""
    text = source_text.lower()
    for num in re.findall(r"\d+", sentence):
        if num in text:
            continue
        words = NUMBER_WORDS.get(int(num), ()) if len(num) <= 2 else ()
        if not any(re.search(rf"\b{w}\b", text) for w in words):
            return False
    return True


def _stem(word):
    word = re.split(r"['’]", word)[0].lower().replace("i̇", "i")  # drop suffixes like Etsy'nin
    return word[:STEM_LENGTH]


def _words(text):
    return re.findall(r"[^\W\d_][\w'’]*", text)


def _name_stems(sentence):
    """Capitalised words that look like names. The first word counts only when it clearly is
    one ('Name, ...', 'Name'nin ...', an all-caps acronym, or a capital inside the word as
    in OpenAI); otherwise it is capitalised just because it starts the sentence."""
    stems = set()
    for i, word in enumerate(_words(sentence)):
        if len(re.split(r"['’]", word)[0]) < 3 or not word[0].isupper():
            continue
        if i == 0 and not (re.match(rf"^{re.escape(word)}\s*[,:]", sentence)
                           or re.search(r"['’]", word) or word.isupper()
                           or any(c.isupper() for c in word[1:])):
            continue
        stems.add(_stem(word))
    return stems


def grounded(sentence, source_text, language):
    """False only when the sentence has names or numbers and shares none of them with the
    source (and, for Turkish sources, fewer than two ordinary word stems). A sentence
    with no name and no number cannot be judged this way, so it passes."""
    names = _name_stems(sentence)
    digits = re.findall(r"\d+", sentence)
    if not names and not digits:
        return True
    lowered = source_text.lower()
    source_stems = {_stem(w) for w in _words(source_text) if len(w) >= 4}
    if names & source_stems:
        return True
    if any(d in lowered for d in digits):
        return True
    if language == "tr":
        sentence_stems = {_stem(w) for w in _words(sentence) if len(w) >= 4}
        return len(sentence_stems & source_stems) >= 2
    return False


def normalise_title(title):
    """Compare titles ignoring case, quotes and punctuation differences."""
    return re.sub(r"[\W_]+", " ", (title or "").lower()).strip()


def title_index(candidates):
    """Map normalised title -> position in the candidate list (first one wins)."""
    index = {}
    for i, c in enumerate(candidates):
        index.setdefault(normalise_title(c["title"]), i)
    return index


def match_title(title, index):
    """Position of the candidate whose title the model copied, or None."""
    return index.get(normalise_title(title))
