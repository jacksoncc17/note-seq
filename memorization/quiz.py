"""Study sheet + drill driver for the memorization experiment.

Usage:
  # readable overview of the piece (the "map" you memorize against)
  .venv/bin/python memorization/quiz.py sheet

  # sample N questions of a tier, answers included (used to quiz you in chat)
  .venv/bin/python memorization/quiz.py drill --tier 1 --count 8 [--seed 7]
  .venv/bin/python memorization/quiz.py drill --tier 3 --count 4   # piano tasks

  # record a result so weak spots can be re-asked later
  .venv/bin/python memorization/quiz.py record --id q012 --result wrong
  .venv/bin/python memorization/quiz.py stats
  .venv/bin/python memorization/quiz.py misses --limit 10

State lives in memorization/output/progress.json.
"""

import argparse
import json
import os
import random

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, 'output')
FP_DEFAULT = os.path.join(OUT, 'fingerprint.json')
PROGRESS = os.path.join(OUT, 'progress.json')

TIER_NAMES = {
    0: 'Tier 0 - structure (the skeleton)',
    1: 'Tier 1 - harmony (the chord map)',
    2: 'Tier 2 - surface (melody, rhythm, register)',
    3: 'Tier 3 - performance (at the piano)',
}


def load_fp(path):
  with open(path) as f:
    return json.load(f)


def load_progress():
  if os.path.exists(PROGRESS):
    with open(PROGRESS) as f:
      return json.load(f)
  return {'results': {}}


def save_progress(p):
  os.makedirs(OUT, exist_ok=True)
  with open(PROGRESS, 'w') as f:
    json.dump(p, f, indent=2)


def write_markdown(fp, path, measures_limit=0):
  """Write the whole map out as a study document."""
  g = fp['global']
  lines = []
  lines.append('# %s' % (g['title'] or os.path.basename(fp['source_file'])))
  if g['composers']:
    lines.append('*%s*' % ', '.join(g['composers']))
  lines.append('')
  lines.append('| | |')
  lines.append('|---|---|')
  lines.append('| measures | %d |' % g['num_measures'])
  lines.append('| notes | %d |' % g['total_notes'])
  lines.append('| key | %s |' % g['key_inferred'])
  lines.append('| meter | %s |' % '/'.join(g['time_signatures_declared']))
  lines.append('| range | %s – %s |' % (g['lowest_note'], g['highest_note']))
  lines.append('| duration (as written) | %s s |' % g['total_time_s'])
  lines.append('| bar grid | %s |' % fp.get('grid_source', '?'))
  lines.append('| climax (by register/loudness/density) | m%s |' % fp['climax_measure'])
  lines.append('')

  lines.append('## Form (key-signature regions)')
  lines.append('')
  lines.append('| | key | bars | length | bars in score |')
  lines.append('|---|---|---|---|---|')
  for s in fp['sections']:
    lines.append('| %s | %s | %d–%d | %d | %s |'
                 % (s['label'], s['key'], s['start_measure'], s['end_measure'],
                    s['measures'], s.get('signature_in_score', '-')))
  lines.append('')

  reps = fp.get('repetitions') or []
  if reps:
    lines.append('## Repeated passages (verified note-for-note)')
    lines.append('')
    lines.append('Learn the first span once and the later one is free.')
    lines.append('')
    lines.append('| learn this | it returns at | length | exact |')
    lines.append('|---|---|---|---|')
    for r in reps:
      lines.append('| m%d–%d | m%d–%d | %d bars | %s |'
                   % (r['from_measure'], r['to_measure'], r['repeat_measure'],
                      r['repeat_end_measure'], r['length'],
                      'yes' if r.get('exact') else 'near (%.2f)' % r.get('similarity', 0)))
    lines.append('')
    covered = set()
    for r in reps:
      for m in range(r['from_measure'], r['to_measure'] + 1):
        covered.add(m)
      for m in range(r['repeat_measure'], r['repeat_end_measure'] + 1):
        covered.add(m)
    unique = [m['measure'] for m in fp['measures'] if m['measure'] not in covered]
    lines.append('Bars that are not part of any repeat: %d of %d.'
                 % (len(unique), g['num_measures']))
    if unique:
      spans, start, prev = [], unique[0], unique[0]
      for m in unique[1:]:
        if m != prev + 1:
          spans.append((start, prev))
          start = m
        prev = m
      spans.append((start, prev))
      lines.append('')
      lines.append('Unique material: %s' % ', '.join(
          'm%d' % a if a == b else 'm%d–%d' % (a, b) for a, b in spans))
    lines.append('')

  if measures_limit:
    lines.append('## Measure map (first %d bars)' % measures_limit)
    lines.append('')
    lines.append('| bar | chord | top note | notes | rhythm | contour |')
    lines.append('|---|---|---|---|---|---|')
    for m in fp['measures'][:measures_limit]:
      lines.append('| %d | %s | %s | %d | %s | %s |'
                   % (m['measure'], m['chord'] or '?', m['melody'] or '-',
                      m['notes_count'], m['rhythm'], m['contour']))
    lines.append('')
  with open(path, 'w') as f:
    f.write('\n'.join(lines) + '\n')
  return path


def cmd_sheet(args):
  fp = load_fp(args.fingerprint)
  if args.markdown:
    path = write_markdown(fp, args.markdown, args.measure_rows)
    print('wrote %s' % path)
    return
  g = fp['global']
  print('=' * 72)
  print('%s' % (g['title'] or os.path.basename(fp['source_file'])))
  if g['composers']:
    print('composer: %s' % ', '.join(g['composers']))
  print('=' * 72)
  print('measures: %d | notes: %d | key: %s | meter: %s'
        % (g['num_measures'], g['total_notes'], g['key_inferred'],
           '/'.join(g['time_signatures_declared'])))
  print('range: %s - %s | duration: %ss | bar grid: %s'
        % (g['lowest_note'], g['highest_note'], g['total_time_s'],
           fp.get('grid_source', '?')))
  print('key from score: %s (mode declared in score: %s)'
        % (g['key_from_score'], g['key_mode_declared_in_score']))
  print('climax: measure %s | tempo events: %d'
        % (fp['climax_measure'], g['num_tempo_events']))

  print('\nKEY AREAS (form)  [basis: %s]' % fp.get('section_basis', '?'))
  for s in fp['sections']:
    sig = s.get('signature_in_score', '-')
    extra = '' if sig in ('-', s['key']) else '  [score signature: %s]' % sig
    print('  %-4s %-9s m%-4d - m%-4d  (%d bars)%s'
          % (s['label'], s['key'], s['start_measure'], s['end_measure'],
             s['measures'], extra))

  reps = fp.get('repetitions') or []
  if reps:
    print('\nREPEATED PASSAGES  [basis: %s]' % fp.get('repeat_basis', '?'))
    for r in reps:
      print('  m%-4d-m%-4d  returns at  m%-4d-m%-4d  (%2d bars)  %s'
            % (r['from_measure'], r['to_measure'], r['repeat_measure'],
               r['repeat_end_measure'], r['length'],
               'EXACT' if r.get('exact') else 'near (sim %.2f)' % r.get('similarity', 0)))

  if args.measures:
    print('\nMEASURE MAP')
    print('  %-6s %-12s %-8s %-6s %-24s %s'
          % ('bar', 'chord', 'top', 'notes', 'rhythm', 'contour'))
    for m in fp['measures']:
      if not m['notes_count']:
        print('  %-6d %-12s %-8s %-6s %s'
              % (m['measure'], m['chord'] or '-', '-', 0, 'rest'))
        continue
      print('  %-6d %-12s %-8s %-6d %-24s %s'
            % (m['measure'], m['chord'] or '?', m['melody'], m['notes_count'],
               m['rhythm'][:24], m['contour']))

  counts = {}
  for q in fp['questions']:
    counts[q['tier']] = counts.get(q['tier'], 0) + 1
  print('\nQUESTION BANK')
  for t in sorted(counts):
    print('  %-45s %4d questions' % (TIER_NAMES[t], counts[t]))


def cmd_drill(args):
  fp = load_fp(args.fingerprint)
  qs = [q for q in fp['questions'] if q['tier'] == args.tier]
  if args.type:
    qs = [q for q in qs if q['type'] == args.type]
  if not qs:
    print('no questions for tier %d' % args.tier)
    return
  progress = load_progress()
  # Weak spots first (previously missed), then unseen, then seen-correct.
  def rank(q):
    r = progress['results'].get(q['id'], {}).get('last')
    return {None: 1, 'correct': 2, 'wrong': 0}[r]
  rng = random.Random(args.seed)
  rng.shuffle(qs)
  qs.sort(key=rank)
  for q in qs[:args.count]:
    print('-' * 72)
    print('[%s] %s' % (q['id'], TIER_NAMES[q['tier']]))
    print('Q: %s' % q['prompt'])
    if q.get('options'):
      print('   options: %s' % ' | '.join(q['options']))
    if q.get('hint') and args.hints:
      print('   hint: %s' % q['hint'])
    print('A: %s' % q['answer'])


def cmd_record(args):
  p = load_progress()
  r = p['results'].setdefault(args.id, {'wrong': 0, 'correct': 0})
  r[args.result] = r.get(args.result, 0) + 1
  r['last'] = args.result
  save_progress(p)
  print('recorded %s = %s' % (args.id, args.result))


def cmd_stats(args):
  fp = load_fp(args.fingerprint)
  p = load_progress()
  by_tier = {}
  for q in fp['questions']:
    r = p['results'].get(q['id'], {})
    t = q['tier']
    d = by_tier.setdefault(t, {'correct': 0, 'wrong': 0, 'unseen': 0})
    if not r:
      d['unseen'] += 1
    elif r.get('last') == 'correct':
      d['correct'] += 1
    else:
      d['wrong'] += 1
  print('%-45s %8s %8s %8s' % ('tier', 'correct', 'wrong', 'unseen'))
  for t in sorted(by_tier):
    d = by_tier[t]
    print('%-45s %8d %8d %8d' % (TIER_NAMES[t], d['correct'], d['wrong'],
                                 d['unseen']))


def cmd_misses(args):
  fp = load_fp(args.fingerprint)
  p = load_progress()
  shown = 0
  for q in fp['questions']:
    r = p['results'].get(q['id'], {})
    if r.get('last') == 'wrong':
      print('[%s] %s -> %s (missed %dx)'
            % (q['id'], q['prompt'], q['answer'], r.get('wrong', 0)))
      shown += 1
      if shown >= args.limit:
        break
  if not shown:
    print('no outstanding misses')


def main():
  ap = argparse.ArgumentParser()
  ap.add_argument('--fingerprint', default=FP_DEFAULT)
  sub = ap.add_subparsers(dest='cmd', required=True)

  s = sub.add_parser('sheet')
  s.add_argument('--measures', action='store_true', help='print per-measure map')
  s.add_argument('--markdown', default=None,
                 help='write the whole map to this .md file instead')
  s.add_argument('--measure-rows', type=int, default=40,
                 help='how many bars to include in the markdown measure map')
  s.set_defaults(func=cmd_sheet)

  d = sub.add_parser('drill')
  d.add_argument('--tier', type=int, required=True)
  d.add_argument('--count', type=int, default=8)
  d.add_argument('--type', default=None)
  d.add_argument('--seed', type=int, default=None)
  d.add_argument('--hints', action='store_true')
  d.set_defaults(func=cmd_drill)

  r = sub.add_parser('record')
  r.add_argument('--id', required=True)
  r.add_argument('--result', required=True, choices=['correct', 'wrong'])
  r.set_defaults(func=cmd_record)

  st = sub.add_parser('stats')
  st.set_defaults(func=cmd_stats)

  m = sub.add_parser('misses')
  m.add_argument('--limit', type=int, default=10)
  m.set_defaults(func=cmd_misses)

  args = ap.parse_args()
  args.func(args)


if __name__ == '__main__':
  main()
