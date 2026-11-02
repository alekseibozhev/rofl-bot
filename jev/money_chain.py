"""Replay real money questions with generated feedback; isolated DB, fixture tools.

python3 jev/money_chain.py --label harry-01
Each label creates a new local artifact directory; never sends Telegram messages.
"""
import argparse
from copy import deepcopy
import datetime
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import bot
import conversation

IDS = (340883, 340885, 340887, 340889, 340891, 340893, 340896, 340899)


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2))


def run(label, short=False, live_search=False):
    ids = (340883, 340887) if short else IDS
    if not label or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789-_' for c in label):
        raise ValueError('Use a lowercase label with digits, hyphens or underscores')
    out = ROOT / 'jev' / ('money-chain-' + label)
    out.mkdir(exist_ok=False)
    with sqlite3.connect(f'file:{ROOT / "history.sqlite3"}?mode=ro', uri=True) as source:
        snap, saved = map(json.loads, source.execute(
            'SELECT snapshot,decision FROM conversation_decisions WHERE event=?',
            (f'message:{ids[0]}:{ids[0]}',)).fetchone())
        chat = source.execute('SELECT chat FROM messages WHERE id=?', (ids[0],)).fetchone()[0]
        incoming = [json.loads(source.execute('SELECT raw FROM messages WHERE chat=? AND id=?',
                                             (chat, mid)).fetchone()[0]) for mid in ids]
    bot.load_env()
    b = bot.Bot.__new__(bot.Bot)
    b.openrouter = True
    b.tool_judge_enabled = True
    b.openrouter_key = os.environ['OPENROUTER_API_KEY']
    b.model = os.environ['OPENROUTER_MODEL']
    b.image = lambda *a: None
    b.send_typing = lambda *a: None
    def blocked(*a, **kw):
        raise RuntimeError('Telegram disabled in money-chain evaluation')
    b.api = blocked
    b.format_answer = lambda answer: (answer, {'skipped': True, 'evaluation': True})
    metadata = dict(label=label, model=b.model, effort=b.effort, repetition_penalty=1.1, tool_judge=True,
                    system=conversation.exposition('puchkov', 7), input_ids=ids,
                    architecture='direct writer; actual joke/direct/selection/write_turn',
                    fixtures='balance=4.67 USD; search/read_url live' if live_search else 'balance=4.67 USD; all other tools unavailable',
                    scope='text money chain; unrelated image interlude omitted; original pre-chain history retained')
    dump(out / 'metadata.json', metadata)
    dump(out / 'input.json', {'snapshot': snap, 'incoming': incoming})
    for name in ('bot.py', 'conversation.py', 'personas.py', 'tooluse.py'):
        (out / name).write_text((ROOT / name).read_text())
    turns = deepcopy(snap['messages'][:-1])
    targets = deepcopy(snap['targets'][:-1])
    results = []
    with tempfile.TemporaryDirectory() as tmp:
        b.history = bot.History(Path(tmp) / 'history.sqlite3')
        store = conversation.Store(b.history.db)
        # Seed the historical initial relationship, without modifying production state.
        for uid, attitude in snap['attitudes'].items():
            b.history.db.execute('INSERT INTO participant_attitudes VALUES (?,?,?,?)',
                                 (chat, int(uid), attitude, snap['date'] - 1))
        b.history.db.commit()
        store.set_role_override('puchkov', snap['date'] - 1, incoming[-1]['date'] + 1)
        for original in incoming:
            message = deepcopy(original)
            mid, user = message['message_id'], message['from']
            text = message.get('text', '')
            parent = message.get('reply_to_message', {}).get('message_id')
            if short and mid != ids[0]:
                parent = ids[0] + 1
            # Never leak old answers from Telegram's embedded reply quotations.
            if parent:
                prior = next((m for m in turns if m['id'] == parent), None)
                message['reply_to_message'] = {'message_id': parent, 'text': prior.get('assistant_text', prior['content']) if prior else ''}
            name = user.get('first_name', '') + (f" (@{user['username']})" if user.get('username') else '')
            date = datetime.datetime.fromtimestamp(message['date']).isoformat()
            turns.append(dict(id=mid, role='user', content=f'[{mid}] {name}: {text}',
                              date=date, reply_to=parent, participants=[user]))
            targets.append(dict(id=mid, label=f'{name}: {text}', user_id=user['id']))
            current = {**deepcopy(snap), 'messages': deepcopy(turns), 'targets': deepcopy(targets),
                       'context': bot.CHAT + '\n\n'.join(m['content'] for m in turns),
                       'target': mid, 'through': mid, 'date': message['date'],
                       'event_user_id': user['id'], 'event': f'Сообщение {mid}',
                       'explicit': True, 'can_interject': True}
            b.snapshot = lambda *a, **kw: deepcopy(current)
            attempted = []
            def fixture(name, args, *a, **kw):
                attempted.append({'name': name, 'args': args})
                if live_search and name in ('sprosit', 'pochitat'):
                    result = bot.Bot.execute_tool(b, name, args, *a, **kw)
                    attempted[-1]['result'] = result
                    return result
                if name == 'skok_deneg':
                    return b.balance_phrase({'available_usd': 4.67})
                return json.dumps({'error': 'External tool unavailable in historical test'}, ensure_ascii=False)
            b.execute_tool = fixture
            print(f'START {mid}: {text}', flush=True)
            try:
                answer, trace = b.joke(message, mid, live=True)
            except Exception as exc:
                dump(out / 'error.json', {'target': mid, 'error': str(exc)})
                raise
            frozen, decision = store.get((chat, 0), f'message:{mid}:{mid}')
            record = dict(target=mid, request=text, answer=answer, trace=trace,
                          tool_attempts=attempted, decision=decision, snapshot=frozen)
            results.append(record)
            dump(out / 'results.json', results)
            print(json.dumps({k: record[k] for k in ('target', 'answer', 'tool_attempts')}, ensure_ascii=False), flush=True)
            if answer and trace['action'] == 'reply':
                turns.append(dict(id=mid+1, role='assistant', content=f'[{mid+1}] Рофл: {answer}',
                                  assistant_text=answer, date=date, reply_to=mid))
                targets.append(dict(id=mid+1, label=f'Рофл: {answer}', user_id=None))
        b.history.db.close()
    report = [f'# Money chain: {label}', '', f'Model: {b.model}. Balance fixture: $4.67.', '']
    for r in results:
        t = r['trace']
        report.extend([f'## {r["target"]}: {r["request"]}', '',
                       f'Intent: {t.get("intent")}; attitude: {t.get("attitude")}; planning: {t.get("planning")}; tools: {r["tool_attempts"]}',
                       '', r['answer'] or '(silence)', ''])
    (out / 'report.md').write_text('\n'.join(report))
    print('REPORT', out / 'report.md', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--label', required=True)
    parser.add_argument('--short', action='store_true', help='Only the initial money question and its repeat')
    parser.add_argument('--system', help='Override the author system prompt for this process only')
    parser.add_argument('--live-search', action='store_true')
    args = parser.parse_args()
    if args.system is not None:
        conversation.exposition = lambda *a, **kw: args.system
    run(args.label, args.short, args.live_search)
