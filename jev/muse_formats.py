"""Offline formatting experiment: frozen approved inputs, no Telegram, no tools.

Reuses Jev selections; only writer presentation changes. Does not read the live DB.
Prepare writes reviewable API messages; run makes at most 20 paid author calls.
"""
import argparse
import copy
import datetime
import json
import os
from pathlib import Path
import re
import time

import context_compare as base

bot = base.bot
SOURCE = Path(__file__).with_name('muse_samples')
OUT = Path(__file__).with_name('muse_formats')
TARGETS = [366, 340798, 340800, 340823, 340830]
VARIANTS = ['compact', 'instruction', 'clean_local', 'clean_index']
MODEL = 'meta/muse-spark-1.3-contributor'


def stamp(value):
    return datetime.datetime.fromisoformat(value).strftime('%d.%m.%Y %H:%M')


def make_messages(target, variant):
    fixture = json.loads((SOURCE / f'input_{target}.json').read_text())
    snapshot, decision = fixture['snapshot'], fixture['decision']
    b = bot.Bot.__new__(bot.Bot)
    b.openrouter = True
    captured = []
    def capture(messages, tools=True):
        captured.extend(copy.deepcopy(messages))
        return {'role': 'assistant', 'content': 'capture only'}
    b.writer_step = capture
    b.write_turn({}, snapshot, decision, live=False)
    if variant == 'instruction':
        captured[0]['content'] += ('\nВ ответе пиши только текст своей реплики. Даты, ID сообщений, '
                                   'имена перед двоеточием и заголовки истории — служебные метаданные, '
                                   'их не нужно воспроизводить.')
        return captured
    selected = [t for t in snapshot['messages'] if t['id'] in decision['context_selection']['ids']]
    index = '\n'.join(f"id={t['id']}; {stamp(t['date'])}; role={t['role']}; reply_to={t['reply_to']}" for t in selected)
    if variant == 'clean_index':
        captured[1]['content'] = 'Метаданные сообщений истории по порядку:\n' + index + '\n\n' + captured[1]['content']
    for turn in selected:
        original = f"Время сообщения: {turn['date']}\n{turn['content']}"
        compact = f"[{stamp(turn['date'])}] {turn['content']}"
        for pos, message in enumerate(captured):
            if original not in message['content']:
                continue
            if turn['role'] == 'assistant' and variant.startswith('clean_'):
                # Authorship comes from the fixture role, never from display names.
                clean, count = re.subn(r'^\[\d+\] Рофл(?: → [^:\n]*)?:\s*', '', turn['content'], count=1)
                assert count == 1, (target, turn['id'])
                message['content'] = message['content'].replace(original, clean, 1)
                if variant == 'clean_local':
                    metadata = f"Метаданные следующей реплики Рофла: id={turn['id']}, {stamp(turn['date'])}, reply_to={turn['reply_to']}."
                    if captured[pos - 1]['role'] == 'user':
                        captured[pos - 1]['content'] += '\n\n' + metadata
                    else:
                        captured.insert(pos, {'role': 'user', 'content': metadata})
            else:
                message['content'] = message['content'].replace(original, compact, 1)
            break
        else:
            raise AssertionError(('Missing turn', target, turn['id']))
    return captured


def prepare():
    OUT.mkdir(exist_ok=True)
    for target in TARGETS:
        for variant in VARIANTS:
            messages = make_messages(target, variant)
            (OUT / f'input_{target}_{variant}.json').write_text(json.dumps(messages, ensure_ascii=False, indent=2))
    print('Prepared 20 inputs from the same five frozen histories. No network.')


def report():
    path = OUT / 'answers.jsonl'
    rows = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    baseline = [dict(json.loads(line), variant='baseline') for line in (SOURCE / 'answers.jsonl').read_text().splitlines()]
    lines = ['Muse format experiment. Same frozen Jev selection, historical state and writer settings.',
             'Baseline reused from prior run; one sample per case/variant; no Telegram or tools.', '']
    summaries = {}
    for variant in ['baseline'] + VARIANTS:
        group = [r for r in baseline + rows if r['variant'] == variant and 'answer' in r]
        summaries[variant] = {
            'n': len(group),
            'metadata_leak': sum(bool(re.search(r'Время сообщения:|\[\d{3,}\]|\[\d{2}\.\d{2}\.\d{4}|^Рофл:|Метаданные|reply_to=', r['answer'])) for r in group),
            'mean_chars': round(sum(len(r['answer']) for r in group) / len(group)) if group else None,
        }
    for target in TARGETS:
        lines += [f'CASE {target}', next(r['request'] for r in baseline if r['case'] == target), '']
        for r in baseline + rows:
            if r['case'] == target:
                lines += [r['variant'], r.get('answer', r.get('error', '')), '']
    cost = sum(u.get('cost') or 0 for r in rows for u in r.get('usage', []))
    (OUT / 'report.txt').write_text('\n'.join(lines))
    (OUT / 'summary.json').write_text(json.dumps({'variants': summaries, 'new_cost': cost}, indent=2))
    print(json.dumps({'variants': summaries, 'new_cost': cost}), flush=True)


def run():
    bot.load_env()
    b = bot.Bot.__new__(bot.Bot)
    b.model, b.openrouter_key = MODEL, os.environ['OPENROUTER_API_KEY']
    path = OUT / 'answers.jsonl'
    previous = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    done = {(r['case'], r['variant']) for r in previous}
    spent = sum(u.get('cost') or 0 for r in previous for u in r.get('usage', []))
    # Rotate variant order to reduce order bias; sequential calls stop immediately on errors.
    for i, target in enumerate(TARGETS):
        for variant in VARIANTS[i % 4:] + VARIANTS[:i % 4]:
            if (target, variant) in done:
                continue
            if spent >= 0.02:
                print('Stopped at $0.02 local experiment cap', flush=True)
                report()
                return
            messages = json.loads((OUT / f'input_{target}_{variant}.json').read_text())
            b.usage = []
            record = {'case': target, 'variant': variant, 'model': MODEL}
            start = time.monotonic()
            try:
                reply = b.writer_step(messages, tools=False)
                record['answer'] = reply.get('content') or ''
                if reply.get('tool_calls') or not record['answer'].strip():
                    raise RuntimeError('Unexpected empty answer or tool calls')
            except RuntimeError as exc:
                record['error'] = str(exc)
            record.update(usage=b.usage, elapsed_s=round(time.monotonic() - start, 2))
            spent += sum(u.get('cost') or 0 for u in b.usage)
            with path.open('a') as output:
                output.write(json.dumps(record, ensure_ascii=False) + '\n')
            print(target, variant, record.get('error', 'ok'), record['elapsed_s'], flush=True)
            if 'error' in record:
                report()
                return
    report()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['prepare', 'run', 'report'])
    args = parser.parse_args()
    globals()[args.action]()
