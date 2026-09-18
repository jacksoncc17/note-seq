"""Round-trip fact checking.

Written because the first version of this check reported "fifty, three, sixty,
nine LOST" for a transcript that correctly contained "53" and "69" -- a check
that cries wolf is worse than none.
"""

import pytest

from tutor.roundtrip import (chinese_numbers, compare_roundtrip, digits,
                             english_numbers, expected_numbers, latin_tokens)


def test_digits_and_english_number_words():
  assert digits('bar 53 and 69') == {53, 69}
  assert english_numbers('fifty three') == {53}
  assert english_numbers('sixty nine') == {69}
  assert english_numbers('twenty') == {20}
  assert english_numbers('bar four') == {4}
  assert english_numbers('fifteen') == {15}


def test_chinese_numbers():
  assert chinese_numbers('第五十三小节') == {53}
  assert chinese_numbers('六十九') == {69}
  assert chinese_numbers('二十') == {20}
  assert chinese_numbers('十') == {10}
  assert chinese_numbers('没有数字') == set()


def test_expected_numbers_merges_every_notation():
  assert expected_numbers('第53小节, 五十三, fifty three') == {53}


def test_latin_tokens_pick_out_note_and_chord_symbols():
  assert latin_tokens('the key changes to A major') == ['A']
  assert latin_tokens('第五十三小节开始，调性转到A大调。') == ['A']
  assert latin_tokens('play Bm7b5 then F# and E7') == ['Bm7b5', 'F#', 'E7']
  assert latin_tokens('ordinary words: Bad Cab Ed Fad') == []
  # ordinary words are not letter facts
  assert latin_tokens('fifty three') == []


def test_digits_are_found_next_to_chinese_text():
  """Regression: \\b\\d never matched inside 第53小节, so a number that plainly
  came back was reported as lost."""
  assert digits('第53小节开始') == {53}
  assert digits('bar 53') == {53}
  assert digits('measures 12-14') == {12, 14}


def test_content_words_exclude_stopwords():
  from tutor.roundtrip import content_words
  assert content_words('the key changes to A major') == ['key', 'changes', 'major']
  assert content_words('fifty three bars') == ['bars']


def test_a_good_roundtrip_reports_no_losses():
  phrase = ('Measure fifty three. The key changes to A major, then returns to '
            'A minor at bar sixty nine.')
  heard = 'measure 53, the key changes to a major, then returns to a minor at bar 69'
  result = compare_roundtrip(phrase, heard)
  assert result['ok'] is True, result
  assert result['lost_numbers'] == []
  assert result['lost_letters'] == []
  assert result['lost_words'] == []


def test_a_lost_key_letter_is_reported():
  # Real output from a live Chinese test: the letter A never reaches the audio.
  result = compare_roundtrip('调性转到A大调。', '调性转到大调')
  assert result['ok'] is False
  assert result['lost_letters'] == ['A']


def test_a_major_minor_swap_is_reported():
  """The error a music tutor must never make."""
  result = compare_roundtrip('the key changes to A major',
                             'the key changes to A minor')
  assert result['ok'] is False
  assert 'major' in result['lost_words']


def test_a_wrong_bar_number_is_reported():
  # Real output from an earlier live run where VAD fragmentation corrupted it.
  result = compare_roundtrip('returns at bar sixty nine', 'returns at bar 60')
  assert result['ok'] is False
  assert 69 in result['lost_numbers']


def test_chinese_character_coverage():
  # Numerals legitimately come back as digits, so they are not counted.
  result = compare_roundtrip('第五十三小节开始', '第53小节开始')
  assert result['cjk_ratio'] == pytest.approx(1.0)
  result = compare_roundtrip('调性转到大调', '调性转了')   # 大调 dropped
  assert 0 < result['cjk_ratio'] < 1.0
