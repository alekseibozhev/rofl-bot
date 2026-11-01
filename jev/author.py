"""A prototype of one writer with tools, against the setup and punchline of bot.py, on stored requests.

The writer always sees the chat, as the punchline does without [без чата]; what the setup gave by its tags it takes
itself, by calling a tool: an original or a reference found on the web, the news, how a form looks, what it knows of
itself, whom to tag, an emoji instead of words. Each request is answered twice, by `Bot.joke` as a replay (live=False)
and by the writer, with the same chat, the same said answers, the same tone and picture.

The database is opened read-only, nothing is sent to Telegram but getMe and, for a picture, getFile.
Usage: author.py [--workers N] [--out FILE] [ids...]   (ids as chat:message or message; default the 50 + #192)
"""
import argparse, datetime, importlib.util, json, re, sqlite3, sys, threading, time
from concurrent.futures import ThreadPoolExecutor
from itertools import dropwhile
from pathlib import Path

HERE = Path(__file__).parent
GOLD = Path('/private/tmp/claude-501/-Users-macacos-Sync-bot/0e731762-3f35-4f3d-898d-5e600d41725a/scratchpad/jev/items.json')
BUDGET = 0.60
AUTHOR_ONLY = False  # --author-only: the current answers are taken from runs.json

spec = importlib.util.spec_from_file_location('botcopy', '/Users/macacos/Sync/bot/bot.py')
bot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bot)
bot.__file__ = '/Users/macacos/Sync/bot/bot.py'
bot.load_env()


def ro_init(self, _path):
    self.db = sqlite3.connect('file:/Users/macacos/Sync/bot/history.sqlite3?mode=ro', uri=True)


bot.History.__init__ = ro_init

TOOLS_TEXT = """

Инструменты — прежде чем писать, реши, что из них нужно этому ответу; не нужно ничего — вызови «ничего»:
- найти(что, вид): вид «оригинал» — текст известной пасты, стиха, песни, мема или цитаты, когда его цитируют, просят \
продолжить, дополнить, переделать, ответить в стиле или по шаблону: по памяти такие тексты путаются; цитату передавай \
дословно. Вид «справка» — пара строк, что это за мем, персонаж или отсылка, если не знаешь.
- новости(запрос): заголовки за сутки из Google News — когда речь о свежем событии, просят доложить обстановку, что \
нового в мире, обсудить новости или навести суету: о свежих событиях ты без них не знаешь. Без запроса — главное в \
мире и политике.
- форма(имя): как выглядит гринтекст, бугурт, рэп, стих или код — когда просят эту форму; батл — рэп, частушка — стих.
- о_себе(): что ты знаешь о себе и как ты сегодня — только когда спрашивают про тебя самого.
- тегнуть(имена): тегнуть людей чата — только когда просят тегнуть, позвать или потегать; «все» — всех.
- реакция(эмодзи): поставить эмодзи вместо ответа словами — когда тебе только поддакнули или поржали и словами \
отвечать незачем. Тогда больше ничего не пиши.

Длина: одна-две строки. Просят расписать, рассказать, доложить — 8–15 строк одна под другой, пустая строка только \
между абзацами. У формы и найденного оригинала длина своя. Просят своё, не про этот чат — анекдот, стих или объяснение \
о постороннем, — переписку в ответ не тащи."""
AUTHOR = bot.SYSTEM + TOOLS_TEXT


def function(name, description, properties=None, required=()):
    return {'type': 'function', 'function': {'name': name, 'description': description, 'parameters': {
        'type': 'object', 'properties': properties or {}, 'required': list(required)}}}


TOOLS = [
    function('найти', 'Оригинал известного текста или справка о меме из интернета.',
             {'что': {'type': 'string', 'description': 'что искать; цитату — дословно'},
              'вид': {'type': 'string', 'enum': ['оригинал', 'справка']}}, ['что', 'вид']),
    function('новости', 'Заголовки новостей за сутки.',
             {'запрос': {'type': 'string', 'description': 'о чём; пусто — главное в мире и политике'}}),
    function('форма', 'Как выглядит форма ответа.',
             {'имя': {'type': 'string', 'enum': list(bot.FORMS)}}, ['имя']),
    function('о_себе', 'Что ты знаешь о себе и как ты сегодня.'),
    function('тегнуть', 'Тегнуть людей чата в ответе.',
             {'имена': {'type': 'array', 'items': {'type': 'string'}, 'description': 'имена как в переписке или «все»'}},
             ['имена']),
    function('реакция', 'Поставить эмодзи на сообщение вместо ответа словами.',
             {'эмодзи': {'type': 'string'}}, ['эмодзи']),
    function('ничего', 'Ничего из инструментов этому ответу не нужно: пишешь сразу.'),
]

local = threading.local()
lock = threading.Lock()
spent_total = [0.0]


def the_bot():
    if not hasattr(local, 'bot'):
        local.bot = bot.Bot(read_only=True)
        assert local.bot.openrouter
    return local.bot


def ask(b, messages, effort='high', choice='auto'):
    payload = {'model': b.model, 'messages': messages, 'temperature': 1.0, 'tools': TOOLS, 'tool_choice': choice,
               'max_tokens': 6000, 'reasoning': {'effort': effort}}
    started = time.monotonic()
    result = bot.post('https://openrouter.ai/api/v1/chat/completions', payload, timeout=180,
                      headers={'Authorization': f'Bearer {b.openrouter_key}'})
    if 'error' in result:
        raise RuntimeError(f'openrouter: {result["error"]}')
    choice = (result.get('choices') or [{}])[0]
    usage = result.get('usage') or {}
    return choice.get('message') or {}, {'cost': usage.get('cost') or 0, 'elapsed_s': round(time.monotonic() - started, 2),
                                         'finish': choice.get('finish_reason')}


def clean(answer, chat, speakers, form):
    """The cleanup of `Bot.joke`: lines copied from the chat and names off the front, the log the model went on with."""
    name = re.compile(rf'^(?i:{"|".join(map(re.escape, speakers))})(?: → [^:\n]*)?:\s*')
    known = {name.sub('', line).lower() for line in chat.splitlines()
             if name.match(line) and not line.startswith('Рофл')} | {''}
    kept = '\n'.join(dropwhile(lambda line: name.sub('', line).strip().lower() in known, answer.splitlines()))
    answer = name.sub('', kept or answer) or answer
    answer = re.split(rf'\n\s*\n(?={name.pattern[1:]})', answer, maxsplit=1)[0]
    answer = re.sub(r'^\\>', '>', '\n'.join(line.rstrip() for line in answer.splitlines()), flags=re.M)
    return answer if form else bot.casual(answer)


def write(b, message, through):
    """The writer's answer and what it did: the tools it called with their arguments, the cost."""
    chat_id, topic = message['chat']['id'], bot.topic_of(message)
    key, sender, date = (chat_id, topic), bot.sender_of(message), message.get('date', time.time())
    lines, names = b.chat(key, through)
    chat = bot.CHAT + '\n\n'.join(lines)
    talk = [line for line in lines if not line.startswith('— ')]
    heads = [re.split(r' → |: ', line, maxsplit=1)[0] for line in talk]
    answers = [bot.clip(' '.join(text.split()), 200) for text in b.history.said(key, through, date, count=6)]
    said = (('\n\nТы недавно ответил, не повторяй ни детали, ни заход, ни построение; что ты обыгрывал не '
             'раз, заезжено, не бери, даже если чат об этом:\n' + '\n'.join(f'- {text}' for text in answers))
            if answers else '')
    image = b.image(message)
    seeing = chat
    if image and not (message.get('photo') or message.get('sticker')):
        before, found, after = chat.rpartition(bot.BLIND)
        seeing = before + 'В исходном сообщении есть фото, оно приложено.' + after if found else chat
    people = {user: name for user, name in names.items() if not user.endswith('bot')}
    manner = bot.tone(datetime.date.fromtimestamp(date))
    now = datetime.datetime.fromtimestamp(date)
    prompt = (seeing + said + f'\n\nСейчас {bot.WEEKDAYS[now.weekday()]}, {now:%H:%M}. Последнее сообщение адресовано '
              f'тебе, пишет {bot.person_label(sender)}.' + (f' {manner}' if manner else '') + ' Ответь одной репликой')
    messages = [{'role': 'system', 'content': AUTHOR}, {'role': 'user', 'content': bot.with_image(prompt, image)}]
    calls, cost, elapsed, tagged, form = [], 0.0, 0.0, [], None
    for round_ in range(4):
        # The first step is a choice of the tools, «ничего» among them: left to itself it took none of them.
        reply, stats = ask(b, messages, choice='required' if round_ == 0 else 'auto')
        cost, elapsed = cost + stats['cost'], elapsed + stats['elapsed_s']
        tool_calls = reply.get('tool_calls') or []
        if not tool_calls:
            break
        messages.append({k: v for k, v in reply.items() if k in ('role', 'content', 'tool_calls', 'reasoning_details')})
        for call in tool_calls:
            fn = call['function']['name']
            try:
                args = json.loads(call['function'].get('arguments') or '{}')
            except ValueError:
                args = {}
            # It named the arguments in English now and then: «name», «names».
            english = {'what': 'что', 'kind': 'вид', 'query': 'запрос', 'name': 'имя', 'names': 'имена', 'emoji': 'эмодзи'}
            args = {english.get(k, k): v for k, v in args.items()}
            calls.append({'tool': fn, 'args': args})
            if fn == 'реакция':
                emoji = bot.reaction_of(f'[реакция {args.get("эмодзи", "")}]')
                return f'[реакция {emoji}]', {'tools': calls, 'cost': round(cost, 6), 'elapsed_s': round(elapsed, 2),
                                              'image': bool(image)}
            result = run_tool(b, fn, args, lines, people, chat_id, date, message)
            if fn == 'тегнуть':
                tagged = result[1]
                result = result[0]
            if fn == 'форма' and args.get('имя') in bot.FORMS:
                form = args['имя']
            calls[-1]['result_chars'] = len(result)
            messages.append({'role': 'tool', 'tool_call_id': call['id'], 'content': result})
    else:
        reply = {'content': ''}
    answer = (reply.get('content') or '').strip()
    if not answer:
        # Out of rounds or tokens: once more, the tools taken away.
        messages.append({'role': 'user', 'content': 'Ответь одной репликой, без инструментов.'})
        reply, stats = ask(b, messages[:1] + messages[1:], effort='low')
        cost, elapsed = cost + stats['cost'], elapsed + stats['elapsed_s']
        answer = (reply.get('content') or '').strip()
    answer = clean(answer, chat, {'Рофл', bot.person_label(sender), *heads}, form)
    trace = {'tools': calls, 'cost': round(cost, 6), 'elapsed_s': round(elapsed, 2), 'image': bool(image)}
    return bot.envelope(answer, {'tagged': tagged}), trace


def run_tool(b, fn, args, lines, people, chat_id, date, message):
    if fn == 'найти':
        kind = 'справка' if args.get('вид') == 'справка' else 'найти'
        found = b.find(args.get('что', ''), None, kind, lines[-1])
        if not found:
            return 'Ничего не нашлось.'
        return (f'Из интернета:\n{found}\n\n' + ('Это тебе для понимания, справку не пересказывай.' if kind == 'справка'
                else 'Бери найденное узнаваемо и не пересказывай, откуда оно. Переделываешь под просьбу — первая строка, '
                     'строй, капс и знаки как в оригинале, своё с нуля не пиши.'))
    if fn == 'новости':
        asked = (args.get('запрос') or '').strip()
        try:
            fresh = bot.headlines(asked) if asked else bot.headlines(None)[:8] + bot.headlines(bot.POLITICS)[:7]
        except RuntimeError as exc:
            return f'Новости не загрузились: {exc}'
        titles = list(dict.fromkeys(title for title, _ in fresh))[:15]
        if not titles:
            return 'Новостей не нашлось.'
        return bot.NEWS.format(about=f' ({asked})' if asked else ' мира',
                               titles='\n'.join(f'- {t}' for t in titles)) + bot.NEWS_TASK.strip()
    if fn == 'форма':
        name = args.get('имя')
        return f'Как выглядит {name}: {bot.FORMS[name]}' if name in bot.FORMS else f'Форм: {", ".join(bot.FORMS)}.'
    if fn == 'о_себе':
        feelings = b.feelings(chat_id, date, message)
        return bot.SELF + (f'\n\n{feelings}' if feelings else '') + '\n\nОтвечай одной-двумя строками, держись этого.'
    if fn == 'тегнуть':
        names = args.get('имена') or []
        tagged = bot.tagged_of(f'[тег {", ".join(names)}]', people)
        if not tagged:
            return f'Таких нет. В чате: {", ".join(dict.fromkeys(people.values()))}.', []
        return (f'Тегнуты: {", ".join(dict.fromkeys(people[u] for u in tagged))} — обращайся к ним по этим именам, '
                '@ не пиши.'), tagged
    if fn == 'ничего':
        return 'Пиши ответ.'
    return 'Нет такого инструмента.'


def current(b, message, through):
    """`Bot.joke` as `--replay` runs it, with its cost."""
    started = time.monotonic()
    answer, trace = b.joke(message, through, live=False)
    trace = trace or {}
    shown = f'[реакция {answer}]' if trace.get('reaction') else bot.envelope(answer, trace)
    return shown, {'tags': trace.get('tags'), 'overruled': trace.get('overruled'), 'alone': trace.get('alone'),
                   'cost': trace.get('cost'), 'elapsed_s': round(time.monotonic() - started, 2),
                   'clown': trace.get('clown')}


def one(chat, message_id):
    b = the_bot()
    row = b.history.db.execute("SELECT raw FROM messages WHERE chat=? AND id=? AND role='user'",
                               (chat, message_id)).fetchone()
    if not row:  # the live bot keeps 200 messages a chat: an old request is gone
        return None
    message = json.loads(row[0])
    later = [json.loads(r) for r, in b.history.db.execute(
        "SELECT raw FROM messages WHERE chat=? AND topic=? AND role='user' AND id > ? ORDER BY id LIMIT 10",
        (chat, bot.topic_of(message), message_id))]
    extra = bot.followups(message, later)
    through = extra[-1]['message_id'] if extra else message_id
    lines, _ = b.chat((chat, bot.topic_of(message)), through)
    out = {'chat': chat, 'id': message_id, 'request': bot.render(row[0], b.me['id']),
           'before': [line for line in lines[-7:-1]]}
    for name, fn in (('current', current), ('author', write)) if not AUTHOR_ONLY else (('author', write),):
        try:
            out[name], out[name + '_trace'] = fn(b, message, through)
        except Exception as exc:
            out[name], out[name + '_trace'] = f'ОШИБКА: {exc!r}', {}
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--workers', type=int, default=6)
    parser.add_argument('--out', default=str(HERE / 'runs.json'))
    parser.add_argument('--author-only', action='store_true', help='only the writer; the current answers stay in --out')
    parser.add_argument('ids', nargs='*')
    args = parser.parse_args()
    global AUTHOR_ONLY
    AUTHOR_ONLY = args.author_only
    if args.ids:
        todo = [(int(i.split(':')[0]), int(i.split(':')[1])) if ':' in i else (None, int(i)) for i in args.ids]
    else:
        todo = [(o['chat'], o['request']) for o in json.load(open(GOLD))] + [(-5130851759, 192)]
    db = sqlite3.connect('file:/Users/macacos/Sync/bot/history.sqlite3?mode=ro', uri=True)
    todo = [(c if c is not None else db.execute("SELECT chat FROM messages WHERE id=? AND role='user'", (i,)).fetchone()[0], i)
            for c, i in todo]
    try: results = json.load(open(args.out))
    except FileNotFoundError: results = {}
    todo = [(c, i) for c, i in todo if f'{c}:{i}' not in results]
    base = json.load(open(HERE / 'runs.json')) if AUTHOR_ONLY else {}
    todo = [(c, i) for c, i in todo if not AUTHOR_ONLY or f'{c}:{i}' in base]

    def job(item):
        if spent_total[0] > BUDGET:
            return None
        out = one(*item)
        if out is None:
            print(item[1], 'gone from the database', flush=True)
            return None
        if AUTHOR_ONLY:
            out = {**base[f'{item[0]}:{item[1]}'], **out}
        cost = sum((out.get(k) or {}).get('cost') or 0 for k in ('current_trace', 'author_trace'))
        with lock:
            spent_total[0] += cost
            results[f'{item[0]}:{item[1]}'] = out
            json.dump(results, open(args.out, 'w'), ensure_ascii=False, indent=1)
            tools = ','.join(c['tool'] for c in out['author_trace'].get('tools', [])) or '-'
            print(f"{item[1]} ${cost:.4f} total ${spent_total[0]:.3f} | tags {out['current_trace'].get('tags')!r} | "
                  f"tools {tools}", flush=True)
        return out

    with ThreadPoolExecutor(args.workers) as pool:
        list(pool.map(job, todo))
    print(f'{len(results)} done, this run ${spent_total[0]:.3f}')


if __name__ == '__main__':
    main()
