"""Summarize the frozen historical retrieval experiment without further API calls."""
import collections
import json
from pathlib import Path
import statistics

OUT = Path(__file__).with_name('context_comparison')
cases = json.loads((OUT / 'cases.json').read_text())
rows = [json.loads(line) for line in (OUT / 'calls.jsonl').read_text().splitlines()]
valid = [r for r in rows if 'response' in r]
answers = {}
for r in valid:
    for i, a in r['response']['answers'].items():
        answers[r['case'], r['mode'], r['rep'], int(i)] = a

report = ['Jev context selection: 6 historical requests, 32 candidates each, 2 repetitions.',
          'Partial manual rubric was frozen before API calls; unlabelled messages are ambiguous, not negatives.',
          'All modes see the current request and its reply chain. Full modes additionally see all candidates.',
          'Batch = 32 keep/drop questions in one request, not a single free-form list selection.',
          'Texts are from read-only history, without future messages. Images are text markers only.',
          'This measures selection, not generated response quality. No author or Telegram calls.',
          'Latency below is per API request under 6 concurrent workers; not end-to-end bot latency.', '']
summary = {}
for mode in ['isolated', 'individual_full', 'batch']:
    calls = [r for r in valid if r['mode'] == mode]
    tp = fn = fp = tn = selected = changes = comparisons = protected_tp = 0
    for c in cases:
        for rep in range(2):
            kept = {i for (target, m, n, i), a in answers.items()
                    if (target, m, n) == (c['target'], mode, rep) and a['choice'] == 'keep'}
            selected += len(kept)
            tp += len(kept & set(c['relevant']))
            fn += len(set(c['relevant']) - kept)
            fp += len(kept & set(c['noise']))
            tn += len(set(c['noise']) - kept)
            protected_tp += len((kept | set(c['anchor_ids'])) & set(c['relevant']))
        for message in c['messages'][:-1]:
            key = (c['target'], mode)
            a = answers.get((*key, 0, message['id']))
            b = answers.get((*key, 1, message['id']))
            if a and b:
                comparisons += 1
                changes += a['choice'] != b['choice']
    summary[mode] = {
        'calls': len(calls), 'cost_usd': sum(r['response'].get('usage', {}).get('cost', 0) for r in calls),
        'median_request_s': statistics.median(r['elapsed_s'] for r in calls) if calls else None,
        'total_request_seconds': sum(r['elapsed_s'] for r in calls),
        'mean_selected_of_32': selected / 12, 'useful_kept': tp, 'useful_total': tp + fn,
        'noise_kept': fp, 'noise_total': fp + tn, 'useful_kept_with_forced_chain': protected_tp,
        'repeat_choice_changes': changes, 'repeat_comparisons': comparisons}
    report += [mode, json.dumps(summary[mode], indent=2), '']

for c in cases:
    report += ['=' * 80, f"CASE {c['target']}: {c['name']}",
               'CURRENT: ' + c['messages'][-1]['text'],
               'Mandatory reply chain IDs: ' + str(c['anchor_ids']),
               'Columns: isolated / individual_full / batch; each shows keep probability in repeats 1,2.', '']
    for m in c['messages'][:-1]:
        vals = []
        for mode in ['isolated', 'individual_full', 'batch']:
            probs = [answers.get((c['target'], mode, rep, m['id']), {}).get('probabilities', {}).get('keep')
                     for rep in range(2)]
            vals.append('/'.join('?' if p is None else f'{p:.2f}' for p in probs))
        label = 'USEFUL' if m['id'] in c['relevant'] else 'NOISE' if m['id'] in c['noise'] else 'unlabelled'
        report += [f"{m['id']} [{label}] {' | '.join(vals)}", m['text'], '']

report += ['Initial sandbox-denied network attempt: ' + str(sum('error' in r for r in rows)),
           'Successful API calls: ' + str(len(valid)),
           'Total reported cost USD: ' + str(sum(v['cost_usd'] for v in summary.values()))]
(OUT / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2))
(OUT / 'report.txt').write_text('\n'.join(report))
print(json.dumps(summary, ensure_ascii=False, indent=2))
