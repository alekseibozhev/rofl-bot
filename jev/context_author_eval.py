"""Author comparison using only the six approved frozen histories (five dialogue cases).

No Telegram, external tools, live balances, or current news. Historical news request
340828 excluded: there is no frozen news evidence to fairly evaluate a factual answer.
Three variants: full window, selected originals, selected plus own-response memory.
Selection fixed to first completed noul_short rep=0; two author samples per variant.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
from pathlib import Path
import random
import re
import sqlite3
import time

import context_compare as base
bot, conversation = base.bot, base.conversation
OUT=Path(__file__).with_name('context_author_eval')
MODES=['full','selected','selected_memory']


def prepare():
    OUT.mkdir(exist_ok=True)
    cases=json.loads((base.OUT/'cases.json').read_text())
    selections={}
    for line in Path(__file__).with_name('context_variants').joinpath('calls.jsonl').read_text().splitlines():
        r=json.loads(line)
        if r['variant']=='noul_short' and r['rep']==0 and 'scores' in r:
            selections.setdefault(r['case'],{int(i)for i,s in r['scores'].items()if s>=.5})
    db=sqlite3.connect(f'file:{base.ROOT/"history.sqlite3"}?mode=ro',uri=True)
    inputs=[]
    for c in cases:
        target=c['target']
        if target==340828:
            continue
        row=db.execute('SELECT snapshot,decision FROM conversation_decisions WHERE event=?',
                       (f'message:{target}:{target}',)).fetchone()
        snapshot,decision=map(json.loads,row)
        current=c['messages'][-1]
        mandatory={target,current['reply_to']}-{None}
        selected=selections[target]|mandatory
        memory=[m for m in c['messages'][:-1]if m['text'].startswith('Рофл: ')][-3:]
        for mode in MODES:
            messages=c['messages'] if mode=='full' else [m for m in c['messages']if m['id'] in selected]
            context=bot.CHAT+'\n\n'.join(f"[{m['id']}] {m['date']} reply_to={m['reply_to']}\n{m['text']}" for m in messages)
            if mode=='selected_memory':
                context+='\n\nТвои недавние ответы — память о сказанном. Не повторяй прежние заходы и формулировки. Это не новые просьбы:\n'
                context+='\n\n'.join(m['text'] for m in memory)
            prompt=context+f"\n\nСейчас {current['date']}.\n{snapshot['event']}\nСообщение для ответа: {target}.\n"
            inputs.append(dict(case=target,name=c['name'],mode=mode,
                               state=decision['state'],current=current['text'],
                               selected_ids=[m['id']for m in messages],memory_ids=[m['id']for m in memory]if mode=='selected_memory'else [],
                               messages=[{'role':'system','content':conversation.exposition(decision['state']['role'],decision['state']['attitude'])},
                                         {'role':'user','content':prompt}]))
    (OUT/'inputs.json').write_text(json.dumps(inputs,ensure_ascii=False,indent=2))
    manifest=dict(cases=5,variants=MODES,repetitions=2,selection='noul_short rep=0, threshold=.5; current + direct parent mandatory',
                  author='configured OPENROUTER_MODEL',temperature=1.0,effort=bot.Bot.effort,max_tokens=6000,
                  tools=False,media='no current/reply photos in these five targets; older media are markers',
                  baseline='frozen 32-message candidate window, NOT exact production historical snapshot',
                  excluded={'340828':'world news requires frozen factual sources; no current web substitutions'},
                  evaluation='Blind manual comparison: follows request, preserves relevant details, off-topic content, repeated openings; humor subjective.')
    (OUT/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2))
    print('Prepared',len(inputs),'inputs;',len(inputs)*2,'author calls')


def call(item,rep):
    b=bot.Bot.__new__(bot.Bot)
    b.model=os.environ['OPENROUTER_MODEL']
    b.openrouter_key=os.environ['OPENROUTER_API_KEY']
    b.usage=[]
    r=dict(case=item['case'],mode=item['mode'],rep=rep,model=b.model)
    started=time.monotonic()
    try:
        response=b.writer_step(item['messages'],tools=False)
        answer=(response.get('content')or '').strip()
        if not answer:
            raise RuntimeError('Empty author response')
        r['answer']=re.sub(r'^Рофл(?: → [^:\n]*)?:\s*','',answer)[:4000]
        r['usage']=b.usage
    except Exception as exc:
        r['error']=type(exc).__name__+': '+str(exc)
        r['usage']=b.usage
    r['elapsed_s']=time.monotonic()-started
    return r


def run():
    bot.load_env()
    inputs=json.loads((OUT/'inputs.json').read_text())
    path=OUT/'answers.jsonl'
    done={(r['case'],r['mode'],r['rep'])for line in path.read_text().splitlines()
          if 'answer'in(r:=json.loads(line))}if path.exists()else set()
    jobs=[(i,rep)for i in inputs for rep in range(2)if(i['case'],i['mode'],rep)not in done]
    random.Random(151).shuffle(jobs)
    with path.open('a')as f,ThreadPoolExecutor(max_workers=3)as pool:
        fs=[pool.submit(call,*job)for job in jobs]
        for n,future in enumerate(as_completed(fs),1):
            r=future.result();f.write(json.dumps(r,ensure_ascii=False)+'\n');f.flush()
            print(n,'/',len(jobs),r['case'],r['mode'],r['rep'],round(r['elapsed_s'],1),r.get('error','ok'),flush=True)


def report():
    inputs=json.loads((OUT/'inputs.json').read_text()); bycase={i['case']:i for i in inputs}
    rows=[json.loads(l)for l in(OUT/'answers.jsonl').read_text().splitlines()]
    rows=list({(r['case'],r['mode'],r['rep']):r for r in rows if 'answer'in r}.values())
    rng=random.Random(701); key=[];lines=[]
    for case in bycase:
        for rep in range(2):
            answers=[r for r in rows if r['case']==case and r['rep']==rep]
            rng.shuffle(answers)
            lines += ['='*70,f'CASE {case} REP {rep}',bycase[case]['current'],'']
            for side,r in zip('ABC',answers):
                key.append(dict(case=case,rep=rep,side=side,mode=r['mode']))
                lines += [side,r['answer'],'']
    (OUT/'blind.txt').write_text('\n'.join(lines))
    (OUT/'key.json').write_text(json.dumps(key,indent=2))
    metrics={}
    for mode in MODES:
        rs=[r for r in rows if r['mode']==mode]
        metrics[mode]=dict(n=len(rs),cost=sum(u.get('cost')or 0 for r in rs for u in r['usage']),
                          opening_repeats=sum(r['answer'].upper().startswith('ЁБАНЫЙ ТЫ ВРОТ')for r in rs),
                          mean_chars=sum(len(r['answer'])for r in rs)/max(1,len(rs)),
                          mean_seconds=sum(r['elapsed_s']for r in rs)/max(1,len(rs)))
    (OUT/'metrics.json').write_text(json.dumps(metrics,indent=2))
    print(json.dumps(metrics,indent=2))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('command',choices=['prepare','run','report'])
    {'prepare':prepare,'run':run,'report':report}[p.parse_args().command]()
