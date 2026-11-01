"""Muse examples on approved frozen histories; no Telegram or external tools.

Runs current Jev selector and current writer with native user/assistant history.
Historical roles/attitudes are fixed. Tools are disabled for comparable dialogue samples.
"""
import copy
import json
import os
from pathlib import Path
import sqlite3
import time

import context_compare as base
bot, conversation = base.bot, base.conversation
OUT = Path(__file__).with_name('muse_samples')
TARGETS = [366, 340798, 340800, 340823, 340830]


def main():
    bot.load_env()
    assert os.environ['OPENROUTER_MODEL'] == 'meta/muse-spark-1.3-contributor'
    OUT.mkdir(exist_ok=True)
    cases = {c['target']: c for c in json.loads((base.OUT / 'cases.json').read_text())}
    db = sqlite3.connect(f'file:{base.ROOT / "history.sqlite3"}?mode=ro', uri=True)
    path = OUT / 'answers.jsonl'
    done = {r['case'] for line in path.read_text().splitlines() if 'answer' in (r := json.loads(line))} if path.exists() else set()
    for target in TARGETS:
        if target in done:
            continue
        c = cases[target]
        chat, topic, raw = db.execute('SELECT chat,topic,raw FROM messages WHERE id=?', (target,)).fetchone()
        message = json.loads(raw)
        assert not any(message.get(k) or message.get('reply_to_message', {}).get(k) for k in ('photo', 'sticker'))
        snapshot_json, decision_json = db.execute('SELECT snapshot,decision FROM conversation_decisions WHERE chat=? AND topic=? AND event=?',
                                                  (chat, topic, f'message:{target}:{target}')).fetchone()
        snapshot, decision = json.loads(snapshot_json), json.loads(decision_json)
        turns = []
        for m in c['messages']:
            role, = db.execute('SELECT role FROM messages WHERE chat=? AND topic=? AND id=?', (chat, topic, m['id'])).fetchone()
            turns.append({'id': m['id'], 'date': m['date'], 'reply_to': m['reply_to'], 'role': role,
                          'content': f"[{m['id']}] {m['text']}"})
        snapshot.update(messages=turns, context=bot.CHAT + '\n\n'.join(m['content'] for m in turns),
                        reactions_context='', target=target, through=target)
        decision['target'] = target
        decision['action'] = 'reply'
        b = bot.Bot.__new__(bot.Bot)
        b.model = os.environ['OPENROUTER_MODEL']
        b.openrouter = True
        b.openrouter_key = os.environ['OPENROUTER_API_KEY']
        b.usage = []
        def no_telegram(*args, **kwargs):
            raise AssertionError('Telegram disabled in offline samples')
        b.api = b.image = b.writer_tool = no_telegram
        b.writer_step = lambda messages, tools=True: bot.Bot.writer_step(b, messages, tools=False)
        start = time.monotonic()
        record = {'case': target, 'request': message.get('text', ''), 'model': b.model,
                  'state': decision['state'], 'note': 'Frozen 32-message window; historical state; native roles; current Jev selection; tools disabled.'}
        try:
            try:
                selection = b.select_context(snapshot, target)
                decision['context_selection'] = selection
                if selection.get('jev'):
                    b.usage.append(selection['jev'])
            except RuntimeError as exc:
                decision['context_selection'] = {'fallback': True}
                record['selection_error'] = str(exc)
            record['selection'] = decision['context_selection']
            # Save exact inputs for review; no mutation of live history or decisions.
            (OUT / f'input_{target}.json').write_text(json.dumps({'snapshot': snapshot, 'decision': decision}, ensure_ascii=False, indent=2))
            record['answer'], _ = b.write_turn(message, snapshot, decision, live=False)
        except RuntimeError as exc:
            record['error'] = str(exc)
        record['usage'] = b.usage
        record['elapsed_s'] = round(time.monotonic() - start, 2)
        with path.open('a') as output:
            output.write(json.dumps(record, ensure_ascii=False) + '\n')
        print(target, record.get('error', 'ok'), record['elapsed_s'], flush=True)
        if 'error' in record:
            break
    records = [json.loads(line) for line in path.read_text().splitlines()]
    lines = ['Muse Spark 1.3 Contributor. Historical role: Глад Валакас.',
             'Current native roles and Jev selection, frozen 32-message windows, tools disabled. No Telegram.', '']
    for r in records:
        lines += [f"#{r['case']} — отношение {r['state']['attitude']}/10", r['request'], '',
                  r.get('answer', r.get('error', '')), '', '=' * 60, '']
    cost = sum(u.get('cost') or 0 for r in records for u in r.get('usage', []))
    lines.append(f'Total reported API cost: ${cost:.6f}')
    (OUT / 'examples.txt').write_text('\n'.join(lines))
    print('Reported cost', cost)


if __name__ == '__main__':
    main()
