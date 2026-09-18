"""Grounding tests: the verifier must catch fabrications without crying wolf."""

import pytest

from tutor.config import TutorConfig
from tutor.core.facts import FactStore
from tutor.core.tools import ToolBox
from tutor.grounding import Evidence, GroundingVerifier
from tutor.session import Session

CFG = TutorConfig()


class NullSession:
  def record_attempt(self, *a, **k):
    pass

  def status(self):
    return {}

  def weak_spots(self, limit=5):
    return []

  def weak_spot_prompts(self, limit=5):
    return []


def setup_verifier(policy='block'):
  facts = FactStore(CFG.fingerprint)
  ev = Evidence(facts)
  return facts, ev, GroundingVerifier(ev, facts, policy=policy)


def an_impossible_bar(facts) -> int:
  """A bar number that cannot exist in the loaded piece and is checkable.

  Kept to four digits because that is what the claim extractor recognises; the
  regex used to stop at three, so "bar 1234" went unchecked entirely.
  """
  total = int(facts.global_facts.get('num_measures', 0))
  if total >= 9990:
    pytest.skip('piece too long for a four-digit impossible bar')
  return max(total + 100, 9999 if total > 9800 else total + 100)


def test_claims_are_ungrounded_before_any_tool_call():
  _, _, verifier = setup_verifier()
  # No tool has run, so nothing is citable -- whichever piece is loaded.
  report = verifier.check('The key changes to F# major at bar 1234.')
  assert report.ok is False
  # Both the bar reference and the key name are unsupported claims.
  assert sorted((c.kind, c.value) for c in report.ungrounded) == [
      ('bar', '1234'), ('key', 'F# major')]


def test_observed_facts_become_citable():
  facts, ev, verifier = setup_verifier()
  if not facts.sections:
    pytest.skip('this bank has no sections')
  toolbox = ToolBox(facts, NullSession(), tier_max=CFG.tier_max)
  ev.observe(toolbox.get_piece_map())
  # Expectations come from the loaded bank, not from a hard-coded piece.
  section = facts.sections[0]
  text = 'Section %s is in %s, starting at bar %d.' % (
      section['label'], section['key'], section['start_measure'])
  assert verifier.check(text).ok is True, verifier.check(text).as_dict()
  # A bar number no tool ever returned is still refused.
  assert verifier.check('It ends at bar %d.' % an_impossible_bar(facts)).ok is False


def test_question_options_are_not_treated_as_assertions():
  facts, ev, verifier = setup_verifier()
  bar = max(1, int(facts.global_facts.get('num_measures', 2)) // 2)
  bar = bar if facts.bar(bar) else 1
  toolbox = ToolBox(facts, NullSession(), tier_max=CFG.tier_max)
  ev.observe(toolbox.get_bar(bar))
  text = ('What is the chord in measure %d? Is it Bm7b5, Am, E7 or Dm?' % bar)
  report = verifier.check(text)
  assert report.ok is True, report.as_dict()
  # Only the bar reference inside the question is checked.
  assert [c.kind for c in report.claims] == ['bar']


def test_ordinary_words_are_not_read_as_chords():
  _, _, verifier = setup_verifier()
  report = verifier.check('I am listening and I am not sure.')
  assert report.claims == []


def test_chord_tokens_are_not_read_as_notes():
  facts, ev, verifier = setup_verifier()
  toolbox = ToolBox(NullSession() and facts, NullSession(), tier_max=CFG.tier_max)
  result = toolbox.grade_answer('E7')  # grounds nothing; just ensures no crash
  ev.observe(result)
  ev.chords.add('e7')
  report = verifier.check('It is E7.')
  assert report.ok is True
  assert [c.kind for c in report.claims] == ['chord']


def test_block_policy_drops_only_the_offending_sentence():
  facts, ev, verifier = setup_verifier(policy='block')
  if not facts.sections:
    pytest.skip('this bank has no sections')
  toolbox = ToolBox(facts, NullSession(), tier_max=CFG.tier_max)
  ev.observe(toolbox.get_piece_map())
  good_bar = facts.sections[0]['start_measure']
  impossible = an_impossible_bar(facts)
  text = ('It starts at bar %d. The coda starts at bar %d.'
          % (good_bar, impossible))
  report = verifier.check(text)
  assert report.ok is False
  repaired = verifier.repair(text, report)
  assert 'bar %d' % good_bar in repaired
  assert str(impossible) not in repaired


def test_block_policy_drops_everything_when_all_sentences_are_bad():
  _, _, verifier = setup_verifier(policy='block')
  report = verifier.check('It starts at bar 5432.')
  assert verifier.repair('It starts at bar 5432.', report) is None


def test_hedge_policy_keeps_the_text_and_flags_uncertainty():
  _, _, verifier = setup_verifier(policy='hedge')
  text = 'It starts at bar 5432.'
  repaired = verifier.repair(text, verifier.check(text))
  assert 'bar 5432' in repaired
  assert 'not certain' in repaired


def test_warn_policy_passes_through_but_reports():
  _, _, verifier = setup_verifier(policy='warn')
  text = 'It starts at bar 999.'
  report = verifier.check(text)
  assert verifier.repair(text, report) == text
  assert report.ok is False
