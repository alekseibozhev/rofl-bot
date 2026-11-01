"""What Jev said in the shadow against what the bot did: shadow.jsonl joined with the bot's answers, whose tags stay
in its table `answers` for good, and with its log, which says what the code took for a request.

Per tag of the setup: Jev's scores on the requests the setup tagged and on the rest, how well they part (AUC), and
the requests they disagree on at the threshold that agrees most. Then the messages the code did not take for
requests that Jev takes as said to Рофл, and the moments Jev would butt in on, at the probe's 0.87. Last, a day a
line, how the chat took what the bot said: Jev's mark on what people wrote after it, and the replies to it.
Usage: stats.py [--since HOURS]
"""
import argparse, datetime, json, re, sqlite3, time
from collections import Counter, defaultdict
from pathlib import Path

from shadow import DB, OUT, TAKEN, TAKES, bot

LOGS = sorted(Path(bot.__file__).parent.glob('bot.log*'))
RECEIVED = re.compile(r'telegram\.received chat=(-?\d+) message=(\d+) .*addressed=(True|False)')


def auc(pairs):
    pos, neg = [x for x, y in pairs if y], [x for x, y in pairs if not y]
    if not pos or not neg:
        return None
    return sum((p > n) + 0.5 * (p == n) for p in pos for n in neg) / len(pos) / len(neg)


def threshold(pairs):
    """The threshold that agrees most with the labels, the middle one of a tie."""
    cuts = sorted({x for x, _ in pairs} | {1.01})
    best = max(sum((x >= c) == y for x, y in pairs) for c in cuts)
    ties = [c for c in cuts if sum((x >= c) == y for x, y in pairs) == best]
    return ties[len(ties) // 2]


def setup_tags(trace):
    """The decisions of the setup as Jev's question names: «[тег все]» is «всем» too, a form is «форма»."""
    tags = {}
    for tag in bot.TAG.finditer(trace.get('tags') or ''):
        tags[tag[1]] = True
        if tag[1] == 'тег' and 'все' in {name.strip().lower() for name in tag[2].split(',')}:
            tags['всем'] = True
        if tag[1] in bot.FORMS:
            tags['форма'] = tag[1]
    for overruled in trace.get('overruled') or ():
        tags.pop(overruled, None)
    return tags


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--since', type=float, default=10 ** 6, help='only the messages of the last HOURS')
    args = parser.parse_args()
    since = time.time() - args.since * 3600
    latest = {}
    for line in OUT.read_text().splitlines():
        try:
            record = json.loads(line)
        except ValueError:  # a line cut by a kill
            continue
        latest[record['chat'], record['id']] = record  # a message asked again: the newest questions
    lines = [line for line in latest.values() if line['date'] >= since]
    db = sqlite3.connect(DB, uri=True)
    traces = defaultdict(dict)
    for chat, request, trace in db.execute('SELECT chat, request, trace FROM answers WHERE request IS NOT NULL'):
        traces[chat, request] |= setup_tags(json.loads(trace) if trace else {})
    requests = {(chat, request) for chat, request in db.execute('SELECT chat, request FROM answers')}
    addressed = {}
    for path in LOGS:
        for match in RECEIVED.finditer(path.read_text(errors='replace')):
            addressed[int(match[1]), int(match[2])] = match[3] == 'True'
    asked = [line for line in lines if (line['chat'], line['id']) in requests]
    print(f'{len(lines)} messages, {len(asked)} of them requests the bot answered, '
          f'${sum(line["jev"].get("usage", {}).get("cost") or 0 for line in lines):.4f}\n')

    print('Tags on the requests: AUC of Jev against the setup, the threshold that agrees most, the disagreements')
    for name in ('без чата', 'обида', 'клоун', 'реакция', 'о себе', 'тег', 'всем', 'паста', 'найти', 'новости',
                 'справка'):
        pairs = [(line['answers'][name], name in traces[line['chat'], line['id']]) for line in asked]
        score = auc(pairs)
        cut = threshold(pairs)
        missed = [line for line, (x, y) in zip(asked, pairs) if (x >= cut) != y]
        print(f'  {name:9} setup {sum(y for _, y in pairs):3} of {len(pairs)}  AUC {score and round(score, 3)}  '
              f'threshold {cut}  disagree {len(missed)}')
        for line in missed[:8]:
            tag = 'setup yes' if name in traces[line['chat'], line['id']] else 'setup no '
            print(f'      {tag} jev {line["answers"][name]:.2f}  #{line["id"]} {bot.clip(" ".join(line["text"].split()), 90)}')
    forms = [(line['answers'].get('форма'), traces[line['chat'], line['id']].get('форма', 'нет')) for line in asked]
    print(f'  форма     agree {sum(a == b for a, b in forms)} of {len(forms)}; disagree: '
          + ', '.join(f'jev {a} / setup {b}' for a, b in forms if a != b))

    print('\nSaid to Рофл: AUC of «to_rofl» against what the code took for a request, '
          'and the top of the rest')
    known = [line for line in lines if (line['chat'], line['id']) in addressed]
    pairs = [(line['answers']['to_rofl'], addressed[line['chat'], line['id']]) for line in known]
    print(f'  {len(known)} messages in the log, {sum(y for _, y in pairs)} requests, AUC {auc(pairs) and round(auc(pairs), 3)}')
    rest = sorted((line for line in known if not addressed[line['chat'], line['id']]),
                  key=lambda line: -line['answers']['to_rofl'])
    for line in rest[:12]:
        print(f'      {line["answers"]["to_rofl"]:.2f}  #{line["id"]} {line["author"]}: '
              f'{bot.clip(" ".join(line["text"].split()), 90)}')

    print('\nButt in at 0.87, on what nobody asked the bot (a cooldown would cut it further)')
    quiet = [line for line in lines if (line['chat'], line['id']) not in requests
             and not addressed.get((line['chat'], line['id']))]
    top = sorted((line for line in quiet if line['answers']['jump_in'] >= 0.87), key=lambda line: line['date'])
    days = max((max(line['date'] for line in lines) - min(line['date'] for line in lines)) / 86400, 1) if lines else 1
    print(f'  {len(top)} of {len(quiet)} messages, {len(top) / days:.1f} a day')
    for line in sorted(top, key=lambda line: -line['answers']['jump_in'])[:12]:
        print(f'      {line["answers"]["jump_in"]:.2f}  #{line["id"]} {line["author"]}: '
              f'{bot.clip(" ".join(line["text"].split()), 90)}')
    jabs = [line for line in quiet if 'jab' in line['jev']['answers']]
    jab = lambda line: 1 - line['jev']['answers']['jab']['probabilities'].get('нет', 0)
    if jabs:
        print(f'  «jab», 1 − P(нет): {sum(jab(line) >= 0.5 for line in jabs)} of {len(jabs)} at 0.5 or more, the top')
    for line in sorted(jabs, key=lambda line: -jab(line))[:12]:
        hook = line['answers']['jab']
        on = '' if hook in ('нет', str(line['id'])) else f' (on #{hook})'
        print(f'      {jab(line):.2f}  jump_in {line["answers"]["jump_in"]:.2f}  #{line["id"]} {line["author"]}: '
              f'{bot.clip(" ".join(line["text"].split()), 80)}{on}')

    print('\nHow the chat took what the bot said, by Jev on the quarter of an hour after it; «own» is what it said '
          'unasked')
    took = {}
    for line in TAKEN.read_text().splitlines() if TAKEN.exists() else ():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        took[record['chat'], record['id']] = record
    answered = {(chat, id_) for chat, id_ in db.execute('SELECT chat, id FROM answers WHERE request IS NOT NULL')}
    days = defaultdict(lambda: defaultdict(list))
    for record in took.values():
        if record['date'] >= since:
            kind = 'answers' if (record['chat'], record['id']) in answered else 'own'
            days[datetime.date.fromtimestamp(record['date'])][kind].append(record)
    for day in sorted(days):
        parts = []
        for kind, group in sorted(days[day].items()):
            counts = Counter(record['taken'] for record in group)
            laughed = sum((record['probabilities'] or {}).get('ржали', 0) for record in group) / len(group)
            parts.append(f'{kind} {len(group)}: ' + ', '.join(f'{name} {counts[name]}' for name in TAKES)
                         + f'; P(ржали) {laughed:.2f}, replies {sum(r["replies"] for r in group) / len(group):.1f}')
        print(f'  {day:%d.%m}  ' + ' | '.join(parts))


if __name__ == '__main__':
    main()
