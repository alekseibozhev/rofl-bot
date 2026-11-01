"""Try two presentation styles on already generated answers, no history/Telegram."""
import json
import os
from pathlib import Path
import context_compare as base


def main():
    base.bot.load_env()
    b = base.bot.Bot.__new__(base.bot.Bot)
    b.openrouter, b.openrouter_key = True, os.environ['OPENROUTER_API_KEY']
    out = Path(__file__).with_name('muse_formats') / 'formatter.jsonl'
    done = {(r['case'], r['style']) for line in out.read_text().splitlines()
            if not (r := json.loads(line))['info'].get('reason', '').startswith('HTTP')} if out.exists() else set()
    for line in Path(__file__).with_name('muse_samples').joinpath('answers.jsonl').read_text().splitlines():
        source = json.loads(line)
        for style in ('default', 'paragraphs_only'):
            if (source['case'], style) in done:
                continue
            os.environ['FORMATTER_STYLE'] = '' if style == 'default' else 'Не используй жирный шрифт. Только удобные абзацы.'
            b.usage = []
            answer, info = b.format_answer(source['answer'])
            record = {'case': source['case'], 'style': style, 'answer': answer, 'info': info, 'usage': b.usage}
            with out.open('a') as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + '\n')
            print(source['case'], style, json.dumps(info), flush=True)
            if info.get('reason', '').startswith('HTTP'):
                return


if __name__ == '__main__':
    main()
