"""Two synthetic smoke cases, real Jev/author, fixture tools, no Telegram or chat export.

python3 jev/prefetch_eval.py --out /tmp/prefetch-eval
Only literal invented messages below are used. Production history is never opened.
"""
import argparse
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import bot


def run(out):
    out.mkdir(exist_ok=False, parents=True)
    bot.load_env()
    b = bot.Bot.__new__(bot.Bot)
    b.openrouter = True
    b.tool_judge_enabled = True
    b.openrouter_key = os.environ['OPENROUTER_API_KEY']
    b.model = os.environ['OPENROUTER_MODEL']
    b.me = {'id': 42, 'username': 'test_bot', 'first_name': 'Рофл'}
    b.image = lambda *a: None
    b.send_typing = lambda *a: None
    def blocked(*a, **kw):
        raise RuntimeError('Telegram is disabled')
    b.api = blocked
    b.format_answer = lambda answer: (answer, {'skipped': True, 'evaluation': True})
    attempts = []
    def fixture(name, args, *a, **kw):
        attempts.append({'tool': name, 'arguments': args})
        if name == 'skok_deneg':
            return b.balance_phrase({'available_usd': 4.67})
        return json.dumps({'error': 'Only fixture money tool is available in this synthetic evaluation'})
    b.execute_tool = fixture
    requests = []
    real_step = b.writer_step
    def writer(messages, tools=False):
        requests.append(copy.deepcopy(messages))
        return real_step(messages, tools=tools)
    b.writer_step = writer
    records = []
    with tempfile.TemporaryDirectory() as tmp:
        b.history = bot.History(Path(tmp) / 'history.sqlite3')
        for i, text in enumerate(('бот, сколько у тебя денег?', 'бот, поясни нормально, я ничего не понял')):
            message = {'chat': {'id': -1, 'type': 'supergroup'}, 'message_id': i * 2 + 1,
                       'date': int(time.time()), 'from': {'id': 7, 'first_name': 'Участник'}, 'text': text}
            if i:
                message['reply_to_message'] = {'message_id': 2, 'from': b.me, 'text': records[-1]['answer']}
            b.history.add_message((-1, 0), message)
            attempts.clear()
            requests.clear()
            started = time.monotonic()
            answer, trace = b.joke(message, message['message_id'], live=True)
            record = {'request': text, 'answer': answer, 'trace': trace, 'attempts': copy.deepcopy(attempts),
                      'author_calls': len(requests), 'elapsed_s': round(time.monotonic() - started, 3),
                      'fixture_balance_usd': 4.67, 'synthetic': True}
            records.append(record)
            (out / 'results.json').write_text(json.dumps(records, ensure_ascii=False, indent=2))
            (out / f'{i+1}.messages.json').write_text(json.dumps(requests, ensure_ascii=False, indent=2))
            assert len(requests) == 1
            if i:
                assert trace.get('tool_history_count') == 1
                assert 'ДЕНЕГ ТОЛЬКО НА ЕДУ И КОММУНАЛКУ' in requests[0][-1]['content']
                assert 'для сообщения #1' in requests[0][-1]['content']
            assert all(m['role'] in ('system', 'user', 'assistant') for m in requests[0])
            b.history.add((-1, 0), message['message_id'] + 1, 'assistant', answer,
                          message['date'] + .01, message['message_id'])
            print(json.dumps(record, ensure_ascii=False), flush=True)
        b.history.db.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    run(parser.parse_args().out)
