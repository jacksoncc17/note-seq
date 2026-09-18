"""Check what survives a speech round-trip.

Used by `--tts-probe --roundtrip`: synthesise a phrase, transcribe it back, and
see which facts made it. The point is catching the failures that matter for a
music tutor -- a bar number that arrives as something else, or a key letter that
never reaches the audio at all.

Numbers are compared by VALUE, not spelling: "fifty three" coming back as "53"
is a success, and a naive word match would report it as a loss (which is exactly
how a check starts crying wolf and gets ignored). Chinese numerals are parsed
too, because 第五十三 -> 53 is the normal path in a Chinese session.
"""

from __future__ import annotations

import re
from typing import Dict, List, Set

EN_UNITS = {
    'zero': 0, 'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5,
    'six': 6, 'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10, 'eleven': 11,
    'twelve': 12, 'thirteen': 13, 'fourteen': 14, 'fifteen': 15,
    'sixteen': 16, 'seventeen': 17, 'eighteen': 18, 'nineteen': 19,
}
EN_TENS = {'twenty': 20, 'thirty': 30, 'forty': 40, 'fifty': 50, 'sixty': 60,
           'seventy': 70, 'eighty': 80, 'ninety': 90}
CN_DIGITS = {'零': 0, '〇': 0, '一': 1, '壹': 1, '二': 2, '两': 2, '三': 3,
             '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9}
CN_UNITS = {'十': 10, '百': 100, '千': 1000}


def english_numbers(text: str) -> Set[int]:
  """Values of number words: 'fifty three' -> 53, 'twenty' -> 20, 'four' -> 4.

  Bare units count, because "bar four" is a bar number. The cost is that the
  pronoun "one" is also read as a number; in a tutor's vocabulary that trade is
  worth taking, and a false "number lost" note is cheap.
  """
  found: Set[int] = set()
  tokens = re.findall(r"[a-z']+", (text or '').lower())
  i = 0
  while i < len(tokens):
    token = tokens[i]
    if token in EN_TENS:
      value = EN_TENS[token]
      if i + 1 < len(tokens) and tokens[i + 1] in EN_UNITS and \
          EN_UNITS[tokens[i + 1]] < 10:
        value += EN_UNITS[tokens[i + 1]]
        i += 1
      found.add(value)
    elif token in EN_UNITS:
      found.add(EN_UNITS[token])
    i += 1
  return found


def chinese_numbers(text: str) -> Set[int]:
  """Values of Chinese numerals: 五十三 -> 53, 二十 -> 20, 十 -> 10."""
  found: Set[int] = set()
  run = ''
  for char in (text or '') + ' ':
    if char in CN_DIGITS or char in CN_UNITS:
      run += char
      continue
    if run:
      found.add(_parse_chinese(run))
      run = ''
  return found


def _parse_chinese(run: str) -> int:
  total, section, number = 0, 0, 0
  for char in run:
    if char in CN_DIGITS:
      number = CN_DIGITS[char]
    else:
      unit = CN_UNITS[char]
      section += (number or 1) * unit
      number = 0
  return total + section + number


def digits(text: str) -> Set[int]:
  """Digit values, including digits embedded in CJK text.

  `\\b\\d` does not work here: Python treats CJK characters as word characters,
  so there is no boundary inside 第53小节 and the number was reported as lost
  even though the transcript plainly contained it.
  """
  return {int(d) for d in re.findall(r'(?<!\d)(\d{1,4})(?!\d)', text or '')}


def expected_numbers(text: str) -> Set[int]:
  return digits(text) | english_numbers(text) | chinese_numbers(text)


# Ordinary words worth checking for preservation: "major" arriving as "minor"
# is exactly the kind of error a tutor cannot make.
STOPWORDS = {
  'a', 'an', 'the', 'and', 'or', 'to', 'at', 'in', 'on', 'of', 'for', 'is',
  'it', 'its', 'this', 'that', 'then', 'than', 'as', 'by', 'with', 'from',
  'be', 'was', 'are', 'do', 'does', 'i', 'you', 'we', 'my', 'your',
}

NOTE_TOKEN_RE = re.compile(r'^[A-G](#|b)?\d{0,2}$')
# Longest alternatives first, or "m" wins and "Bm7b5" never matches. The
# suffixes are spelled out rather than a loose character class so ordinary short
# words ("Bad", "Cab", "Ed") are not mistaken for chords.
CHORD_QUALITY = (r'(maj9|maj7|maj|min7|min|m7b5|m9|m7|m|dim7|dim|aug|sus4|sus2|'
                 r'add9|6|7|9|11|13|\+|°|ø)')
CHORD_TOKEN_RE = re.compile(
    r'^[A-G](#|b)?%s?(b5|#5|b9|#9)?\d{0,2}$' % CHORD_QUALITY)


def latin_tokens(text: str) -> List[str]:
  """Note and chord symbols -- the tokens that carry a key letter.

  Ordinary words are not included: they are checked separately by
  `content_words`, and mixing the two made "letters kept: Measure, The, at"
  look like a meaningful result.
  """
  tokens = re.findall(r'[A-Za-z][A-Za-z#b0-9]*', text or '')
  return [t for t in tokens if NOTE_TOKEN_RE.match(t) or CHORD_TOKEN_RE.match(t)]


def content_words(text: str) -> List[str]:
  words = re.findall(r"[A-Za-z][A-Za-z'#]*", text or '')
  return [w for w in words if w.lower() not in STOPWORDS
          and w.lower() not in EN_UNITS and w.lower() not in EN_TENS]


def compare_roundtrip(phrase: str, transcript: str) -> Dict[str, object]:
  """Which facts from `phrase` survived into `transcript`."""
  want_numbers = expected_numbers(phrase)
  have_numbers = expected_numbers(transcript)
  kept_numbers = sorted(n for n in want_numbers if n in have_numbers)
  lost_numbers = sorted(n for n in want_numbers if n not in have_numbers)

  lower = (transcript or '').lower()
  letters = latin_tokens(phrase)
  kept_letters = sorted({w for w in letters if w.lower() in lower})
  lost_letters = sorted({w for w in letters if w.lower() not in lower})

  words = content_words(phrase)
  kept_words = sorted({w for w in words if w.lower() in lower})
  lost_words = sorted({w for w in words if w.lower() not in lower})

  # Chinese numerals are excluded: they legitimately come back as digits, so
  # counting them as missing characters reported a false failure.
  cjk = [c for c in (phrase or '')
         if '\u4e00' <= c <= '\u9fff' and c not in CN_DIGITS and c not in CN_UNITS]
  seen = set(re.findall(r'[\u4e00-\u9fff]', transcript or ''))
  ratio = (sum(1 for c in cjk if c in seen) / len(cjk)) if cjk else 1.0

  return {'kept_numbers': kept_numbers, 'lost_numbers': lost_numbers,
          'kept_letters': kept_letters, 'lost_letters': lost_letters,
          'kept_words': kept_words, 'lost_words': lost_words,
          'cjk_ratio': ratio,
          'ok': not lost_numbers and not lost_letters and not lost_words}
