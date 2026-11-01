"""The author with Jev's marks: one writer with tools as in author.py, but what it got wrong there is decided before
it writes, by Jev's answers in the shadow (shadow.jsonl). Asked about itself (0 of 6 times it took the card), it gets
the card of what it knows of itself; asked for a paste («рейдж пасту на 20 строк» went a rap), the length and no form
to pick; asked about everyone (the second chat's погоняла got three names), the roster, the whole chat tagged.

Each request is answered by `Bot.joke` as a replay and by this writer, on one snapshot of the database, read-only;
nothing goes to Telegram but getMe, getChatMemberCount and, for a picture, getFile. The answers go to hybrid_runs.json
as pairs for ab.py, the writer's under «author».
Usage: hybrid.py [--workers N] [ids...]   (ids as chat:message; default the 50 the marks fire on)
"""
import argparse, datetime, json, re, sqlite3, sys, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import author
from author import bot

HERE = Path(__file__).resolve().parent
SNAPSHOT = Path('/private/tmp/claude-501/-Users-macacos-Sync-bot/39fcaec3-7317-4b81-a7c0-f5868e119e50/scratchpad/'
                'snapshot.sqlite3')
OUT = HERE / 'hybrid_runs.json'
# Guessed on the backlog, before any labels: a card too many costs nothing, a paste or the whole chat tagged does.
SELF_AT, LONG_AT, ALL_AT = 0.35, 0.6, 0.7
LONG = ' Распиши на 8–15 строк, строки одна под другой, пустая строка только между абзацами.'

TOOL_LINES = {
    'найти': author.TOOLS_TEXT.split('\n- ')[1],
    'новости': author.TOOLS_TEXT.split('\n- ')[2],
    'форма': author.TOOLS_TEXT.split('\n- ')[3],
    'тегнуть': author.TOOLS_TEXT.split('\n- ')[5],
    'реакция': author.TOOLS_TEXT.split('\n- ')[6].split('\n\n')[0],
}
HEAD = author.TOOLS_TEXT.split('\n- ')[0]
TAIL = '\n\n' + author.TOOLS_TEXT.split('\n\n')[-1]


def ro_snapshot(self, _path):
    self.db = sqlite3.connect(f'file:{SNAPSHOT}?mode=ro', uri=True)


def marks():
    """Jev's answers by «chat:id», the newest questions."""
    found = {}
    for line in (HERE / 'shadow.jsonl').read_text().splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        found[f'{record["chat"]}:{record["id"]}'] = record['answers']
    return found


def ask(b, messages, tools, effort='high', choice='auto'):
    payload = {'model': b.model, 'messages': messages, 'temperature': 1.0, 'tools': tools, 'tool_choice': choice,
               'max_tokens': 6000, 'reasoning': {'effort': effort}}
    started = time.monotonic()
    result = bot.post('https://openrouter.ai/api/v1/chat/completions', payload, timeout=180,
                      headers={'Authorization': f'Bearer {b.openrouter_key}'})
    if 'error' in result:
        raise RuntimeError(f'openrouter: {result["error"]}')
    choice = (result.get('choices') or [{}])[0]
    usage = result.get('usage') or {}
    return choice.get('message') or {}, {'cost': usage.get('cost') or 0,
                                         'elapsed_s': round(time.monotonic() - started, 2)}


def write(b, message, through, jev):
    """The writer's answer with the marks decided, and what it did."""
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
    about, long, everyone = jev['о себе'] >= SELF_AT, jev['паста'] >= LONG_AT, jev['всем'] >= ALL_AT
    tagged, unseen = [], 0
    if everyone:  # as «[тег все]» in `Bot.joke`
        unseen = b.roster(chat_id, live=False)
        names = {**b.history.names(chat_id), b.me['username'].lower(): 'Рофл'}
        people = {user: name for user, name in names.items() if not user.endswith('bot')}
        unseen, tagged = max(0, unseen - len(names)), list(people)
    # A paste with no form asked: the form is taken away, or a rap comes of «рейдж пасту».
    shown = [name for name in TOOL_LINES if name != 'форма' or not (long and jev['форма'] == 'нет')]
    tools = [tool for tool in author.TOOLS
             if tool['function']['name'] in (*shown, 'ничего')]
    system = bot.SYSTEM + HEAD + ''.join(f'\n- {TOOL_LINES[name]}' for name in shown) + TAIL
    card = ''
    if about:
        feelings = b.feelings(chat_id, date, message)
        card = bot.SELF + (f'\n\n{feelings}' if feelings else '') + '\n\n'
    manner = bot.tone(datetime.date.fromtimestamp(date))
    now = datetime.datetime.fromtimestamp(date)
    tagging = (f' Тегнуты: {", ".join(dict.fromkeys(people[user] for user in tagged))} — обращайся к ним по этим '
               'именам, @ не пиши.' if tagged else '') + (
        f' Ещё {unseen} человек в чате ты не знаешь: Telegram не показывает их боту, пока они не напишут.'
        if unseen else '')
    prompt = (card + seeing + said + f'\n\nСейчас {bot.WEEKDAYS[now.weekday()]}, {now:%H:%M}. Последнее сообщение '
              f'адресовано тебе, пишет {bot.person_label(sender)}.'
              + (' Спрашивают о тебе: держись того, что знаешь о себе.' if about else '') + tagging
              + (f' {manner}' if manner else '') + (LONG if long else '') + ' Ответь одной репликой')
    messages = [{'role': 'system', 'content': system}, {'role': 'user', 'content': bot.with_image(prompt, image)}]
    calls, cost, elapsed, form = [], 0.0, 0.0, None
    for round_ in range(4):
        reply, stats = ask(b, messages, tools, choice='required' if round_ == 0 else 'auto')
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
            english = {'what': 'что', 'kind': 'вид', 'query': 'запрос', 'name': 'имя', 'names': 'имена', 'emoji': 'эмодзи'}
            args = {english.get(k, k): v for k, v in args.items()}
            calls.append({'tool': fn, 'args': args})
            if fn == 'реакция':
                emoji = bot.reaction_of(f'[реакция {args.get("эмодзи", "")}]')
                return f'[реакция {emoji}]', {'tools': calls, 'cost': round(cost, 6), 'marks': jev}
            result = author.run_tool(b, fn, args, lines, people, chat_id, date, message)
            if fn == 'тегнуть':
                tagged, result = list(dict.fromkeys(tagged + result[1])), result[0]
            if fn == 'форма' and args.get('имя') in bot.FORMS:
                form = args['имя']
            calls[-1]['result_chars'] = len(result)
            messages.append({'role': 'tool', 'tool_call_id': call['id'], 'content': result})
    else:
        reply = {'content': ''}
    answer = (reply.get('content') or '').strip()
    if not answer:
        messages.append({'role': 'user', 'content': 'Ответь одной репликой, без инструментов.'})
        reply, stats = ask(b, messages, tools, effort='low', choice='none')
        cost, elapsed = cost + stats['cost'], elapsed + stats['elapsed_s']
        answer = (reply.get('content') or '').strip()
    answer = author.clean(answer, chat, {'Рофл', bot.person_label(sender), *heads}, form)
    decided = [name for name, on in (('о себе', about), ('паста', long), ('всем', everyone)) if on]
    return bot.envelope(answer, {'tagged': tagged}), {'tools': calls, 'decided': decided, 'cost': round(cost, 6),
                                                      'elapsed_s': round(elapsed, 2), 'image': bool(image)}


def one(chat, message_id, jev):
    b = author.the_bot()
    row = b.history.db.execute("SELECT raw FROM messages WHERE chat=? AND id=? AND role='user'",
                               (chat, message_id)).fetchone()
    if not row:
        return None
    message = json.loads(row[0])
    later = [json.loads(r) for r, in b.history.db.execute(
        "SELECT raw FROM messages WHERE chat=? AND topic=? AND role='user' AND id > ? ORDER BY id LIMIT 10",
        (chat, bot.topic_of(message), message_id))]
    extra = bot.followups(message, later)
    through = extra[-1]['message_id'] if extra else message_id
    lines, _ = b.chat((chat, bot.topic_of(message)), through)
    out = {'chat': chat, 'id': message_id, 'request': bot.render(row[0], b.me['id']), 'before': lines[-7:-1]}
    for name, run in (('current', lambda: author.current(b, message, through)),
                      ('author', lambda: write(b, message, through, jev))):
        try:
            out[name], out[name + '_trace'] = run()
        except Exception as exc:
            out[name], out[name + '_trace'] = f'ОШИБКА: {exc!r}', {}
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('ids', nargs='*')
    args = parser.parse_args()
    live = sqlite3.connect('file:/Users/macacos/Sync/bot/history.sqlite3?mode=ro', uri=True)
    copy = sqlite3.connect(SNAPSHOT)
    live.backup(copy)  # both answer on the same chat, while the live bot goes on pruning
    copy.execute('PRAGMA journal_mode=DELETE')  # a WAL copy will not open read-only without its -shm
    copy.close(), live.close()
    bot.History.__init__ = ro_snapshot
    jev = marks()
    gold = json.loads((HERE / 'ab_pairs.json').read_text())
    todo = args.ids or [k for k in gold if k in jev and (jev[k]['о себе'] >= SELF_AT or jev[k]['паста'] >= LONG_AT
                                                         or jev[k]['всем'] >= ALL_AT)]
    results = json.loads(OUT.read_text()) if OUT.exists() else {}
    todo = [k for k in todo if k not in results]
    print(f'{len(todo)} requests: {", ".join(todo)}', flush=True)

    def job(k):
        chat, message_id = map(int, k.split(':'))
        out = one(chat, message_id, jev[k])
        if out is None:
            print(k, 'gone from the database', flush=True)
            return
        results[k] = out
        OUT.write_text(json.dumps(results, ensure_ascii=False, indent=1))
        cost = sum((out.get(n) or {}).get('cost') or 0 for n in ('current_trace', 'author_trace'))
        print(f"{k} ${cost:.4f} | setup {out['current_trace'].get('tags')!r} | marks "
              f"{out['author_trace'].get('decided')} | tools "
              f"{','.join(c['tool'] for c in out['author_trace'].get('tools', [])) or '-'}", flush=True)

    with ThreadPoolExecutor(args.workers) as pool:
        list(pool.map(job, todo))
    print(f'{len(results)} pairs in {OUT}')


if __name__ == '__main__':
    main()
