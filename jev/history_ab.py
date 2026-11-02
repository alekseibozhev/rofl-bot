"""A/B: same short clarification, with or without selected older chat history.

Requires explicit permission to send the selected saved history to OpenRouter.
No Telegram, tools, live DB writes, or changes to the running bot.
"""
import argparse
import copy
import datetime
import json
import os
from pathlib import Path
import sqlite3
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import bot
import conversation


def run(out):
    out.mkdir(exist_ok=False, parents=True)
    with sqlite3.connect(f'file:{ROOT / "history.sqlite3"}?mode=ro', uri=True) as source:
        stored, selection = map(json.loads, source.execute(
            'SELECT snapshot,decision FROM conversation_decisions WHERE event=?',
            ('message:340933:340933',)).fetchone())
    approved_example = json.loads((ROOT / 'jev/prefetch-synthetic-01/results.json').read_text())
    now = time.time()
    user = {'kind': 'user', 'id': 7, 'name': 'Участник', 'is_bot': False}
    def stamp(t):
        return datetime.datetime.fromtimestamp(t).isoformat()
    common = [
        {'id': 1, 'role': 'user', 'reply_to': None, 'date': stamp(now - 60),
         'content': '[1] Участник: бот, сколько у тебя денег?', 'author': 'Участник',
         'message_text': 'бот, сколько у тебя денег?', 'participants': [user]},
        {'id': 2, 'role': 'assistant', 'reply_to': 1, 'date': stamp(now - 40),
         'content': '[2] Рофл: ' + approved_example[0]['answer'], 'assistant_text': approved_example[0]['answer']},
        {'id': 3, 'role': 'user', 'reply_to': 2, 'date': stamp(now),
         'content': '[3] Участник: бот, поясни нормально, я ничего не понял', 'author': 'Участник',
         'message_text': 'бот, поясни нормально, я ничего не понял', 'participants': [user]}]
    kept = set(selection['context_selection']['ids'])
    old = [m for m in stored['messages'] if m['id'] in kept]
    bot.load_env()
    b = bot.Bot.__new__(bot.Bot)
    b.openrouter = True
    b.openrouter_key = os.environ['OPENROUTER_API_KEY']
    b.model = os.environ['OPENROUTER_MODEL']
    b.me = {'id': 8962336348, 'username': 'aleksei_ollama_rofl_bot'}
    b.image = lambda *a: None
    records = []
    for label, history in [('A', []), ('B', old)]:
        turns = copy.deepcopy(history + common)
        snapshot = {'messages': turns, 'context': bot.CHAT + '\n\n'.join(m['content'] for m in turns),
                    'participants': (copy.deepcopy(stored['participants']) if history else []) + [user],
                    'event': 'Сообщение 3, пишет Участник', 'target': 3, 'through': 3, 'date': now}
        decision = {'action': 'reply', 'target': 3, 'reply_size': 'один абзац',
                    'context_selection': {'ids': [m['id'] for m in turns]}, 'tool_results': [], 'tool_history': []}
        captured = []
        b.writer_step = lambda messages, tools=False: (captured.append(copy.deepcopy(messages)),
                                                       {'role': 'assistant', 'content': 'capture'})[1]
        b.write_turn({}, snapshot, decision, live=False)
        messages = captured[0]
        (out / f'{label}.messages.json').write_text(json.dumps(messages, ensure_ascii=False, indent=2))
        b.usage = []
        started = time.monotonic()
        reply = bot.Bot.writer_step(b, messages, tools=False)
        answer = (reply.get('content') or '').strip()
        if not answer or reply.get('tool_calls'):
            raise RuntimeError('Expected a nonempty author response without tools')
        record = {'variant': label, 'old_history': bool(history), 'old_messages': len(history),
                  'common_messages': len(common), 'request': common[-1]['message_text'], 'answer': answer,
                  'system': conversation.exposition(), 'reply_size': decision['reply_size'],
                  'tool_results': [], 'past_tool_results': [], 'author_calls': 1,
                  'elapsed_s': round(time.monotonic() - started, 3), 'usage': b.usage}
        records.append(record)
        (out / 'results.json').write_text(json.dumps(records, ensure_ascii=False, indent=2))
        print(json.dumps(record, ensure_ascii=False), flush=True)
    lines = ['A/B: один запрос, промпт и длина; результаты инструментов отсутствуют в обоих вариантах.',
             'A: короткая цепочка из 3 реплик. B: те же 3 + 13 выбранных старых реплик про деньги.',
             'Jev-отбор старой истории переиспользован из сохранённого решения 340933.', '']
    for r in records:
        lines += [r['variant'], r['request'], '', r['answer'], '']
    (out / 'report.txt').write_text('\n'.join(lines))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', required=True, type=Path)
    run(parser.parse_args().out)
