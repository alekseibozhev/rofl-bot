"""Manual prompt comparison on invented conversations; never reads chat history or calls Telegram.

python3 eval_prompts.py --output /tmp/prompts.json
python3 eval_prompts.py --module /tmp/old_bot.py --output /tmp/prompts-before.json

Uses the configured model (paid API calls), real Jev decisions, role selection and writing, and a temporary database per case.
Web lookup is disabled and reported; this evaluates conversation behaviour, not retrieval accuracy.
No automatic funniness score: read the replies and compare relevance, surprise, repetition and unnecessary tails.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import time


CASES = [
    ('soup', [('user', 'бот, курьер привёз к супу вилку')]),
    ('lamp', [('user', 'бот, умная лампочка требует подписку на свет')]),
    ('continuation', [
        ('user', 'бот, курьер привёз к супу вилку'),
        ('assistant', 'Курьер решил, что бульон надо фильтровать.'),
        ('user', 'бот, я уже полчаса вылавливаю одну морковку'),
        ('assistant', 'Морковка у тебя теперь промысловая рыба.'),
        ('user', 'бот, может через дуршлаг?')]),
    ('explained_reference', [
        ('user', 'бот, мой холодильник опять перешёл в режим стратегического кабачка'),
        ('assistant', 'Он у тебя военный?'),
        ('user', 'бот, стратегическим кабачком я называю единственный овощ, который месяц лежит на полке')]),
    ('feedback', [
        ('user', 'бот, умная лампочка требует подписку на свет'),
        ('assistant', 'Лампочка пошла на курсы финансовой грамотности.'),
        ('user', 'бот, не смешно, слишком натянуто')]),
    ('serious', [('user', 'бот, без шуток и капса объясни HTTP 502 одной фразой')]),
    ('identity', [('user', 'бот, кто ты сейчас?')]),
    ('four_lines', [('user', 'бот, напиши рэп ровно в четыре строки про чайник, который боится воды')]),
    ('own_request', [
        ('user', 'Снова обсуждаем доставку супа и вилку.'),
        ('user', 'бот, сменим тему: придумай короткую абсурдную шутку про лифт')]),
]


def load_module(path):
    spec = importlib.util.spec_from_file_location('evaluated_bot', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def evaluate(module, case):
    name, dialogue = case
    b = module.Bot.__new__(module.Bot)
    b.openrouter = bool(os.environ.get('OPENROUTER_MODEL'))
    b.model = os.environ.get('OPENROUTER_MODEL') or os.environ.get('OLLAMA_MODEL', '')
    b.openrouter_key = os.environ.get('OPENROUTER_API_KEY', '')
    b.ollama_url = os.environ.get('OLLAMA_URL', 'http://localhost:11434').rstrip('/')
    b.me = {'id': 42, 'username': 'synthetic_test_bot'}
    b.allowed_groups = {-1}
    lookups = []

    def no_telegram(*args, **kwargs):
        raise AssertionError('Evaluation must never call Telegram')

    def no_lookup(wanted, *args, **kwargs):
        lookups.append(wanted)
        return None

    b.api = no_telegram
    b.find = no_lookup
    original_tool = b.writer_tool
    def evaluation_tool(name, args, message, snapshot, live):
        if name == 'read_url':
            lookups.append({'read_url': args.get('url')})
            return 'Чтение внешних страниц отключено в этом прогоне.'
        return original_tool(name, args, message, snapshot, live)
    b.writer_tool = evaluation_tool
    # Never supplies chat_id to generate: even sendChatAction is disabled.
    with tempfile.TemporaryDirectory(prefix='rofl-eval-') as directory:
        b.history = module.History(Path(directory) / 'history.sqlite3')
        try:
            now, previous = time.time(), None
            for i, (role, text) in enumerate(dialogue, 1):
                at = now - (len(dialogue) - i) * 30
                message = {'message_id': i, 'chat': {'id': -1, 'type': 'group'}, 'date': at,
                           'from': {'id': 7, 'first_name': 'Участник'}, 'text': text}
                if role == 'assistant':
                    b.history.add((-1, 0), i, role, text, at, i - 1)
                    previous = {'message_id': i, 'from': {'id': 42}, 'text': text}
                else:
                    if previous:
                        message['reply_to_message'] = previous
                    b.history.add_message((-1, 0), message)
                    previous = None
            answer, trace = b.joke(message, message['message_id'], live=False)
            return {'case': name, 'dialogue': dialogue, 'answer': answer, 'trace': trace,
                    'lookup_requests': lookups}
        finally:
            b.history.db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--module', type=Path, default=Path(__file__).with_name('bot.py'))
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--cases', nargs='+', choices=[name for name, _ in CASES])
    parser.add_argument('--workers', type=int, default=2, choices=(1, 2))
    parser.add_argument('--role', help='Optional role name for a controlled comparison; otherwise the model chooses')
    args = parser.parse_args()
    # Load only configuration from the workspace, never Bot.__init__ or the production database.
    import bot
    bot.load_env()
    module = load_module(args.module)
    module.headlines = lambda *args, **kwargs: []
    if args.role:
        module.Bot.choose_role = lambda self, snapshot, previous: args.role
    cases = [case for case in CASES if not args.cases or case[0] in args.cases]
    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for result in pool.map(lambda case: evaluate(module, case), cases):
            results.append(result)
            print(f'{result["case"]}: {result["answer"]}', flush=True)
            args.output.write_text(json.dumps({'module': str(args.module), 'role': args.role, 'synthetic_only': True,
                                              'web_lookup_disabled': True, 'results': results},
                                             ensure_ascii=False, indent=2) + '\n')
    if any(result['trace'] is None for result in results):
        raise SystemExit('Some model calls failed; see the saved results')


if __name__ == '__main__':
    main()
