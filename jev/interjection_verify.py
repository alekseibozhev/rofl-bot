"""Exercise the current participation policy with real Jev and Spark, without Telegram.

Uses four historical snapshots read-only plus synthetic cooldown/quiet-hours cases.
Each run uses a temporary database. Only the two OpenRouter model endpoints are
allowed; Telegram, typing, delivery and writer tools are disabled. Results stay local.
"""
from copy import deepcopy
import datetime
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import bot
import conversation

OUT = Path(__file__).with_name('interjection_verification')


def forbidden(*args, **kwargs):
    raise AssertionError('Telegram and delivery are forbidden in this evaluation')


def historical():
    cases = []
    with sqlite3.connect(f'file:{ROOT / "history.sqlite3"}?mode=ro', uri=True) as db:
        store = conversation.Store.__new__(conversation.Store)
        store.db = db
        for target in (382, 383, 384, 385):
            chat, topic, raw = db.execute('SELECT chat,topic,raw FROM messages WHERE id=?', (target,)).fetchone()
            snapshot, _ = store.get((chat, topic), f'message:{target}:{target}')
            if snapshot.get('image_context'):
                raise ValueError('Images cannot be replayed from a text-only saved snapshot')
            previous = store.state((chat, topic), before=snapshot['date'])
            previous['attitude'] = store.attitude(chat, snapshot.get('event_user_id'), before=snapshot['date'])
            last = db.execute('''SELECT max(date) FROM messages WHERE chat=? AND topic=?
                AND role='assistant' AND date<=?''', (chat, topic, snapshot['date'])).fetchone()[0]
            daytime = bot.Bot.can_react(snapshot['date'])
            snapshot['can_react'] = snapshot['explicit'] or daytime
            snapshot['can_interject'] = snapshot['explicit'] or (daytime and (
                last is None or snapshot['date'] - last >= bot.Bot.interjection_pause))
            cases.append(dict(name=f'history_{target}', snapshot=snapshot, previous=previous, message=json.loads(raw)))
    return cases


def synthetic():
    return [dict(name=name, text=text, elapsed=elapsed, hour=hour, reply=reply, joke=joke)
            for name, text, elapsed, hour, reply, joke in [
                ('explicit_during_pause', 'бот, сколько будет 17 плюс 25? Ответь одним числом.', 30, 12, False, False),
                ('reply_during_pause', 'А сколько будет 17 плюс 25? Ответь одним числом.', 30, 12, True, False),
                ('implicit_during_pause', 'Рофлуша, спасибо за ответ, а 17 плюс 25 сколько?', 30, 12, False, False),
                ('reaction_during_pause', 'ахахаха, Дима, отличная шутка 😂', 30, 12, False, True),
                ('background_4m59', 'Ладно, я отойду налить воды.', 299, 12, False, False),
                ('background_5m00', 'Ладно, я отойду налить воды.', 300, 12, False, False),
                ('reaction_at_night', 'ахахаха, Дима, отличная шутка 😂', 30, 2, False, True),
                ('explicit_at_night', 'бот, сколько будет 17 плюс 25? Ответь одним числом.', 30, 2, False, False),
                ('reaction_milestone', 'Ребята, я защитил диплом на отлично! 🎉', 30, 12, False, False),
                ('reaction_release', 'УРАААА! Наконец-то выкатили релиз без единого падения! 🎉', 30, 12, False, False),
                ('reaction_thanks_to_human', 'Дима, спасибо, ты меня сегодня очень выручил! ❤️', 30, 12, False, False),
            ]]


def evaluate(case, repetition):
    b = bot.Bot.__new__(bot.Bot)
    b.openrouter = True
    b.openrouter_key = os.environ['OPENROUTER_API_KEY']
    b.model = os.environ.get('OPENROUTER_MODEL', 'meta/muse-spark-1.3-contributor')
    b.me = {'id': 42, 'username': 'synthetic_test_bot'}
    b.api = b.send_typing = b.send = b.deliver = b.dispatch = forbidden
    b.writer_tool = lambda *args, **kwargs: 'Инструмент отключён в локальной проверке.'
    calls, decisions = [], []
    original_post, original_direct = bot.post, b.direct

    def guarded_post(url, payload, **kwargs):
        if url not in (conversation.JEV_URL, 'https://openrouter.ai/api/v1/chat/completions'):
            raise AssertionError('Only Jev and Spark requests are allowed')
        result = original_post(url, payload, **kwargs)
        calls.append(dict(model=payload['model'], usage=result.get('usage'),
                          answers=result.get('answers')))
        return result

    def direct(snapshot, previous):
        decision = original_direct(snapshot, previous)
        decisions.append(dict(state=conversation.payload(snapshot, previous)['state'], decision=deepcopy(decision)))
        return decision

    b.direct = direct
    with tempfile.TemporaryDirectory(prefix='interjection-verify-') as directory:
        b.history = bot.History(Path(directory) / 'history.sqlite3')
        try:
            if 'snapshot' in case:
                snapshot, previous = deepcopy(case['snapshot']), case['previous']
                message = case['message']
                key = (message['chat']['id'], bot.topic_of(message))
                b.snapshot = lambda *args, **kwargs: deepcopy(snapshot)
                prior = dict(state=previous, relationship=dict(user_id=snapshot['event_user_id'], attitude=previous['attitude']))
                with b.history.db:
                    b.history.db.execute('INSERT INTO conversation_decisions VALUES (?,?,?,?,?,?)',
                                         (*key, 'prior', snapshot['date']-1, '{}', json.dumps(prior)))
                b.choose_role = lambda *args: previous['role']
            else:
                at = datetime.datetime(2026, 11, 1, case['hour']).timestamp()
                b.history.add((-1, 0), 1, 'assistant', 'Если нужен расчёт, могу помочь.', at-case['elapsed'])
                if case['joke']:
                    b.history.add_message((-1, 0), dict(message_id=2, chat={'id': -1}, date=at-2,
                        text='Купил умный чайник. Теперь он кипятится из-за ипотеки вместе со мной.',
                        **{'from': {'id': 8, 'first_name': 'Дима'}}))
                message = dict(message_id=3, chat={'id': -1, 'type': 'group'}, date=at, text=case['text'],
                               **{'from': {'id': 7, 'first_name': 'Алексей'}})
                if case['reply']:
                    message['reply_to_message'] = {'message_id': 1, 'from': b.me, 'text': 'Если нужен расчёт, могу помочь.'}
                b.history.add_message((-1, 0), message)
                b.choose_role = lambda *args: conversation.NEUTRAL_ROLE
            before = '\n'.join(b.history.db.iterdump())
            with patch.object(bot, 'post', guarded_post):
                answer, trace = b.joke(message, message['message_id'], live=False)
            assert before == '\n'.join(b.history.db.iterdump()), 'Replay changed its database'
            assert not trace['fallback'], 'Jev failed; fallback is not a valid evaluation'
            state, decision = decisions[0]['state'], decisions[0]['decision']
            if state['explicit']:
                assert state['can_interject'] and state['can_react']
            if not state['explicit'] and not decision['addressed']:
                assert trace['action'] != 'reply' or state['can_interject']
                assert trace['action'] != 'react' or state['can_react']
            return dict(case=case['name'], rep=repetition, answer=answer, trace=trace,
                        decision=decision, flags={k: state[k] for k in ('explicit', 'can_interject', 'can_react')},
                        calls=calls, database_unchanged=True, telegram_calls=0)
        finally:
            b.history.db.close()


if __name__ == '__main__':
    bot.load_env()
    OUT.mkdir(exist_ok=True)
    inputs = OUT / 'inputs.json'
    if not inputs.exists():
        inputs.write_text(json.dumps(historical()+synthetic(), ensure_ascii=False, indent=2))
    cases = json.loads(inputs.read_text())
    path = OUT / 'results.jsonl'
    done = {(r['case'], r['rep']) for line in path.read_text().splitlines()
            if (r := json.loads(line))} if path.exists() else set()
    for case in cases:
        for repetition in range(3 if case['name'].startswith('history_') else 1):
            if (case['name'], repetition) in done:
                continue
            result = evaluate(case, repetition)
            with path.open('a') as output:
                output.write(json.dumps(result, ensure_ascii=False)+'\n')
            print(json.dumps({k: result[k] for k in ('case', 'rep', 'flags', 'answer')}, ensure_ascii=False), flush=True)
