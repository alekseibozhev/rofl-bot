"""Jev in the shadow of the bot: every message of people in its groups gets Jev's answers to what the bot decides,
one JSON line each in shadow.jsonl next to this file: is it said to Рофл, would he butt in on it and on which line,
and the tags of the setup. Every message of the bot, a quarter of an hour on, gets how the chat took it by what people
wrote after it, in reception.jsonl: Telegram tells a bot of reactions only in a chat that made it an admin, and none
did. Nothing goes to Telegram, nothing acts on the answers, the bot itself is not touched.

The bot's database is read every `POLL` seconds, read-only. It keeps 200 messages a chat, so a line keeps the chat
Jev saw; the bot's answers and their tags stay in its table `answers` for good, to be joined by the request.
Usage: shadow.py [--once] [--limit N] [--dry-run] [--budget DOLLARS]
"""
import argparse, fcntl, importlib.util, json, logging, os, sqlite3, sys, time, urllib.error, urllib.request
from collections import Counter
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
DB = f'file:{HERE.parent / "history.sqlite3"}?mode=ro'
OUT = HERE / 'shadow.jsonl'
URL = 'https://openrouter.ai/api/alpha/decisions'
MODEL = 'typesafe/jev-1.14-20261014'  # pinned: thresholds hold for one version
VERSION = 2  # of the questions below; a line says which it was asked, a message is asked again on a new one
POLL = 30  # seconds
WINDOW = 30  # messages through the one decided on
TAKEN = HERE / 'reception.jsonl'
TAKEN_VERSION = 1
AFTER = 15 * 60  # seconds of the chat after a message of the bot that tell how it was taken
spec = importlib.util.spec_from_file_location('bot', HERE.parent / 'bot.py')
bot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bot)
LOG = logging.getLogger('shadow')

# In English: asked in Russian, Jev got 22 setups of 50 right against 32 (the test of 2026-10-21). «jump_in» and
# «hook» are the butt-in probe's, its threshold 0.87 made 6 butt-ins in two days; the tags are in the words of SETUP.
TO_ROFL = ('Rofl is a joke bot in this Russian-language group chat of friends. Is the last message said to Rofl '
           'himself: does it ask him something, tell him something, answer or mock what he wrote, even without his '
           'name? A message to other people of the chat, even one about Rofl, is no.')
JUMP = ("Rofl is a rude, witty joke bot in this Russian-language group chat of friends. He answers when called, "
        "and now and then he may butt in on his own with one jab. Nobody called him in the last message. Is there, "
        "in the last few messages, something fresh he could jump on right now so that the chat would laugh: a slip "
        "or a typo, a boast, an absurd claim, a dumb plan, two people arguing, someone whining? If the talk is "
        "dry business, already answered by Rofl, or butting in would just be noise, it is no.")
HOOK = "If Rofl butts in now, which message should his jab reply to?"
# «jump_in» went 0.78–0.85 on half the moments and gave 0.89 to «НИЧЕ???»; whether and where in one choice, its «нет»
# a score of its own, may part them wider.
JAB = ('Rofl is a rude, witty joke bot in this Russian-language group chat of friends; now and then he butts in on his '
       'own with one jab. Which of these messages would he jump on right now so that the chat would laugh, if any? '
       'Most moments have none: dry business, talk Rofl has already answered, or a jab that would just be noise.')
NO_JAB = 'none: nothing here is worth butting in on'
FORM = ('The chat is in Russian; its last message is addressed to Rofl, a joke bot. Which form of answer does the '
        'last message itself ask for? Only one it actually asks for.')
FORMS = {'нет': 'no form is asked for, or one not on this list',
         'гринтекст': 'a greentext is asked for (a story in lines starting with «>»)',
         'бугурт': 'a bugurt is asked for (caps lines separated by «@»)',
         'рэп': 'a rap, a diss or a rap battle is asked for',
         'стих': 'a poem or a chastushka is asked for',
         'код': 'program code is asked for'}
TAGS = {
    'без чата': 'Does the last message ask for something not about this chat: a joke, poem or explanation about '
                'something outside the conversation, a change of topic, to forget or leave someone alone? A request '
                'about the chat or its people (stir things up, report the situation, roast someone) is no.',
    'обида': 'Does the last message seriously insult Rofl or tell him to fuck off?',
    'клоун': 'Is the last message a dumb jab at Rofl with no thought and no joke: an empty «рофл», a bare «fuck off» '
             'or «you are stupid», nitpicking at trifles? A request, a joke, a witty dig or agreeing with Rofl is no.',
    'реакция': 'Does the last message only agree with Rofl or laugh, so that no words are needed in reply?',
    'о себе': 'Does the last message ask about Rofl himself: how he is, what he remembers, how he works, what he read?',
    'тег': 'Does the last message ask Rofl to tag, call or ping people of the chat?',
    # «РАЗДАЙ ВСЕМ УЧАСТНИКАМ ЧАТА ПОГОНЯЛА» (#243) got 0.60 on «тег»: it asks no tagging, it wants the whole chat.
    'всем': 'Does the last message ask Rofl for something about every person of the chat: tag or call everyone, give '
            'everyone nicknames, roast or rate everyone?',
    'паста': 'Does the last message ask Rofl to write at length, tell a story or report: 8–15 lines instead of one or '
             'two?',
    'найти': 'Does the last message quote a line of a known paste, poem or meme (complete it, continue it, a line with '
             'an ellipsis) or ask for a specific known text (a paste, quote, song, joke) or an answer in the style or '
             'on the template of a specific meme?',
    'новости': 'Is the last message about a fresh world or political event, or does it ask to report the situation, '
               'what is new, what is going on in the world, or to discuss the news?',
    'справка': 'Does the last message name a meme, a character or a reference worth a short explanation?',
}
RECEPTION = ('Rofl is a rude, witty joke bot in this Russian-language group chat of friends. How did the people of the '
             'chat take the message of Rofl with the id «rofl_message_id», judging by what they wrote right after it?')
TAKES = {'ржали': 'they laughed at it, praised it or played along with the joke',
         'ответили': 'they answered it or went on talking about it, without laughing',
         'мимо': 'nobody took it up: the talk went on without it, or nobody wrote',
         'бесит': 'it annoyed them: they told Rofl off for it, complained or said it was bad or beside the point'}


def lines(db, chat):
    """The chat's messages oldest first, as the bot's chat lines show them: who, when, to which message, the text
    with a mark of a picture, a sticker or a forward."""
    out = []
    for id_, role, content, date, reply_to, raw in db.execute(
            'SELECT id, role, content, date, reply_to, raw FROM messages WHERE chat=? ORDER BY date, id',
            (chat,)).fetchall():
        if role == 'assistant':
            content = content or ''
            author, is_bot = 'Рофл', True
            text = bot.text_of(json.loads(content) if content.startswith('[') else content)
        else:
            message = json.loads(raw) if raw else {}
            sender = bot.sender_of(message)
            author, is_bot = bot.author_label(sender), bool(sender.get('is_bot'))
            text = message.get('text') or message.get('caption') or ''
            media = next((label for kind, label in bot.MEDIA if message.get(kind)), None)
            if media:
                text = f'[{media}] {text}'.strip()
            elif message.get('sticker'):
                text = f'[стикер {message["sticker"].get("emoji", "")}]'
            forward = bot.origin_label(message.get('forward_origin'))
            if forward:
                text = f'(переслано из «{forward}») {text}'
        out.append({'id': id_, 'time': datetime.fromtimestamp(date).strftime('%d.%m %H:%M'), 'date': date,
                    'author': author, 'bot': is_bot, 'reply_to': reply_to, 'text': text})
    return out


def payload(chat_lines, i):
    """The request on the i-th message: the `WINDOW` messages through it and its reply chain however old, as the bot
    reads it ten replies deep; the hook is one of the last eight lines of people."""
    window = chat_lines[max(0, i - WINDOW + 1):i + 1]
    by_id, shown = {m['id']: m for m in chat_lines[:i + 1]}, {m['id'] for m in window}
    chain, reply = [], chat_lines[i]['reply_to']
    for _ in range(10):
        if reply not in by_id:
            break
        if reply not in shown:
            chain.insert(0, by_id[reply])
        reply = by_id[reply]['reply_to']
    messages = [{k: m[k] for k in ('id', 'time', 'author', 'reply_to', 'text')} | ({'bot': True} if m['bot'] else {})
                for m in chain + window]
    noul = lambda text: {'type': 'noul', 'instructions': text}
    questions = {'to_rofl': noul(TO_ROFL), 'jump_in': noul(JUMP),
                 'форма': {'type': 'choice', 'instructions': FORM, 'criteria': FORMS},
                 **{tag: noul(text) for tag, text in TAGS.items()}}
    hooks = {str(m['id']): f"{m['author']}: {bot.clip(' '.join(m['text'].split()), 80)}"
             for m in window[-8:] if m['author'] != 'Рофл'}
    if len(hooks) > 1:
        questions['hook'] = {'type': 'choice', 'instructions': HOOK, 'criteria': hooks}
    questions['jab'] = {'type': 'choice', 'instructions': JAB, 'criteria': {**hooks, 'нет': NO_JAB}}
    return {'model': MODEL, 'state': {'chat': messages, 'last_message_id': chat_lines[i]['id']}, 'questions': questions}


def taken_payload(chat_lines, i):
    """The request on how the i-th message, the bot's, was taken: six messages before it, the one it answered however
    old, and the twelve at most written in the `AFTER` seconds after it; and those."""
    m = chat_lines[i]
    after = [n for n in chat_lines[i + 1:i + 13] if n['date'] <= m['date'] + AFTER]
    asked = [n for n in chat_lines[:max(0, i - 6)] if n['id'] == m['reply_to']]
    messages = [{k: n[k] for k in ('id', 'time', 'author', 'reply_to', 'text')} | ({'bot': True} if n['bot'] else {})
                for n in asked + chat_lines[max(0, i - 6):i + 1] + after]
    return {'model': MODEL, 'state': {'chat': messages, 'rofl_message_id': m['id']},
            'questions': {'taken': {'type': 'choice', 'instructions': RECEPTION, 'criteria': TAKES}}}, after


def post(body):
    request = urllib.request.Request(URL, json.dumps(body).encode(),
                                     {'Authorization': 'Bearer ' + os.environ['OPENROUTER_API_KEY'],
                                      'Content-Type': 'application/json'})
    started = time.monotonic()
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response), round(time.monotonic() - started, 3)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--once', action='store_true', help='decide on what is there and exit')
    parser.add_argument('--limit', type=int, help='at most this many messages')
    parser.add_argument('--dry-run', action='store_true', help='print the first request, send nothing')
    parser.add_argument('--budget', type=float, default=1.0, help='dollars this run may spend, then it stops')
    args = parser.parse_args()
    logging.basicConfig(filename=HERE / 'shadow.log', level=logging.INFO,
                        format='%(asctime)s %(levelname)s %(message)s')
    lock = open(HERE / '.shadow.lock', 'w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit('shadow.py is running already')
    bot.load_env()
    done = set()
    if OUT.exists():
        for line in OUT.read_text().splitlines():
            try:
                record = json.loads(line)
            except ValueError:  # a line cut by a kill
                continue
            if record['questions'] == VERSION:
                done.add((record['chat'], record['id']))
    taken = set()
    if TAKEN.exists():
        for line in TAKEN.read_text().splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if record['questions'] == TAKEN_VERSION:
                taken.add((record['chat'], record['id']))
    spent, sent, failures = 0.0, 0, Counter()
    LOG.info('start model=%s questions=%s done=%s taken=%s', MODEL, VERSION, len(done), len(taken))
    while True:
        db = sqlite3.connect(DB, uri=True)
        chats = [chat for (chat,) in db.execute('SELECT DISTINCT chat FROM messages WHERE chat < 0').fetchall()]
        todo, later = [], []
        for chat in chats:
            chat_lines = lines(db, chat)
            # Ten messages before it at least: the oldest the database keeps have no chat above them.
            todo += [('decide', chat, chat_lines, i) for i, m in enumerate(chat_lines)
                     if i >= 10 and not m['bot'] and (chat, m['id']) not in done and failures[chat, m['id']] < 3]
            # The bot's own, once the quarter of an hour after it has passed.
            later += [('taken', chat, chat_lines, i) for i, m in enumerate(chat_lines)
                      if m['author'] == 'Рофл' and m['bot'] and m['date'] <= time.time() - AFTER
                      and (chat, m['id']) not in taken and failures[chat, m['id']] < 3]
        db.close()
        for kind, chat, chat_lines, i in (todo + later)[:args.limit]:
            m = chat_lines[i]
            body, after = (payload(chat_lines, i), None) if kind == 'decide' else taken_payload(chat_lines, i)
            if args.dry_run:
                print(json.dumps(body, ensure_ascii=False, indent=1))
                return
            try:
                answer, elapsed = post(body)
            except urllib.error.HTTPError as exc:
                LOG.warning('failed chat=%s id=%s HTTP %s %s', chat, m['id'], exc.code, exc.read()[:300])
                if exc.code == 429 or exc.code >= 500:  # busy or down: all again on the next poll
                    break
                failures[chat, m['id']] += 1  # refused: three times, then the message is skipped
                continue
            except ValueError as exc:  # a broken answer: as refused
                LOG.warning('failed chat=%s id=%s %s', chat, m['id'], exc)
                failures[chat, m['id']] += 1
                continue
            except OSError as exc:  # the network, a Mac asleep: all again on the next poll
                LOG.warning('offline %s', exc)
                break
            cost = answer.get('usage', {}).get('cost') or 0
            spent, sent = spent + cost, sent + 1
            if kind == 'taken':
                replies = [n for n in after if n['reply_to'] == m['id'] and not n['bot']]
                took = answer['answers']['taken']
                with TAKEN.open('a') as out:
                    out.write(json.dumps({'chat': chat, 'id': m['id'], 'date': m['date'], 'reply_to': m['reply_to'],
                                          'text': m['text'], 'taken': took.get('choice'),
                                          'probabilities': took.get('probabilities'),
                                          'after': sum(not n['bot'] for n in after), 'replies': len(replies),
                                          'repliers': len({n['author'] for n in replies}),
                                          'questions': TAKEN_VERSION, 'elapsed_s': elapsed, 'jev': answer,
                                          'state': body['state']}, ensure_ascii=False) + '\n')
                taken.add((chat, m['id']))
                LOG.info('taken chat=%s id=%s %s replies=%s cost=%s', chat, m['id'], took.get('choice'), len(replies),
                         cost)
                if spent > args.budget:
                    LOG.error('budget $%s spent, %s answers: stopped', args.budget, sent)
                    sys.exit(f'budget ${args.budget} spent')
                continue
            flat = {name: a.get('noul', a.get('choice')) for name, a in answer.get('answers', {}).items()}
            with OUT.open('a') as out:
                out.write(json.dumps({'chat': chat, 'id': m['id'], 'date': m['date'], 'author': m['author'],
                                      'reply_to': m['reply_to'], 'text': m['text'], 'answers': flat,
                                      'questions': VERSION, 'elapsed_s': elapsed, 'jev': answer,
                                      'state': body['state']}, ensure_ascii=False) + '\n')
            done.add((chat, m['id']))
            LOG.info('decided chat=%s id=%s to_rofl=%s jump_in=%s cost=%s', chat, m['id'], flat.get('to_rofl'),
                     flat.get('jump_in'), cost)
            if spent > args.budget:
                LOG.error('budget $%s spent, %s answers: stopped', args.budget, sent)
                sys.exit(f'budget ${args.budget} spent')
        if args.once or args.limit:
            print(f'{sent} answers, ${spent:.5f}')
            return
        time.sleep(POLL)


if __name__ == '__main__':
    main()
