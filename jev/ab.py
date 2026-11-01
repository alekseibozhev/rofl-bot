"""A blind look at the 50 pairs of the author prototype: the bot as it is against one writer with tools, the same
request on the same chat. `build` writes ab.html, a pair a screen with the sides in a random order, and ab_key.json,
which side is which; `judge` asks Jev the better of each pair both ways round, to see whether it picks as you do;
`score PICKS` reads the string the page gives back.
Usage: ab.py build | judge | score PICKS | text BATCH
Another set of pairs, such as the hybrid's: AB_SET=ab2 AB_RUNS=hybrid_runs.json ab.py build, then AB_SET=ab2 for the rest.
"""
import json, os, random, sys
from collections import Counter
from pathlib import Path

from shadow import HERE, MODEL, bot, post

RUNS = Path(os.environ.get('AB_RUNS', HERE / 'author_runs2.json'))
SET = os.environ.get('AB_SET', 'ab')
PAIRS, KEY, JUDGED, PAGE = (HERE / f'{SET}_pairs.json', HERE / f'{SET}_key.json', HERE / f'{SET}_jev.json',
                            HERE / f'{SET}.html')
JUDGE = ('Rofl is a rude, witty joke bot in this Russian-language group chat of friends. Here is the end of the chat '
         'and two replies Rofl could give to its last message. Which one would the chat like more: the funnier one '
         'that answers what was asked and stays on its topic?')
PICKS = {'A': 'A лучше', 'B': 'B лучше', '=': 'одинаково', 'x': 'оба плохо'}
DIGITS = {'1': 'A', '2': 'B', '3': '=', '4': 'x', '0': '?', 'a': 'A', 'b': 'B', 'X': 'x',
          'А': 'A', 'а': 'A', 'В': 'B', 'в': 'B', 'Б': 'B', 'б': 'B', 'х': 'x', 'Х': 'x'}


def request_of(run):
    return run['request'].replace(bot.BLIND, '').strip()  # the note is for the model, it does not see the photo


def build():
    runs = json.loads(RUNS.read_text())
    PAIRS.write_text(json.dumps(runs, ensure_ascii=False))  # the scratchpad goes with a reboot
    # Half the pairs one way round, half the other: a liking for the left one counts for neither.
    orders = [('current', 'author'), ('author', 'current')] * (len(runs) // 2)
    orders += [('current', 'author')] * (len(runs) % 2)
    random.Random(20261022).shuffle(orders)
    items, key = [], []
    for (name, run), sides in zip(runs.items(), orders):
        items.append({'before': run['before'], 'request': request_of(run), 'A': run[sides[0]], 'B': run[sides[1]]})
        key.append({'pair': name, 'A': sides[0], 'B': sides[1]})
    KEY.write_text(json.dumps(key, ensure_ascii=False, indent=1))
    data = json.dumps(items, ensure_ascii=False).replace('<', '\\u003c')  # no «</script>» or «<!--» in the page
    page = (HERE / 'ab_page.html').read_text().replace('{{set}}', SET)  # a set of as many pairs gets its own picks
    PAGE.write_text(page.replace('{{items}}', data))
    print(f'{PAGE}: {len(items)} pairs')


def judge():
    bot.load_env()
    runs, judged, spent = json.loads(PAIRS.read_text()), {}, 0.0
    for name, run in runs.items():
        for first, second in (('current', 'author'), ('author', 'current')):
            answer, elapsed = post({'model': MODEL, 'state': {'chat': run['before'] + [request_of(run)]},
                                    'questions': {'better': {'type': 'choice', 'instructions': JUDGE,
                                                             'criteria': {'A': run[first], 'B': run[second]}}}})
            spent += answer.get('usage', {}).get('cost') or 0
            p = answer['answers']['better']['probabilities']
            judged.setdefault(name, []).append(p['A'] if first == 'current' else p['B'])
        print(name, 'P(current)', judged[name], flush=True)
    JUDGED.write_text(json.dumps(judged, indent=1))
    print(f'${spent:.4f}')


def score(picks):
    key, judged = json.loads(KEY.read_text()), json.loads(JUDGED.read_text()) if JUDGED.exists() else {}
    # From the chat: digits, or the Cyrillic look-alikes of a phone keyboard.
    picks = ''.join(DIGITS.get(c, c) for c in picks if not c.isspace())
    if len(picks) != len(key):
        sys.exit(f'{len(picks)} picks for {len(key)} pairs')
    won, agree, told = Counter(), 0, 0
    for k, pick in zip(key, picks):
        winner = k.get(pick, PICKS.get(pick, 'пропущено'))
        won[winner] += 1
        current = sum(judged.get(k['pair'], [0.5])) / len(judged.get(k['pair'], [0.5]))  # both ways round
        if pick in 'AB' and k['pair'] in judged and current != 0.5:
            told += 1
            agree += (current > 0.5) == (winner == 'current')
        print(f'  {k["pair"]:22} {winner:10} Jev P(current) {current:.2f}')
    print(dict(won))
    if told:
        print(f'Jev picked as you did on {agree} of {told}')


def text(batch, size=10):
    """A batch of pairs as Markdown for the chat, the sides as on the page: the phone app shows ab.html without
    running its script."""
    runs, key = json.loads(PAIRS.read_text()), json.loads(KEY.read_text())
    for n, k in enumerate(key[(batch - 1) * size:batch * size], (batch - 1) * size + 1):
        run = runs[k['pair']]
        print(f'**{n}.**  ')
        for line in run['before'][-3:]:
            print(f'… {bot.clip(" ".join(line.split()), 160)}  ')
        print(f'**{bot.clip(" ".join(request_of(run).split()), 400)}**\n')
        for side in 'AB':
            print(f'**{side}**\n' + '\n'.join('> ' + line for line in run[k[side]].splitlines()) + '\n')
        print('---\n')


if __name__ == '__main__':
    if sys.argv[1:2] == ['score']:
        score(sys.argv[2])
    elif sys.argv[1:2] == ['text']:
        text(int(sys.argv[2]))
    else:
        {'build': build, 'judge': judge}[sys.argv[1]]()
