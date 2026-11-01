"""Compare only can_interject on frozen production decisions; never send to Telegram.

prepare reads SQLite in read-only mode. run calls only Jev (paid OpenRouter calls),
preserving context, explicit, previous attitude and time across both variants.
Raw inputs and results stay in the gitignored experiment directory.
"""
import argparse
from collections import Counter
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import random
import sqlite3
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import bot
import conversation

OUT = Path(__file__).with_name('interjection_comparison')


def prepare(ids):
    OUT.mkdir(exist_ok=True)
    with sqlite3.connect(f'file:{ROOT / "history.sqlite3"}?mode=ro', uri=True) as db:
        # Store's constructor creates tables; these methods only read existing ones.
        store = conversation.Store.__new__(conversation.Store)
        store.db = db
        cases = []
        for target in ids:
            rows = db.execute('''SELECT chat,topic,event,date,snapshot,decision
                FROM conversation_decisions WHERE json_extract(snapshot,'$.target')=?
                AND event LIKE 'message:%' ''', (target,)).fetchall()
            if len(rows) != 1:
                raise ValueError(f'{target}: expected one saved message decision, got {len(rows)}')
            chat, topic, event, date, snapshot, decision = rows[0]
            snapshot, decision = json.loads(snapshot), json.loads(decision)
            if snapshot.get('image_context'):
                raise ValueError(f'{target}: image bytes are not preserved; cannot faithfully replay')
            previous = store.state((chat, topic), before=date)
            previous['attitude'] = store.attitude(chat, snapshot.get('event_user_id'), before=date)
            body = conversation.payload(snapshot, previous)
            body['model'] = decision.get('jev', {}).get('model', conversation.JEV_MODEL)
            last = db.execute('''SELECT max(date) FROM messages WHERE chat=? AND topic=?
                AND role='assistant' AND date<=?''', (chat, topic, date)).fetchone()[0]
            cases.append(dict(target=target, event=event, body=body, historical=decision,
                              seconds_since_bot=None if last is None else date-last,
                              label=next(t['label'] for t in snapshot['targets'] if t['id']==target)))
    path = OUT / 'inputs.json'
    if path.exists():
        raise ValueError('Frozen inputs already exist; use a separate output directory')
    path.write_text(json.dumps(cases, ensure_ascii=False, indent=2))
    print(f'Frozen {len(cases)} cases in {path}')


def run(repeats):
    bot.load_env()
    key = os.environ['OPENROUTER_API_KEY']
    raw = (OUT / 'inputs.json').read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    cases = json.loads(raw)
    path = OUT / 'calls.jsonl'
    records = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    if any(r['input_sha256'] != digest for r in records):
        raise ValueError('Inputs differ from recorded calls')
    done = {(r['target'], r['flag'], r['rep']) for r in records if 'response' in r}
    jobs = [(c, flag, rep) for c in cases for flag in (False, True) for rep in range(repeats)
            if (c['target'], flag, rep) not in done]
    random.Random(20261101).shuffle(jobs)
    with path.open('a') as output:
        for case, flag, rep in jobs:
            body = deepcopy(case['body'])
            body['state']['can_interject'] = flag
            record = dict(target=case['target'], flag=flag, rep=rep, input_sha256=digest)
            started = time.monotonic()
            try:
                record['response'] = bot.post(conversation.JEV_URL, body, timeout=30,
                                              headers={'Authorization': f'Bearer {key}'})
            except RuntimeError as exc:
                record['error'] = str(exc)
            record['elapsed_s'] = round(time.monotonic()-started, 3)
            output.write(json.dumps(record, ensure_ascii=False)+'\n')
            output.flush()
            answers = record.get('response', {}).get('answers', {})
            print(json.dumps(dict(target=case['target'], flag=flag, rep=rep,
                                  action=answers.get('action'), addressed=answers.get('addressed'),
                                  error=record.get('error')), ensure_ascii=False), flush=True)
            if 'error' in record:
                raise RuntimeError('Stopped after first failed call; completed calls are resumable')
    report()


def report():
    cases = json.loads((OUT / 'inputs.json').read_text())
    records = [json.loads(line) for line in (OUT / 'calls.jsonl').read_text().splitlines()]
    summary = []
    for case in cases:
        row = {k: case[k] for k in ('target', 'label', 'seconds_since_bot')}
        row['explicit'] = case['body']['state']['explicit']
        row['historical_action'] = case['historical']['action']
        for flag in (False, True):
            selected = [r['response']['answers'] for r in records
                        if r['target']==case['target'] and r['flag']==flag and 'response' in r]
            row[str(flag).lower()] = dict(Counter(
                f"{a['action']['choice']}; addressed={a['addressed']['choice']}; target={a['target']['choice']}"
                for a in selected))
            row[f'mean_action_probabilities_{str(flag).lower()}'] = {
                action: round(sum(a['action']['probabilities'][action] for a in selected)/len(selected), 4)
                for action in ('ignore', 'reply', 'react')} if selected else {}
        summary.append(row)
    result = dict(cases=summary, calls=sum('response' in r for r in records),
                  cost=sum((r.get('response', {}).get('usage') or {}).get('cost', 0) or 0 for r in records),
                  limitations='Four cases; repeated calls may be cached or deterministic. Raw Jev decisions, before local policy. Previous state reconstructed from history; current prompt frozen at preparation.')
    (OUT / 'summary.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('prepare', 'run', 'report'))
    parser.add_argument('--ids', type=int, nargs='+', default=[382, 383, 384, 385])
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--out', type=Path, default=OUT)
    args = parser.parse_args()
    OUT = args.out
    if args.command == 'prepare':
        prepare(args.ids)
    elif args.command == 'run':
        run(args.repeats)
    else:
        report()
