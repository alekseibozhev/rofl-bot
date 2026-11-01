"""Compare six author reasoning levels on five approved frozen histories.

No Telegram, database, selector, formatter, or external tools. Two repetitions;
same production-format messages, temperature and output ceiling for all efforts.
"""
import argparse
import copy
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import hashlib
import json
import os
from pathlib import Path
import random
import statistics
import time

import context_compare as base
import formatting

OUT = Path(__file__).with_name('muse_reasoning')
SOURCE = Path(__file__).with_name('muse_samples')
MODEL = 'meta/muse-spark-1.3-contributor'
LEVELS = ['minimal', 'low', 'medium', 'high', 'xhigh', 'max']
TARGETS = [366, 340798, 340800, 340823, 340830]
LIMIT = 12000  # Same ceiling for every effort, including medium control.
CAP = .08
RESERVE = .003  # 12k output * $0.20/M + <=6k input * $0.10/M.


def prepare():
    OUT.mkdir(exist_ok=True)
    for target in TARGETS:
        fixture = json.loads((SOURCE / f'input_{target}.json').read_text())
        b = base.bot.Bot.__new__(base.bot.Bot)
        b.openrouter = True
        captured = []
        def capture(messages, tools=True):
            captured.extend(copy.deepcopy(messages))
            return {'role': 'assistant', 'content': 'capture only'}
        b.writer_step = capture
        b.write_turn({}, fixture['snapshot'], fixture['decision'], live=False)
        (OUT / f'input_{target}.json').write_text(json.dumps(captured, ensure_ascii=False, indent=2))
    (OUT / 'design.json').write_text(json.dumps({
        'model': MODEL, 'efforts': LEVELS, 'targets': TARGETS, 'repetitions': 2,
        'temperature': 1, 'max_tokens': LIMIT, 'concurrency': 3, 'spend_cap': CAP,
        'criteria': ['Answers current request', 'Tracks speakers and references',
                     'Preserves requested persona', 'Avoids invented balance/topups/facts',
                     'Avoids repetition and excessive length'],
        'quality_notes': 'Blind review of visible answers only; small sample, subjective persona assessment.',
        'references': ['https://dev.meta.ai/docs/reasoning',
                       'https://openrouter.ai/docs/guides/best-practices/reasoning-tokens'],
        'capability_note': 'OpenRouter lists max and accepted synthetic max request; Meta direct-API documentation excludes max for contributor.'
    }, ensure_ascii=False, indent=2))
    print('Prepared five frozen inputs; no network.')


def call(job):
    target, effort, rep = job
    messages = json.loads((OUT / f'input_{target}.json').read_text())
    encoded = json.dumps(messages, ensure_ascii=False, sort_keys=True).encode()
    record = {'case': target, 'effort': effort, 'rep': rep, 'model': MODEL,
              'input_sha256': hashlib.sha256(encoded).hexdigest(), 'max_tokens': LIMIT}
    started = time.monotonic()
    try:
        response = base.bot.post('https://openrouter.ai/api/v1/chat/completions',
            {'model': MODEL, 'messages': messages, 'temperature': 1,
             'max_tokens': LIMIT, 'reasoning': {'effort': effort}}, timeout=240,
            headers={'Authorization': 'Bearer ' + os.environ['OPENROUTER_API_KEY']})
        choice = (response.get('choices') or [{}])[0]
        answer = (choice.get('message') or {}).get('content') or ''
        record.update(answer=answer, clean_answer=formatting.clean_prefix(answer),
                      finish_reason=choice.get('finish_reason'), usage=response.get('usage') or {},
                      response_model=response.get('model'), provider=response.get('provider'))
        if not answer.strip() or choice.get('finish_reason') != 'stop':
            record['error'] = 'Incomplete answer: ' + str(choice.get('finish_reason'))
    except RuntimeError as exc:
        record['error'] = str(exc)
    record['elapsed_s'] = round(time.monotonic() - started, 3)
    return record


def records():
    path = OUT / 'answers.jsonl'
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def report():
    rows = records()
    summary = {}
    for effort in LEVELS:
        all_rows = [r for r in rows if r['effort'] == effort]
        valid = [r for r in all_rows if not r.get('error')]
        if not valid:
            continue
        reason = [(r.get('usage', {}).get('completion_tokens_details') or {}).get('reasoning_tokens') for r in valid]
        summary[effort] = {
            'complete': len(valid), 'errors': len(all_rows) - len(valid),
            'total_cost': sum(r.get('usage', {}).get('cost') or 0 for r in all_rows),
            'mean_cost': statistics.mean(r.get('usage', {}).get('cost') or 0 for r in valid),
            'median_seconds': statistics.median(r['elapsed_s'] for r in valid),
            'mean_chars': round(statistics.mean(len(r['clean_answer']) for r in valid)),
            'mean_reasoning_tokens': round(statistics.mean(reason)) if all(isinstance(n, int) for n in reason) else None,
            'above_production_6000_tokens': sum(r.get('usage', {}).get('completion_tokens', 0) > 6000 for r in valid),
            'metadata_prefix': sum(r['answer'].strip() != r['clean_answer'] for r in valid),
        }
    (OUT / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    rng, key, blind, full = random.Random(31847), {}, [], []
    for target in TARGETS:
        group = [r for r in rows if r['case'] == target]
        rng.shuffle(group)
        request = next(r['request'] for r in map(json.loads, (SOURCE / 'answers.jsonl').read_text().splitlines()) if r['case'] == target)
        blind += [f'CASE {target}', request, '']
        full += [f'CASE {target}', request, '']
        for i, r in enumerate(group):
            label = f'{target}-{chr(65+i)}'
            key[label] = {'case': target, 'effort': r['effort'], 'rep': r['rep']}
            blind += [label, r.get('clean_answer') or r.get('error', ''), '']
        for r in sorted(group, key=lambda r: (LEVELS.index(r['effort']), r['rep'])):
            full += [f"{r['effort']} / повтор {r['rep']}", r.get('clean_answer') or r.get('error', ''), '']
    (OUT / 'blind.txt').write_text('\n'.join(blind))
    (OUT / 'key.json').write_text(json.dumps(key, indent=2))
    (OUT / 'comparison.txt').write_text('\n'.join(full))
    print(json.dumps(summary), flush=True)


def run():
    base.bot.load_env()
    old = records()
    done = {(r['case'], r['effort'], r['rep']) for r in old}
    probe = json.loads((OUT / 'max_probe.json').read_text())
    assert probe['accepted'], 'max probe must succeed before historical calls'
    spent = sum(r.get('usage', {}).get('cost') or 0 for r in old) + (probe.get('usage') or {}).get('cost', 0)
    jobs = []
    for rep in (1, 2):
        block = [(case, effort, rep) for case in TARGETS for effort in LEVELS]
        random.Random(771 + rep).shuffle(block)
        jobs.extend(job for job in block if job not in done)
    pending, stopped = {}, False
    with ThreadPoolExecutor(max_workers=3) as pool:
        while jobs or pending:
            while jobs and len(pending) < 3 and not stopped:
                if spent + (len(pending) + 1) * RESERVE > CAP:
                    stopped = True
                    print('Stopped scheduling at conservative cost cap', flush=True)
                    break
                job = jobs.pop(0)
                pending[pool.submit(call, job)] = job
            if not pending:
                break
            finished, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in finished:
                pending.pop(future)
                r = future.result()
                with (OUT / 'answers.jsonl').open('a') as stream:
                    stream.write(json.dumps(r, ensure_ascii=False) + '\n')
                spent += r.get('usage', {}).get('cost') or 0
                print(r['case'], r['effort'], r['rep'], r.get('error', 'ok'), r['elapsed_s'], flush=True)
                if r.get('error', '').startswith('HTTP'):
                    stopped = True
            if stopped:
                jobs.clear()
    report()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['prepare', 'run', 'report'])
    globals()[parser.parse_args().action]()
