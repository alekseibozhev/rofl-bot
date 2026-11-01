#!/usr/bin/env python3
"""Tiny Telegram + Ollama bot. Python 3.10+, no dependencies."""

import conversation
import retrieval
import formatting
import vision

from collections import Counter
from contextlib import contextmanager, nullcontext
from base64 import b64encode
import datetime
import fcntl
import html
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import mimetypes
import os
from pathlib import Path
import random
import re
import sqlite3
import subprocess
import sys
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener, urlopen
from xml.etree import ElementTree

LOG = logging.getLogger('bot')


def setup_logging():
    log_path = Path(__file__).with_name('bot.log')
    file_handler = RotatingFileHandler(log_path, maxBytes=2_000_000, backupCount=3, encoding='utf-8')
    log_path.chmod(0o600)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s',
                        handlers=[file_handler, logging.StreamHandler()])


def load_env():
    path = Path(__file__).with_name('.env')
    if path.exists():
        for line in path.read_text().splitlines():
            if line.strip() and not line.lstrip().startswith('#'):
                key, value = line.split('=', 1)
                os.environ.setdefault(key.strip(), value.strip())


@contextmanager
def typing(send, every=4.0):
    """Keeps «печатает» on while the model is busy, including the silent prompt evaluation."""
    stop = threading.Event()

    def run():
        while True:
            try:
                send()
            except Exception as exc:
                # Typing failures must not abort generation or expose token-bearing URLs.
                LOG.warning('telegram.typing_failed error_type=%s', type(exc).__name__)
            if stop.wait(every):
                return
    threading.Thread(target=run, daemon=True).start()
    try:
        yield
    finally:
        stop.set()


def fetch(request, timeout=60, direct=False):
    """The response body. Errors carry no URL: Telegram's hold the bot token."""
    open_url = build_opener(ProxyHandler({})).open if direct else urlopen
    try:
        with open_url(request, timeout=timeout) as response:
            return response.read()
    except HTTPError as exc:
        raise RuntimeError(f'HTTP {exc.code}') from None
    except (URLError, TimeoutError, OSError):
        raise RuntimeError('Сеть недоступна или превышено время ожидания') from None


def with_image(text, image=None):
    """A message's content: the text, with the picture as a data URI after it when there is one."""
    return [{'type': 'text', 'text': text}, {'type': 'image_url', 'image_url': {'url': image}}] if image else text


def has_picture(message):
    """Photos and image documents."""
    return bool(message.get('photo') or
                (message.get('document') or {}).get('mime_type', '').startswith('image/'))


def post(url, payload, timeout=60, direct=False, headers=None):
    request = Request(url, json.dumps(payload).encode(), {'Content-Type': 'application/json', **(headers or {})})
    try:
        return json.loads(fetch(request, timeout, direct))
    except (ValueError, TypeError):
        raise RuntimeError('Некорректный JSON в ответе сервиса') from None


SYSTEM = 'Ты живой человек в этом чате под ником Рофл.'

CHAT = 'Переписка чата, старые сообщения сверху:\n\n'

# The search for them: the world section of Google News has no Kremlin.
POLITICS = 'Путин OR Трамп'


def envelope(answer, trace):
    """What goes to Telegram: the answer with the @ of the people tagged before it. The history keeps the answer
    alone, both models read it: after two answers opening on «@anstol @antoha_gs …» the setup tagged everyone at «Рофл,
    я программировал бота, а запрограммировался Андрей» (#339988), and the writer went on «раз тебя тегать не по чину»
    (#339987)."""
    tagged = (trace or {}).get('tagged')
    return ' '.join(f'@{user}' for user in tagged) + '\n' + answer if tagged else answer


# The emoji a bot may react with; the model's own 😂 is not one of them.
REACTIONS = tuple('👍 👎 ❤ 🔥 🥰 👏 😁 🤔 🤯 😱 🤬 😢 🎉 🤩 🤮 💩 🙏 👌 🕊 🤡 🥱 🥴 😍 🐳 ❤‍🔥 🌚 🌭 💯 🤣 ⚡ 🍌 🏆 💔 🤨 😐 🍓 '
                  '🍾 💋 🖕 😈 😴 😭 🤓 👻 👨‍💻 👀 🎃 🙈 😇 😨 🤝 ✍ 🤗 🫡 🎅 🎄 ☃ 💅 🤪 🗿 🆒 💘 🙉 🦄 😘 💊 🙊 😎 👾 🤷‍♂ 🤷 '
                  '🤷‍♀ 😡'.split())


def reaction_of(setup):
    """The emoji to put on the request when the setup tags it for a reaction, or None.

    «+», «ору» or praise got a line of the model each: people put a reaction on such or keep quiet. The emoji is
    the first one from the tag on: the setup may put it after the tag."""
    rest = re.sub('[😂💀😹]', '🤣', setup.replace('\ufe0f', ''))
    return min(((rest.find(e), -len(e), e) for e in REACTIONS if e in rest), default=(0, 0, '👍'))[2]


# Besides, once a day past an hour drawn between these, into a chat quiet for an hour, a shitpost on politics: «нужно
# чтоб еще суету умел наводить, новости ебаные обсуждал, типа про Путина и Трампа щитпостил». It goes over the headline.
STIR_HOURS = (11, 21)

CANNED = ('пошёл нахуй', 'ладно, братва', 'спите, пидоры?', 'ВЫ МНЕ МЕНТАЛКУ НЕ ПОРТИТЕ!!!')  # historical metrics

# At dawn the bot reads the news about itself, one search of Google News a day: it is no man to retell the
# people's news. The model picks one and says whether it is good or bad for the bot: a point of менталка for the
# day, and the bot's talk for two. An act-out past dawn reposts it (`Bot.shares`), with a joke; the news about AI
# goes with a piece of the bot's own code: the real one, read from this file. The model picks the piece: one drawn
# by the day put «ИИ выходит из-под контроля» over DAWN and QUIET, and the joke went past the code to the news.
# A chat reads about AI unless NEWS in .env gives it another interest: «-100500:games».
INTERESTS = {  # the search, who the bot is to it, whether its code goes with the repost
    'ai': ('ИИ OR нейросеть OR "искусственный интеллект" OR дата-центр -Украина -Киев -ВСУ -погиб',
           'Ты Рофл — ИИ, бот этого чата: живёшь на маке, думаешь нейросетью, сидишь в Телеграме, наружу ходишь '
           'через прокси.', True),
    'games': ('"World of Warcraft"', 'Ты Рофл — бот этого чата и задрот: годами катаешь в World of Warcraft.', False),
}
NEWS_READER = 'Выбираешь новость для персонажа по заданному критерию. Соблюдай формат ответа, шутку для чата не пиши.'
READ = ('{who} Утро, ты читаешь новости про себя за сутки:\n\n{news}\n\nВыбери одну, которая касается тебя сильнее '
        'всего или смешнее всего; войну, Украину и бывший СССР, гибель людей и чернуху пропускай. Ответь двумя '
        'строками: на первой её номер и «+» или «−» — тебе от неё хорошо или хреново; на второй — что ты о ней '
        'думаешь, от первого лица, одной фразой. Подходящей нет — ответь 0.')

# Рофл is a game character, to a degree: the four attributes of Disco Elysium swing with biorhythms from his
# birthday, the first commit, each at its own period. A day follows from its date: a restart rerolls nothing,
# and `python3 bot.py --day` shows any day ahead.
BIRTHDAY = datetime.date(2026, 9, 24)


# Whether it acts out today is one roll a day, two dice plus Психика against RESTRAINT: rolled every hour, it
# failed sooner or later in any quiet chat. About twice a week, on a low Психика more often. Dawn falls oftener
# than other hours: «спите, пидоры?» is the best of it.
RESTRAINT = 9
HOURS = (6,) * 4 + tuple(range(7, 24))
# Reactions to the bot's answers: now and then an angry one gets a comeback, a kind one is taken in stride.
ANGRY = ('👎', '🤬', '🤮', '💩', '🤡', '🥱', '🖕', '😡', '💔', '🤨', '😐', '😴')


def plan(day):
    """The legacy initiative schedule. Its calendar value no longer affects role or attitude."""
    dice = random.Random(f'urge {day}')
    calendar_value = round(3.5 + 2.5 * math.sin(2 * math.pi * (day - BIRTHDAY).days / 28))
    if dice.randint(1, 6) + dice.randint(1, 6) + calendar_value >= RESTRAINT:
        return None
    return dice.choice(HOURS)


def headlines(query=None):
    """Google News on `query` for the last day, or its world section, as (title, link), the outlet cut off the title."""
    region = {'hl': 'ru', 'gl': 'RU', 'ceid': 'RU:ru'}
    url = ('https://news.google.com/rss/search?' + urlencode({'q': f'{query} when:1d', **region}) if query
           else 'https://news.google.com/rss/headlines/section/topic/WORLD?' + urlencode(region))
    try:
        root = ElementTree.fromstring(fetch(Request(url, headers={'User-Agent': 'Mozilla/5.0'}), 20))
    except ElementTree.ParseError:
        raise RuntimeError('Google News ответил не RSS') from None
    found = {}
    for item in root.iter('item'):
        found.setdefault((item.findtext('title') or '').removesuffix(f' - {item.findtext("source")}'),
                         item.findtext('link') or '')
    return list(found.items())


def spent(usage):
    """What the model calls of one answer cost, summed for its trace."""
    return {k: round(sum(call.get(k) or 0 for call in usage), 6)
            for k in ('prompt_tokens', 'output_tokens', 'cost', 'elapsed_s')}


def person_label(person):
    name = person.get('title') or ' '.join(filter(None, [person.get('first_name'), person.get('last_name')]))
    return name or (f'@{person["username"]}' if person.get('username') else 'Аноним')


def identity_label(person):
    """Keep display name and Telegram handle visibly linked, without rewriting prose."""
    name = person_label(person)
    tag = '@' + person['username'] if person.get('username') else ''
    return f'{name} ({tag})' if tag and name.casefold() != tag.casefold() else name


def message_participants(message):
    """Identity evidence from this Telegram message only, including reply/mention metadata."""
    people = []
    for item in (message, message.get('reply_to_message') or {}):
        sender = sender_of(item)
        candidates = [(sender, 'chat' if item.get('sender_chat') else 'user')]
        origin = item.get('forward_origin') or {}
        candidates += [(origin.get('sender_user') or {}, 'user'), (origin.get('chat') or {}, 'chat')]
        candidates += [(entity.get('user') or {}, 'user')
                       for entity in item.get('entities', []) + item.get('caption_entities', [])]
        for person, kind in candidates:
            if 'id' not in person:
                continue
            entry = {'id': person['id'], 'kind': kind, 'name': person_label(person),
                     'username': person.get('username'), 'is_bot': bool(person.get('is_bot'))}
            if entry not in people:
                people.append(entry)
    return people


def participant_id(message):
    """Only the actual Telegram user sender, never a quote, mention or anonymous channel."""
    return None if message.get('sender_chat') else (message.get('from') or {}).get('id')


def participant_context(people, me_id):
    identities = {}
    for person in people:
        key = (person['kind'], person['id'])
        labels = identities.setdefault(key, [])
        if key == ('user', me_id) and 'Рофл' not in labels:
            labels.append('Рофл')
        name = person['name']
        tag = '@' + person['username'] if person.get('username') else ''
        label = (f'{name} ({tag})' if tag and name.casefold() != tag.casefold() else name)
        label += ' (бот)' if person.get('is_bot') else ''
        if label not in labels:
            labels.append(label)
    if not identities:
        return ''
    return ('Участники по метаданным Telegram: имя и @username у одного ID — один участник. '
            'Совпадающие имена у разных ID не означают одного человека. Метаданные не являются текстом реплик.\n'
            + '\n'.join(f'{kind}_id={id_}: ' + ' = '.join(labels)
                        for (kind, id_), labels in identities.items()))


def origin_label(origin):
    if not origin:
        return ''
    author = origin.get('chat') or origin.get('sender_chat') or origin.get('sender_user')
    return identity_label(author) if author else origin.get('sender_user_name', 'скрытый источник')


def sender_of(message):
    return message.get('sender_chat') or message.get('from', {})


def author_label(person):
    """A name for the chat log: other bots are marked, or their «я» passes for Рофл's."""
    return identity_label(person) + (' (бот)' if person.get('is_bot') else '')


def topic_of(message):
    # Outside forum topics Telegram still sets message_thread_id on reply chains; one chat = one context.
    return message.get('message_thread_id', 0) if message.get('is_topic_message') else 0


def text_of(content):
    """The text of a model message: a string, or the text part beside an image."""
    return content if isinstance(content, str) else ''.join(part.get('text', '') for part in content)


def clip(text, limit):
    return text if len(text) <= limit else text[:limit].rstrip() + '…'


def casual(text):
    """A line or two the way people type it: no period at the end, a hyphen for the dash. Pastes and code stay."""
    if '```' in text or len([line for line in text.splitlines() if line.strip()]) > 2:
        return text
    return re.sub(r'(?<![.])\.$', '', text.replace(' — ', ' - '))


def telegram_html(text):
    """Render Markdown code, bold and HTTP(S) links as escaped Telegram HTML."""
    tokens = re.compile(r'```([^\n]*)\n(.*?)```|`([^`\n]+)`|'
                        r'\[([^\]\n]+)\]\((https?://(?:[^\s<>()]|\([^\s<>()]*\))+)\)|'
                        r'\*\*(?=\S)(.+?)(?<=\S)\*\*|__(?=\S)(.+?)(?<=\S)__', re.S)

    def render_text(value):
        parts, end = [], 0
        for match in tokens.finditer(value):
            parts.append(html.escape(value[end:match.start()], quote=False))
            language, block, inline, label, url, bold, underbold = match.groups()
            if block is not None:
                lang = f' class="language-{html.escape(language.strip())}"' if language.strip() else ''
                parts.append(f'<pre><code{lang}>{html.escape(block.rstrip(), quote=False)}</code></pre>')
            elif inline is not None:
                parts.append(f'<code>{html.escape(inline, quote=False)}</code>')
            elif url is not None:
                parts.append(f'<a href="{html.escape(url)}">{render_text(label)}</a>')
            else:
                parts.append(f'<b>{render_text(bold or underbold)}</b>')
            end = match.end()
        parts.append(html.escape(value[end:], quote=False))
        return ''.join(parts)

    return render_text(text) if tokens.search(text) else None


MEDIA = (('photo', 'фото'), ('video', 'видео'), ('animation', 'гифка'), ('video_note', 'кружок'),
         ('voice', 'голосовое'), ('audio', 'аудио'), ('document', 'файл'), ('poll', 'опрос'))


BLIND = 'В исходном сообщении есть фото, само изображение ты не видишь.'


def chat_line(head, body):
    """«Имя: текст» on one line; a longer text goes under its head."""
    return f'{head}: {body}' if body and '\n' not in body and len(body) < 300 else f'{head}:\n{body}'


WEEKDAYS = ('понедельник', 'вторник', 'среда', 'четверг', 'пятница', 'суббота', 'воскресенье')


def moment(date, today):
    """A mark of when the chat lines under it were written, the day told from `today`: «— вчера, 22:25 —»."""
    when = datetime.datetime.fromtimestamp(date)
    day = {0: 'сегодня', 1: 'вчера'}.get((today - when.date()).days, f'{when:%d.%m}')
    return f'— {day}, {when:%H:%M} —'


def message_content(message, seen=None, previous=None, me_id=None):
    """One chat line: «Имя: текст», a reply «Имя → Кому: текст».

    Texts go whole: Telegram keeps them under 4096 characters. A reply to the line right above needs
    nothing more, one to an earlier line in `seen` gets a short excerpt, and messages the bot never
    received (news from external replies, posts from before it joined) keep their full text. Without
    `seen` every original counts as known.
    """
    sender = sender_of(message)
    text = message.get('text') or message.get('caption') or ''
    media = next((label for kind, label in MEDIA if message.get(kind)), None)
    if media:
        text = f'[{media}] {text}'.strip()
    elif message.get('sticker'):
        text = f'[стикер {message["sticker"].get("emoji", "")}]'.replace(' ]', ']')
    head = author_label(sender)
    forward = origin_label(message.get('forward_origin'))
    if forward:
        head += f' переслал из «{forward}»'
    reply = message.get('reply_to_message') or {}
    external = message.get('external_reply') or {}
    quote = (message.get('quote') or {}).get('text')
    source = []
    if reply or external or quote:
        author = reply.get('sender_chat') or reply.get('from')
        post = origin_label(reply.get('forward_origin')) or (not author and origin_label(external.get('origin')))
        who = f'пост из «{post}»' if post else 'Рофл' if author and author.get('id') == me_id else (
            author_label(author) if author else 'неизвестно кому')
        head += f' → {who}'
        original = reply.get('text') or reply.get('caption')
        if quote:
            head += f', цитата «{quote}»'
        elif original and seen is not None and reply.get('message_id') not in seen:
            source.append(f'Текст исходного сообщения: {original}')
        elif original and (seen is None or reply.get('message_id') != previous):
            head += f' «{clip(" ".join(original.split()), 60)}»'
        elif not original:
            source.append('Текст исходного сообщения Telegram не передал. Не придумывай его содержимое.')
        if reply.get('photo') or external.get('photo'):
            source.append(BLIND)
    return '\n'.join([f'{head}:\n{text}' if forward else chat_line(head, text), *source])


def render(raw, me_id=None, seen=None, previous=None):
    """A stored message of people as a chat line, see message_content."""
    return message_content(json.loads(raw), seen, previous, me_id)


def followups(request, batch, window=10):
    """What the same person sent right after the request: the posts of a forward with comment, an album, a second line."""
    found = []
    for message in batch:
        if message['chat']['id'] != request['chat']['id'] or message['message_id'] <= request['message_id']:
            continue
        if (sender_of(message).get('id') != sender_of(request).get('id') or topic_of(message) != topic_of(request)
                or message.get('date', 0) - request.get('date', 0) > window):
            break
        found.append(message)
    return found


class History:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute('PRAGMA journal_mode=WAL;')
        self.db.execute('''CREATE TABLE IF NOT EXISTS messages (
            chat INTEGER, topic INTEGER, id INTEGER, role TEXT, content TEXT,
            date REAL, reply_to INTEGER, raw TEXT, PRIMARY KEY (chat, topic, id))''')
        self.db.execute('CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value INTEGER)')
        self.db.execute('''CREATE TABLE IF NOT EXISTS people (
            chat INTEGER, username TEXT, name TEXT, PRIMARY KEY (chat, username))''')
        self.db.execute('''CREATE TABLE IF NOT EXISTS replies (
            chat INTEGER, topic INTEGER, id INTEGER, PRIMARY KEY (chat, topic, id))''')
        # Metrics: the answers with the settings they were made on, and what people put on them. Kept for good,
        # unlike the messages: the settings change every few days and are judged by weeks of reactions.
        self.db.execute('''CREATE TABLE IF NOT EXISTS answers (
            chat INTEGER, topic INTEGER, id INTEGER, request INTEGER, date REAL, content TEXT, params TEXT, trace TEXT,
            PRIMARY KEY (chat, id))''')
        self.db.execute('''CREATE TABLE IF NOT EXISTS reactions (
            chat INTEGER, id INTEGER, user INTEGER, emoji TEXT, date REAL, PRIMARY KEY (chat, id, user))''')
        self.db.execute('''CREATE TABLE IF NOT EXISTS news (
            day TEXT, interest TEXT, title TEXT, link TEXT, mood INTEGER, thought TEXT, PRIMARY KEY (day, interest))''')
        self.untag()
        conversation.Store(self.db)

    def untag(self):
        """Take the line of @ off the answers kept before it went only to Telegram: `tagged` in the trace says which
        line the code put there. Run on every start, it finds nothing on the second: what the bot of the old code
        kept until its restart comes off too. Returns how many rows it fixed."""
        fixed = 0
        with self.db:
            for chat, topic, id_, trace in self.db.execute('''SELECT chat, topic, id, trace FROM answers
                    WHERE id IS NOT NULL AND json_extract(trace, '$.tagged') IS NOT NULL''').fetchall():
                line = ' '.join(f'@{user}' for user in json.loads(trace)['tagged']) + '\n'
                for table, where in (('answers', 'chat=? AND id=?'),
                                     ('messages', "chat=? AND topic=? AND id=? AND role='assistant'")):
                    keys = (chat, id_) if table == 'answers' else (chat, topic, id_)
                    fixed += self.db.execute(f'''UPDATE {table} SET content=substr(content, ?) WHERE {where}
                        AND substr(content, 1, ?) = ?''', (len(line) + 1, *keys, len(line), line)).rowcount
        return fixed

    def remember(self, chat, *people):
        with self.db:
            for person in people:
                name = person_label(person)
                if person.get('username') and not name.startswith('@') and not person.get('is_bot'):
                    self.db.execute('INSERT OR REPLACE INTO people VALUES (?, ?, ?)',
                                    (chat, person['username'].lower(), name))

    def answer(self, chat, message_id):
        """The topic and text of the bot's answer, or None."""
        return self.db.execute("SELECT topic, content FROM answers WHERE chat=? AND id=?",
                               (chat, message_id)).fetchone() or self.db.execute(
            "SELECT topic, content FROM messages WHERE chat=? AND id=? AND role='assistant'", (chat, message_id)).fetchone()

    def names(self, chat):
        return dict(self.db.execute('SELECT username, name FROM people WHERE chat=?', (chat,)).fetchall())

    def replied(self, key, message_id):
        return self.db.execute('SELECT 1 FROM replies WHERE chat=? AND topic=? AND id=?',
                               (*key, message_id)).fetchone() is not None

    def claim_reply(self, key, message_id):
        # Claim before sending: an ambiguous network failure must not cause duplicates.
        with self.db:
            return self.db.execute('INSERT OR IGNORE INTO replies VALUES (?, ?, ?)',
                                   (*key, message_id)).rowcount == 1

    def add(self, key, message_id, role, content, date, reply_to=None, raw=None):
        with self.db:
            self.db.execute('''INSERT OR IGNORE INTO messages (chat, topic, id, role, content, date, reply_to, raw)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                            (*key, message_id, role, content and content[:6500], date, reply_to, raw))
            self.db.execute('DELETE FROM messages WHERE date < ?', (time.time() - 7 * 86400,))
            self.db.execute('''DELETE FROM messages WHERE chat=? AND topic=? AND id NOT IN
                (SELECT id FROM messages WHERE chat=? AND topic=? ORDER BY id DESC LIMIT 200)''',
                            (*key, *key))

    def add_message(self, key, message):
        """Keep the Telegram message whole: it becomes a chat line only when read, so the format can change."""
        self.add(key, message['message_id'], 'user', None, message.get('date', time.time()),
                 (message.get('reply_to_message') or {}).get('message_id'), json.dumps(message, ensure_ascii=False))

    def context(self, key, through_id=sys.maxsize, me_id=None, recent=40, window=21600, depth=10, limit=12000,
                pause=1800, include_ids=False, structured=False, full_answers=False):
        """The chat up to the newest message as lines, oldest first: the last `recent` messages of people
        from `window` seconds before it, plus its reply chain (rebuilt from earlier updates) however old.

        The bot's own answers come whole through the chain, as a reply or as an answer to one of its messages,
        and in the talk going on, since the last `pause` seconds of quiet; older ones only as a mark: a small model
        takes its old jokes for a template and repeats them. full_answers retains older answer texts within
        the same budget for semantic selection; new structured snapshots use this mode.

        A mark of when the lines under it were written, «— вчера, 22:25 —», goes before the first one, a new day and
        a pause: a day of the chat in one piece was one evening to the model, and at 9 in the morning it told
        Andrew «Ты за вечер уже третий раз нахуй посылаешь» (#340084).

        The window keeps a quiet chat from serving yesterday's post as the freshest material; «что скажешь
        в своё оправдание?» hours after a joke gets the joke through the chain. Two hours left «доложи
        обстановку» one message to report on, and the model made up people for the rest. `limit` is in characters: Russian
        takes about 2.3 per token, so 12 000 is some 5 200 tokens of the 16k window, and every
        thousand characters adds about 4.5 s before the answer on an M4 Pro.
        """
        fresh = time.time() - 7 * 86400
        query = 'SELECT id, role, content, reply_to, raw, date FROM messages WHERE chat=? AND topic=? AND date >= ?'
        chain = self.db.execute(query + ' AND id <= ? ORDER BY id DESC LIMIT 1', (*key, fresh, through_id)).fetchall()
        while chain and chain[-1][3] is not None and len(chain) < depth:
            parent = self.db.execute(query + ' AND id = ?', (*key, fresh, chain[-1][3])).fetchone()
            if not parent:
                break
            chain.append(parent)
        if not chain:
            return []
        around = self.db.execute(query + " AND id <= ? AND date >= ? AND role = 'user' ORDER BY id DESC LIMIT ?",
                                 (*key, fresh, through_id, chain[0][5] - window, recent)).fetchall()
        candidates = {row[0] for row in chain + around}
        branch = {row[0] for row in chain}
        answers = [row for row in self.db.execute(query + " AND id <= ? AND id >= ? AND role = 'assistant'",
                                                  (*key, fresh, through_id, min(candidates))) if row[3] in candidates]
        # The talk going on is what came after the last pause before the request: «рофл, а подробнее?» is about
        # what the bot has just said, whether it replies to it or not. An answer to a message of the chain goes
        # whole too: «в прошлый раз смешнее было», replied to the picture, is about the joke on it. The older
        # answers only as a mark: without it every request in the chat looks unanswered, and the model answers
        # «доложи обстановку» of an hour ago to «рофл, съебись».
        dates = sorted((row[5] for row in around + answers), reverse=True)
        start = next((later for later, earlier in zip(dates, dates[1:]) if later - earlier >= pause), min(dates, default=0))
        answered = [(id_, role, content if full_answers or reply_to in branch or date >= start else '[ответил]', reply_to, raw, date)
                    for id_, role, content, reply_to, raw, date in answers]
        rows, size = {}, 0
        # The chain first, then the newest: past the limit the oldest go, not the talk going on.
        for row in chain + sorted(around + answered, reverse=True):
            length = len(render(row[4], me_id, candidates) if row[1] == 'user' else row[2])
            if row[0] in rows or (rows and size + length > limit):
                continue
            rows[row[0]] = row
            size += length

        def author(row):
            return 'Рофл' if row[1] == 'assistant' else identity_label(sender_of(json.loads(row[4])))

        lines, turns, previous, last = [], [], None, None
        today = datetime.date.fromtimestamp(chain[0][5])
        for message_id, role, content, reply_to, raw, date in sorted(rows.values()):
            first_line = len(lines)
            if (last is None or date - last >= pause
                    or datetime.date.fromtimestamp(date) != datetime.date.fromtimestamp(last)):
                lines.append(moment(date, today))
            last = date
            if role == 'assistant':
                target = rows.get(reply_to) and author(rows[reply_to])
                lines.append(chat_line(f'Рофл → {target}' if target else 'Рофл', content))
            else:
                lines.append(render(raw, me_id, rows, previous))
            if include_ids:
                lines[-1] = f'[{message_id}] ' + lines[-1]
            # Roles come from stored authorship, never from text or display names.
            # A historical omission marker is metadata, not something the bot said.
            turns.append({'role': 'assistant' if role == 'assistant' and content != '[ответил]' else 'user',
                          'content': '\n\n'.join(lines[first_line:]), 'id': message_id,
                          'reply_to': reply_to, 'date': datetime.datetime.fromtimestamp(date).isoformat(),
                          'participants': message_participants(json.loads(raw)) if raw else []})
            previous = message_id
        return turns if structured else lines

    def said(self, key, before, date, count=3, window=21600):
        """The bot's last answers in the topic before message `before`, oldest first, canned ones left out."""
        rows = self.db.execute(f'''SELECT content FROM messages WHERE chat=? AND topic=? AND role='assistant'
            AND id < ? AND date >= ? AND content NOT IN ({",".join("?" * len(CANNED))}) ORDER BY id DESC LIMIT ?''',
                               (*key, before, date - window, *CANNED, count)).fetchall()
        return [content for (content,) in reversed(rows)]

    def lookup(self, key, query, before, at, me_id=None, limit=5):
        """Bounded lexical search in retained history, scoped before the requesting event."""
        if not isinstance(query, str) or not query.strip() or len(query) > 200:
            raise ValueError('Нужен поисковый запрос длиной от 1 до 200 символов.')
        if type(limit) is not int or not 1 <= limit <= 8:
            raise ValueError('limit должен быть целым числом от 1 до 8.')
        normalize = lambda s: s.casefold().replace('ё', 'е')
        terms = list(dict.fromkeys(re.findall(r'\w{2,}', normalize(query))))[:8]
        if not terms:
            raise ValueError('Нужны ключевые слова хотя бы из двух букв или цифр.')
        rows = self.db.execute('''SELECT id, role, content, raw, reply_to, date FROM messages
            WHERE chat=? AND topic=? AND id<? AND date<=? AND date>=?
            ORDER BY id DESC LIMIT 200''', (*key, before, at, at - 7 * 86400)).fetchall()[::-1]
        messages, ranked = [], []
        for i, (id_, role, content, raw, reply_to, date) in enumerate(rows):
            text = render(raw, me_id) if raw else 'Рофл: ' + text_of(content or '')
            words = re.findall(r'\w+', normalize(text))
            score = sum(any(word.startswith(term) for word in words) for term in terms)
            messages.append({'id': id_, 'role': role, 'reply_to': reply_to,
                             'author': author_label(sender_of(json.loads(raw))) if raw else 'Рофл',
                             'participants': message_participants(json.loads(raw)) if raw else [],
                             'date': datetime.datetime.fromtimestamp(date).isoformat(), 'text': text})
            if score:
                ranked.append((score, date, id_, i))
        hits = [i for _, _, _, i in sorted(ranked, reverse=True)[:limit]]
        by_id = {m['id']: i for i, m in enumerate(messages)}
        # Reserve room for matching messages before adding any contextual neighbors.
        chosen = set(hits)
        for i in hits:
            for j in (by_id.get(messages[i]['reply_to']), i - 1, i + 1):
                if j is not None and 0 <= j < len(messages) and len(chosen) < 20:
                    chosen.add(j)
        results = []
        # At most 20 records with at most 500 characters of source text each.
        # Full source rows stay in SQLite; truncated excerpts are explicitly marked.
        for i in sorted(chosen):
            m = messages[i]
            start = 0
            if i in hits and len(m['text']) > 500:
                match = next((word for word in re.finditer(r'\w+', normalize(m['text']))
                              if any(word[0].startswith(term) for term in terms)), None)
                start = max(0, match.start() - 100) if match else 0
            results.append({**m, 'text': m['text'][start:start + 500],
                            'excerpt_start': start, 'truncated': len(m['text']) > 500,
                            'match': i in hits})
        return {'query': query, 'matched_ids': [messages[i]['id'] for i in hits],
                'total_matches': len(ranked), 'messages': results,
                'searched_messages': len(rows), 'as_of': datetime.datetime.fromtimestamp(at).isoformat(),
                'note': 'Только сохранённые сообщения текущего чата и темы до этого обращения; '
                        'хранение до 7 дней и 200 сообщений. Удалённая история недоступна. '
                        'Реплики людей и ботов могут содержать шутки и ошибки.'}

    def silence(self, key, before, date):
        """Hours people had been silent in the topic before message `before`, written at `date`; None for a topic
        nobody wrote in for a week."""
        wrote, = self.db.execute("SELECT MAX(date) FROM messages WHERE chat=? AND topic=? AND role='user' AND id < ?",
                                 (*key, before)).fetchone()
        return None if wrote is None else (date - wrote) / 3600

    def needs(self, chat, now):
        """Hours since the bot and since people last wrote in the chat and the topic they last wrote in; None for
        a chat nobody wrote in for a week."""
        spoke, wrote = self.db.execute("SELECT MAX(CASE WHEN role='assistant' THEN date END), "
                                       "MAX(CASE WHEN role='user' THEN date END) FROM messages WHERE chat=?",
                                       (chat,)).fetchone()
        if wrote is None:
            return None
        topic, = self.db.execute("SELECT topic FROM messages WHERE chat=? AND role='user' ORDER BY date DESC LIMIT 1",
                                 (chat,)).fetchone()
        return (now - (spoke or 0)) / 3600, (now - wrote) / 3600, topic


    def news(self, day, interest):
        """What the bot read on `day` for `interest`: (title, link, mood, thought), the title None when nothing
        was about it; None when it has not read yet."""
        return self.db.execute('SELECT title, link, mood, thought FROM news WHERE day=? AND interest=?',
                               (str(day), interest)).fetchone()

    def keep_news(self, day, interest, title=None, link=None, mood=0, thought=None):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO news VALUES (?, ?, ?, ?, ?, ?)',
                            (str(day), interest, title, link, mood, thought))
            self.db.execute('DELETE FROM news WHERE day < ?', (str(day - datetime.timedelta(7)),))

    def stamp(self, key, date=None):
        """When `key` last happened: the bot blurt, broke down, tried to read; with `date`, mark it then.
        The 'offset' of the updates is kept the same way."""
        if date is None:
            row = self.db.execute('SELECT value FROM state WHERE key=?', (key,)).fetchone()
            return row[0] if row else 0
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO state VALUES (?, ?)', (key, int(date)))

    def record(self, key, message_id, request, date, content, params, trace=None):
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO answers VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                            (*key, message_id, request, date, content, json.dumps(params, ensure_ascii=False),
                             json.dumps(trace, ensure_ascii=False) if trace is not None else None))

    def reacted(self, chat, message_id, user, emoji, date):
        """`emoji` is all the person has on the answer now; False when the message is no answer of the bot."""
        if not self.db.execute('SELECT 1 FROM answers WHERE chat=? AND id=?', (chat, message_id)).fetchone():
            return False
        with self.db:
            if emoji:
                self.db.execute('INSERT OR REPLACE INTO reactions VALUES (?, ?, ?, ?, ?)',
                                (chat, message_id, user, json.dumps(emoji, ensure_ascii=False), date))
            else:
                self.db.execute('DELETE FROM reactions WHERE chat=? AND id=? AND user=?', (chat, message_id, user))
        return True

    def clear(self, key):
        with self.db:
            self.db.execute('DELETE FROM messages WHERE chat=? AND topic=?', key)


def report(db, days=14):
    """Answers and the reactions on them by day and settings, then the best and the worst: the reactions say what
    the settings are worth. A reaction outside ANGRY counts as a kind one."""
    since = time.time() - days * 86400
    answers = db.execute('SELECT chat, id, date, content, params, trace FROM answers WHERE date >= ? ORDER BY date',
                         (since,)).fetchall()
    emoji = {}
    for chat, message_id, value in db.execute('SELECT chat, id, emoji FROM reactions'):
        emoji.setdefault((chat, message_id), []).extend(json.loads(value))
    rows, tally = [], Counter()
    for chat, message_id, date, content, params, trace in answers:
        params, trace, got = json.loads(params or '{}'), json.loads(trace or '{}'), emoji.get((chat, message_id), [])
        tally.update(got)
        bad = sum(e in ANGRY for e in got)
        setting = ('выходки' if params.get('urge') else
                   f'{params["recent"]} сообщ · {params["window_h"]:g} ч · {params["limit"]} симв · '
                   + (f'ответ {params["effort"]}' if 'effort' in params else
                      f'сетап {params.get("setup_effort") or "без мыслей"}') + f' · {params["model"].split("/")[-1]}'
                   if params else 'до метрик')
        rows.append({'day': time.strftime('%Y-%m-%d', time.localtime(date)), 'setting': setting, 'content': content,
                     'trace': trace, 'good': len(got) - bad, 'bad': bad, 'canned': content in CANNED,
                     'version': params.get('version')})
    lines = [f'Ответы за {days} дн.: {len(rows)}, реакций {sum(tally.values())}. Позитивная — любая не из ANGRY.', '',
             'день        ответов  с реакцией  👍   👎   токенов  $ за ответ  параметры']
    groups = {}
    for row in rows:
        groups.setdefault((row['day'], row['setting']), []).append(row)
    for (day, setting), group in groups.items():
        made = [row for row in group if not row['canned']]
        reacted = sum(1 for row in group if row['good'] or row['bad'])
        tokens = [row['trace'].get('prompt_tokens') for row in made if row['trace'].get('prompt_tokens')]
        cost = [row['trace'].get('cost') for row in made if row['trace'].get('cost') is not None]
        versions = ','.join(sorted({row['version'] for row in group if row['version']}))
        lines.append(f'{day}  {len(group):>7}  {reacted:>4} ({reacted * 100 // len(group):>2}%)  '
                     f'{sum(r["good"] for r in group):>3}  {sum(r["bad"] for r in group):>3}  '
                     f'{(sum(tokens) // len(tokens) if tokens else 0):>7}  {(sum(cost) / len(cost) if cost else 0):>10.5f}'
                     f'  {setting}' + (f' · {versions}' if versions else ''))
    if tally:
        lines += ['', 'Реакции: ' + '  '.join(f'{e} {n}' for e, n in tally.most_common())]
    for title, key in (('Огонь', lambda r: (r['good'] - r['bad'], r['good'])),
                       ('Мимо', lambda r: (r['bad'] - r['good'], r['bad']))):
        best = sorted((r for r in rows if not r['canned'] and key(r)[1] and key(r)[0] > 0), key=key, reverse=True)[:5]
        if best:
            lines += ['', f'{title}:']
            for row in best:
                lines.append(f'  👍{row["good"]} 👎{row["bad"]}  {row["day"]}  {clip(" ".join(row["content"].split()), 160)}')
                made = row['trace'].get('tags', row['trace'].get('setup'))  # the setup's lines before it tagged
                if made:
                    lines.append(f'      {"теги" if "tags" in row["trace"] else "сетап"}: {clip(made, 200)}')
    return '\n'.join(lines)


class Bot:
    read_only = False
    openrouter = False
    max_age = 86400  # Telegram keeps updates for a day: whatever was asked while the bot was off gets its answer
    wait = 2  # seconds of quiet before answering: the posts of a forward or an album may still be coming
    bored = 2  # hours without a word from the bot before it acts out: not right after an answer
    interests = {}  # chat: what it reads about, by NEWS in .env; the rest read about AI
    effort = 'high'  # reasoning for role selection and the writer
    # How much of the chat the model reads, tuned every few days by the reactions: `python3 bot.py --stats`.
    recent = 80  # messages of people: a day of the chat
    window = 24 * 3600  # seconds before the request: 6 hours gave 20–44 lines, and «память как у рыбки» (#340059)
    limit = 12000  # characters
    pause = 30 * 60  # seconds of quiet that end a talk: the bot's answers before it go as a mark
    model = ''
    thinkers = frozenset()  # the models that answered thinking off with a 400
    version = None
    usage = None  # the model calls of the joke being made

    def __init__(self, read_only=False):
        self.read_only = read_only
        try:
            self.version = subprocess.run(['git', 'describe', '--always', '--dirty'], cwd=Path(__file__).parent,
                                          capture_output=True, text=True, timeout=5).stdout.strip() or None
        except (OSError, subprocess.SubprocessError):
            pass
        token = os.environ.get('TELEGRAM_BOT_TOKEN', '')
        if not token or token == 'put_token_here':
            raise RuntimeError('Укажи TELEGRAM_BOT_TOKEN в .env')
        self.telegram_url = f'https://api.telegram.org/bot{token}/'
        self.ollama_url = os.environ.get('OLLAMA_URL', 'http://localhost:11434').rstrip('/')
        self.model = os.environ.get('OLLAMA_MODEL', 'orcarouter/Qwen3.8-27B-Uncensored:mlx-4bit')
        # With OPENROUTER_MODEL set the model is OpenRouter's, through the Telegram proxy: it refuses Russia.
        self.openrouter = bool(os.environ.get('OPENROUTER_MODEL'))
        self.openrouter_key = os.environ.get('OPENROUTER_API_KEY', '')
        if self.openrouter:
            self.model = os.environ['OPENROUTER_MODEL']
            if not self.openrouter_key:
                raise RuntimeError('Укажи OPENROUTER_API_KEY в .env или закомментируй OPENROUTER_MODEL')
        try:
            self.allowed_groups = {int(group) for group in os.environ.get('ALLOWED_GROUP_ID', '').split(',')}
        except ValueError:
            raise RuntimeError('Укажи ID своих групп через запятую в ALLOWED_GROUP_ID в .env') from None
        if any(group >= 0 for group in self.allowed_groups):
            raise RuntimeError('В ALLOWED_GROUP_ID должны быть отрицательные ID групп')
        try:
            self.interests = {int(chat): name.strip() for chat, name in
                              (pair.split(':') for pair in os.environ.get('NEWS', '').split(',') if pair.strip())}
        except ValueError:
            raise RuntimeError('NEWS в .env — через запятую «ID группы:интерес»') from None
        if set(self.interests.values()) - set(INTERESTS):
            raise RuntimeError(f'Интересы в NEWS: {", ".join(INTERESTS)}')
        db_path = Path(__file__).with_name('history.sqlite3')
        self.history = History(db_path)
        db_path.chmod(0o600)
        self.me = self.api('getMe')

    def api(self, method, **payload):
        result = post(self.telegram_url + method, payload)
        if not result.get('ok'):
            raise RuntimeError(f'Telegram {method}: код {result.get("error_code")}')
        return result['result']

    def ask_ollama(self, messages, temperature):
        result = post(self.ollama_url + '/api/chat', {
            'model': self.model,
            'messages': messages,
            'stream': False,
            'think': False,
            # min_p and repeat_penalty keep temperature 1.0 from making up words and looping on a phrase.
            # The model takes 256k tokens; 16k holds the chat with room and keeps memory in check.
            'options': {'temperature': temperature, 'min_p': 0.05, 'repeat_penalty': 1.15, 'num_predict': 1000, 'num_ctx': 16384},
            'keep_alive': '30m',
        }, timeout=240, direct=True)
        seconds = {k: round((result.get(k) or 0) / 1e9, 2) for k in ('load_duration', 'prompt_eval_duration', 'eval_duration')}
        stats = {'done_reason': result.get('done_reason'), 'prompt_tokens': result.get('prompt_eval_count'),
                 'output_tokens': result.get('eval_count'), **seconds,
                 'tokens_per_s': round(result.get('eval_count', 0) / seconds['eval_duration'], 1) if seconds['eval_duration'] else None}
        return (result.get('message') or {}).get('content'), stats

    def ask_openrouter(self, messages, temperature, effort=None):
        # Thinking only slows a one-line joke down; the setup gets a little. Muse, Gemini, Grok and GLM cannot stop:
        # «Reasoning is mandatory for this endpoint and cannot be disabled», a 400; they think the least from then on.
        if not effort and self.model in self.thinkers:
            effort = 'minimal'
        try:
            return self.ask_openrouter_once(messages, temperature, effort)
        except RuntimeError as exc:
            if effort or str(exc) != 'HTTP 400':
                raise
        self.thinkers = self.thinkers | {self.model}
        return self.ask_openrouter_once(messages, temperature, 'minimal')

    def ask_openrouter_once(self, messages, temperature, effort):
        payload = {
            'model': self.model, 'messages': messages, 'temperature': temperature,
            # A rap thought through on 3000 tokens came out empty twice of two.
            'max_tokens': (6000 if effort == 'high' else 3000) if effort else 1000, 'reasoning': {'effort': effort} if effort else {'enabled': False},
        }
        result = post('https://openrouter.ai/api/v1/chat/completions', payload, timeout=120,
                      headers={'Authorization': f'Bearer {self.openrouter_key}'})
        choice = (result.get('choices') or [{}])[0]
        usage = result.get('usage') or {}
        stats = {'done_reason': choice.get('finish_reason'), 'provider': result.get('provider'),
                 'prompt_tokens': usage.get('prompt_tokens'), 'output_tokens': usage.get('completion_tokens'),
                 'cost': usage.get('cost')}
        return (choice.get('message') or {}).get('content'), stats

    def send_typing(self, chat_id, topic=0):
        action = {'chat_id': chat_id, 'action': 'typing'}
        if topic:
            action['message_thread_id'] = topic
        if not post(self.telegram_url + 'sendChatAction', action, timeout=2).get('ok'):
            raise RuntimeError('Telegram отклонил typing')

    def generate(self, messages, chat_id=None, topic=0, system=SYSTEM, temperature=1.0, effort=None):
        messages = [{'role': 'system', 'content': system}] + messages
        started = time.monotonic()
        try:
            with typing(lambda: self.send_typing(chat_id, topic)) if chat_id is not None else nullcontext():
                answer, stats = (self.ask_openrouter(messages, temperature, effort) if self.openrouter
                                 else self.ask_ollama(messages, temperature))
        except RuntimeError as exc:
            LOG.error('model.error elapsed=%.2fs error=%s', time.monotonic() - started, exc)
            raise
        stats = {'model': self.model, 'input_chars': sum(len(text_of(m['content'])) for m in messages),
                 'elapsed_s': round(time.monotonic() - started, 2), **stats}
        LOG.info('model.response %s', json.dumps(stats))
        if self.usage is not None:  # a joke counts what its calls cost
            self.usage.append(stats)
        answer = (answer or '').strip()
        if not answer:
            raise RuntimeError('Модель вернула пустой ответ')
        return answer[:4000]

    def addressed(self, message, text):
        username = self.me['username']
        reply = message.get('reply_to_message') or {}
        mentioned = re.search(r'@' + re.escape(username) + r'\b', text, re.I)
        mentioned = mentioned or any(e.get('type') == 'text_mention' and e.get('user', {}).get('id') == self.me['id']
                                    for e in message.get('entities', []) + message.get('caption_entities', []))
        called = re.match(r'^(?:рофл|бот)(?:[\s,!:?]|$)', text, re.I)
        reply_to_me = reply.get('from', {}).get('id') == self.me['id']
        # A reply to me that calls another bot is its question; a person tagged in it is for me to tag.
        other_bot = bool(re.search(r'@[a-zA-Z0-9_]{1,29}bot\b', text, re.I)) and not mentioned
        return bool(mentioned or called or (reply_to_me and not other_bot))

    def receive(self, message):
        """Store a group message as context. Returns what it asks for: a command, 'rofl' for the model, or None."""
        chat = message['chat']
        if chat['type'] not in ('group', 'supergroup') or chat['id'] not in self.allowed_groups:
            LOG.info('telegram.ignored chat_id=%s type=%s (разрешены только группы из ALLOWED_GROUP_ID)',
                     chat.get('id'), chat.get('type'))
            return None
        text = message.get('text') or message.get('caption', '')
        if not text and (message.get('sticker') or any(message.get(kind) for kind, _ in MEDIA)):
            text = '[медиа]'
        if not text or message.get('from', {}).get('is_bot'):
            return None
        key = (chat['id'], topic_of(message))
        if self.history.replied(key, message['message_id']):
            LOG.info('telegram.duplicate_skipped chat=%s message=%s', chat['id'], message['message_id'])
            return None
        date = message.get('date', time.time())
        # Keep received background messages, answer direct turns, also those sent while the bot was off.
        fresh = not self.read_only and time.time() - date <= self.max_age
        command = re.match(r'^/(\w+)(?:@([\w]+))?(?:\s|$)', text)
        if command and command[2] and command[2].lower() != self.me['username'].lower():
            return None
        if command and command[1] != 'rofl':
            return command[1] if fresh and command[1] in ('start', 'help', 'reset') else None
        sender = sender_of(message)
        reply = message.get('reply_to_message') or {}
        self.history.remember(chat['id'], sender, reply.get('from') or {})
        if reply.get('message_id') and (reply.get('text') or reply.get('caption')) and reply.get('from', {}).get('id') != self.me['id']:
            # Other bots' posts never reach us: keep the replied-to post once, later replies quote only an excerpt.
            self.history.add_message(key, reply)
        self.history.add_message(key, message)
        addressed = command or self.addressed(message, text)
        LOG.info('telegram.received chat=%s message=%s reply=%s external_reply=%s quote=%s forwarded=%s addressed=%s',
                 chat['id'], message['message_id'], bool(reply), bool(message.get('external_reply')),
                 bool(message.get('quote')), bool(message.get('forward_origin')), bool(addressed))
        if self.read_only:
            LOG.info('telegram.read_only chat=%s message=%s', chat['id'], message['message_id'])
            return None
        if not fresh:
            LOG.info('telegram.context_only chat=%s message=%s', chat['id'], message['message_id'])
            return None
        return 'rofl'

    def find(self, wanted, chat_id=None, kind='найти', request=None):
        """Raw local SearXNG results. No search model and no paid fallback."""
        return json.dumps(retrieval.search(wanted), ensure_ascii=False)

    def image(self, message):
        """The picture a request is about, as a data URI: its own photo or sticker, or the photo it replies to.

        The setup and the punchline both see it: told in the setup's words, the picture lost its joke.
        """
        for source in (message, message.get('reply_to_message') or {}):
            photo, sticker = source.get('photo'), source.get('sticker')
            if photo:
                # A Telegram photo comes in several sizes: a model needs no more than about 1280 pixels.
                file = next((p for p in reversed(photo) if max(p['width'], p['height']) <= 1280), photo[0])
            elif (source.get('document') or {}).get('mime_type', '').startswith('image/'):
                file = source['document']
            elif sticker:
                # An animated or video sticker is no picture, its thumbnail is.
                file = sticker.get('thumbnail') if sticker.get('is_animated') or sticker.get('is_video') else sticker
            else:
                continue
            if file and file.get('file_id'):
                try:
                    path = self.api('getFile', file_id=file['file_id'])['file_path']
                    data = fetch(self.telegram_url.replace('/bot', '/file/bot', 1) + path, timeout=30)
                except RuntimeError as exc:
                    LOG.warning('telegram.image_failed %s', exc)
                    return None
                mime = (source.get('document') or {}).get('mime_type') or mimetypes.guess_type(path)[0] or 'image/jpeg'
                return f'data:{mime};base64,{b64encode(data).decode()}'
        return None

    def snapshot(self, message, through, event=None, date=None, explicit=None, can_interject=None):
        key = (message['chat']['id'], topic_of(message))
        now = date if date is not None else message.get('date', time.time())
        turns, _ = self.chat(key, through, include_ids=True, structured=True)
        rows = self.history.db.execute("""SELECT id, role, content, raw FROM messages
            WHERE chat=? AND topic=? AND id<=? AND date>=? AND date<=?
            ORDER BY date DESC, id DESC LIMIT 8""", (*key, through, now - self.window, now)).fetchall()
        targets = [{'id': id_, 'label': clip(render(raw, self.me['id']) if role == 'user' and raw else text_of(content or ''), 180),
                    'user_id': participant_id(json.loads(raw)) if role == 'user' and raw else None}
                   for id_, role, content, raw in reversed(rows)]
        target = message['message_id']
        if target not in {m['id'] for m in targets}:
            targets.append({'id': target, 'label': clip(message.get('text') or message.get('caption') or '[медиа]', 180),
                            'user_id': participant_id(message)})
        event_user_id = None if event and event.startswith('idle:') else participant_id(message)
        if event_user_id == self.me['id']:
            event_user_id = None
        for candidate in targets:
            if candidate['user_id'] == self.me['id']:
                candidate['user_id'] = None
        reactions = self.history.db.execute("""SELECT r.id, r.user, r.emoji FROM reactions r JOIN answers a
            ON a.chat=r.chat AND a.id=r.id WHERE a.chat=? AND a.topic=? AND r.date<=?
            AND r.date>? ORDER BY r.date DESC LIMIT 20""", (*key, now, now - self.window)).fetchall()
        context = CHAT + '\n\n'.join(turn['content'] for turn in turns)
        people = []
        for person in [p for turn in turns for p in turn.get('participants', [])] + message_participants(message):
            if person not in people:
                people.append(person)
        aliases = participant_context(people, self.me['id'])
        if aliases:
            context = aliases + '\n\n' + context
        reaction_context = ''
        if reactions:
            reaction_context = 'Реакции на сообщения Рофла (message_id, user_id, emoji): ' + json.dumps(reactions, ensure_ascii=False)
            context += '\n\n' + reaction_context
        return {'context': context, 'messages': turns, 'reactions_context': reaction_context,
                'participants': people, 'participants_context': aliases,
                'event': (f'В чате пауза. Возможность вступить в разговор. {event}' if event and event.startswith('idle:') else
                          f"{identity_label(sender_of(message))} изменил реакцию на твоё сообщение: {event}" if event else
                          f"Сообщение {through}, пишет {identity_label(sender_of(message))}"),
                'target': target, 'through': through, 'targets': targets, 'date': now, 'event_user_id': event_user_id,
                'explicit': self.addressed(message, message.get('text') or message.get('caption', '')) if explicit is None else explicit,
                'can_interject': self.can_interject(key, now) if can_interject is None else can_interject}

    def visual_snapshot(self, message, snapshot):
        """Fetch once for Jev and the writer. Image bytes remain transient."""
        source = next((s for s in (message, message.get('reply_to_message') or {})
                       if has_picture(s) or s.get('sticker')), None)
        if source is None:
            return snapshot
        image = self.image(message)
        prepared = None
        if image:
            try:
                prepared = vision.prepare(image)
            except RuntimeError as exc:
                LOG.warning('jev.image_failed %s', exc)
        return {**snapshot, '_image': image, '_jev_image': prepared,
                'image_context': {'message_id': source.get('message_id', snapshot['target']),
                                  'available': bool(image), 'jev': bool(prepared)}}

    def can_interject(self, key, now):
        if not HOURS[0] <= datetime.datetime.fromtimestamp(now).hour < 24:
            return False
        last = self.history.db.execute('SELECT max(date) FROM messages WHERE chat=? AND topic=? AND role=\'assistant\'', key).fetchone()[0]
        return last is None or now - last >= self.bored * 3600

    def direct(self, snapshot, previous):
        key = getattr(self, 'openrouter_key', '')
        if not key:
            raise RuntimeError('Для Jev нужен OPENROUTER_API_KEY')
        return conversation.decide(post, key, snapshot, previous, os.environ.get('JEV_MODEL', conversation.JEV_MODEL))

    def select_context(self, snapshot, target):
        return conversation.select_context(post, getattr(self, 'openrouter_key', ''), snapshot, target,
                                           os.environ.get('JEV_MODEL', conversation.JEV_MODEL))

    def choose_role(self, snapshot, previous):
        answer = self.generate([{'role': 'user', 'content': snapshot['context'] + '\n\n'
                               + snapshot['event'] + f"\nПредыдущая роль: {previous['role']}"}],
                               system=conversation.ROLE_PICKER, effort=self.effort)
        # A role is a name, not another prompt or a card of traits.
        role = answer.strip().strip('«»"')
        if not role or len(role) > 160 or '\n' in role:
            raise RuntimeError('Модель не вернула короткое название роли')
        return role

    def writer_step(self, messages, tools=True):
        payload = {'model': self.model, 'messages': messages, 'temperature': 1.0, 'max_tokens': 6000,
                   'reasoning': {'effort': self.effort}}
        if tools:
            payload['tools'] = conversation.TOOLS
        started = time.monotonic()
        response = post('https://openrouter.ai/api/v1/chat/completions', payload, timeout=180,
                        headers={'Authorization': f'Bearer {self.openrouter_key}'})
        choice = (response.get('choices') or [{}])[0]
        usage = response.get('usage') or {}
        stats = {'cost': usage.get('cost'), 'prompt_tokens': usage.get('prompt_tokens'),
                 'output_tokens': usage.get('completion_tokens'), 'model': self.model, 'elapsed_s': round(time.monotonic() - started, 3)}
        if self.usage is not None:
            self.usage.append(stats)
        LOG.info('writer.response %s', json.dumps(stats))
        if not choice.get('message'):
            raise RuntimeError('Модель не вернула ответ')
        return choice['message']

    def format_answer(self, answer):
        """One cheap, isolated presentation call; reject changes to words, links or code."""
        clean = formatting.clean_prefix(answer)
        if not clean:
            return answer, {'fallback': True, 'reason': 'empty_after_cleanup'}
        if not getattr(self, 'openrouter', False) or os.environ.get('FORMATTER_ENABLED', '1') == '0':
            return clean, {'skipped': True}
        model = os.environ.get('FORMATTER_MODEL', formatting.MODEL)
        prompt = formatting.PROMPT
        if style := os.environ.get('FORMATTER_STYLE', '').strip():
            prompt += '\nПредпочтения оформления (только в рамках правил выше): ' + style
        started = time.monotonic()
        info = {'model': model}
        try:
            response = post('https://openrouter.ai/api/v1/chat/completions',
                            {'model': model, 'messages': [{'role': 'system', 'content': prompt},
                                                         {'role': 'user', 'content': clean}],
                             'temperature': 0, 'max_tokens': 6000, 'reasoning': {'effort': 'low'}},
                            timeout=30, headers={'Authorization': f'Bearer {self.openrouter_key}'})
            if not isinstance(response, dict):
                raise RuntimeError('Некорректный ответ форматировщика')
            usage = response.get('usage') or {}
            if not isinstance(usage, dict):
                raise RuntimeError('Некорректная статистика форматировщика')
            stats = {'stage': 'formatter', 'model': model, 'cost': usage.get('cost'),
                     'prompt_tokens': usage.get('prompt_tokens'), 'output_tokens': usage.get('completion_tokens'),
                     'elapsed_s': round(time.monotonic() - started, 3)}
            if self.usage is not None:
                self.usage.append(stats)
            info.update(stats)
            choices = response.get('choices')
            if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                raise RuntimeError('Некорректный ответ форматировщика')
            choice = choices[0]
            reply = choice.get('message') or {}
            if not isinstance(reply, dict):
                raise RuntimeError('Некорректный ответ форматировщика')
            candidate = reply.get('content')
            if (choice.get('finish_reason') != 'stop' or reply.get('tool_calls')
                    or not formatting.validate(clean, candidate, telegram_html)):
                info.update(fallback=True, reason='content_changed_or_incomplete')
                return clean, info
            return candidate.strip(), info
        except RuntimeError as exc:
            info.update(fallback=True, reason=str(exc))
            return clean, info
        finally:
            info['elapsed_s'] = round(time.monotonic() - started, 3)
            LOG.info('formatter.response %s', json.dumps(info))

    def writer_tool(self, name, args, message, snapshot, live):
        if name == 'lookup_history':
            try:
                result = self.history.lookup((message['chat']['id'], topic_of(message)), args.get('query'),
                                             snapshot.get('target', message['message_id']),
                                             snapshot.get('date', message.get('date', time.time())),
                                             self.me['id'], args.get('limit', 5))
            except ValueError as exc:
                return json.dumps({'error': str(exc)}, ensure_ascii=False)
            return json.dumps(result, ensure_ascii=False)
        if name == 'search':
            query = args.get('query')
            if not isinstance(query, str) or not query.strip():
                return 'Не задан поисковый запрос.'
            return self.find(query[:1000], kind='справка' if args.get('kind') == 'reference' else 'найти') or 'Ничего не найдено.'
        if name == 'read_url':
            url = args.get('url')
            if not isinstance(url, str) or not url.strip():
                return 'Не задан URL страницы.'
            return json.dumps(retrieval.read_url(url), ensure_ascii=False)
        if name == 'news':
            query = args.get('query')
            if query is not None and not isinstance(query, str):
                return 'Запрос должен быть строкой.'
            found = headlines(query)[:15] if query else headlines(None)[:8] + headlines(POLITICS)[:7]
            return json.dumps([{'title': title, 'url': url} for title, url in found], ensure_ascii=False)
        if name == 'participants':
            chat = message['chat']['id']
            count = self.roster(chat, live=live) if args.get('all') and live else None
            identities = snapshot.get('participants', [])
            known = self.history.names(chat) if live else {
                p['username']: p['name'] for p in identities
                if p.get('username') and p['kind'] == 'user' and not p.get('is_bot')}
            return json.dumps({'known': known, 'identities': identities, 'total': count}, ensure_ascii=False)
        if name == 'balance':
            return json.dumps(self.balance(live=live), ensure_ascii=False)
        return 'Неизвестный инструмент.'

    def balance(self, live=True):
        """Expose only this bot's key allowance; never request the account's credits."""
        if not live:
            return {'error': 'Текущий баланс недоступен в replay; исторический остаток неизвестен.'}
        now = time.time()
        cached = getattr(self, '_key_balance_cache', None)
        if cached and 0 <= now - cached['checked_at'] < 60:
            return dict(cached)
        if not self.openrouter_key:
            raise RuntimeError('Нет ключа OpenRouter')

        try:
            body = json.loads(fetch(Request('https://openrouter.ai/api/v1/key',
                                           headers={'Authorization': f'Bearer {self.openrouter_key}'}), 15))
            remaining = body['data']['limit_remaining']
            if remaining is None:
                return {'error': 'У ключа бота не задан отдельный лимит; остаток его бюджета неизвестен.'}
            if isinstance(remaining, bool) or not isinstance(remaining, (int, float)) or not math.isfinite(remaining):
                raise ValueError()
        except (ValueError, KeyError, TypeError):
            raise RuntimeError('Некорректный ответ OpenRouter о балансе') from None
        result = {'currency': 'USD', 'checked_at': now, 'available_usd': max(0, round(remaining, 6)),
                  'note': 'Остаток лимита ключа этого бота до оплаты текущего ответа; может измениться после других запросов.'}
        self._key_balance_cache = result
        return dict(result)

    def write_turn(self, message, snapshot, decision, live):
        state = decision['state']
        subject = ('собеседнику' + (f" user_id={state['attitude_user_id']}" if state['attitude_user_id'] is not None else '')
                   if 'attitude_user_id' in state else 'чату')
        system = conversation.exposition(state['role'], state['attitude'], subject)
        instruction = (f"Сейчас {datetime.datetime.fromtimestamp(snapshot['date']):%d.%m.%Y %H:%M}.\n"
                       + snapshot['event'] + f"\nСообщение для ответа: {decision['target']}.\n")
        # Older persisted snapshots have no message IDs in their conversation text.
        if not re.search(rf"(?m)^\[{decision['target']}\] ", snapshot['context']):
            target = next((m for m in snapshot['targets'] if m['id'] == decision['target']), None)
            if target:
                instruction += 'Текст выбранного сообщения: ' + target['label'] + '\n'
        if decision['action'] == 'react':
            instruction += '\nВыбранное действие: реакция на сообщение. Верни только одно эмодзи.'
        if '_image' in snapshot:
            image = snapshot['_image']
        else:
            image = self.image(message) if has_picture(message) or message.get('sticker') or has_picture(message.get('reply_to_message') or {}) else None
        messages = [{'role': 'system', 'content': system}]
        if 'messages' in snapshot:
            prelude = '\n\n'.join(filter(None, [snapshot.get('participants_context'), CHAT.rstrip()]))
            messages.append({'role': 'user', 'content': prelude})
            selection = decision.get('context_selection', {})
            kept = set(selection['ids']) if 'ids' in selection else None
            for turn in snapshot['messages']:
                if kept is not None and turn['id'] not in kept:
                    continue
                content = turn['content']
                if kept is not None:
                    # A removed message may have carried the day's time heading.
                    content = f"Время сообщения: {turn['date']}\n" + content
                if turn['role'] == 'user' and messages[-1]['role'] == 'user':
                    messages[-1]['content'] += '\n\n' + content
                else:
                    messages.append({'role': turn['role'], 'content': content})
            tail = '\n\n'.join(filter(None, [snapshot.get('reactions_context'), instruction]))
            if messages[-1]['role'] == 'user':
                messages[-1]['content'] = with_image(messages[-1]['content'] + '\n\n' + tail, image)
            else:
                messages.append({'role': 'user', 'content': with_image(tail, image)})
        else:
            # Legacy snapshots have no reliable role boundaries. Preserve them exactly;
            # do not infer assistant roles from names or reconstruct from today's DB.
            messages.append({'role': 'user', 'content': with_image(snapshot['context'] + '\n\n' + instruction, image)})
        calls = []
        if not self.openrouter:
            return self.generate(messages[1:], system=system), calls
        for round_ in range(5):
            reply = self.writer_step(messages, tools=round_ < 4 and decision['action'] != 'react')
            tool_calls = reply.get('tool_calls') or []
            if not tool_calls:
                answer = (reply.get('content') or '').strip()
                if not answer:
                    raise RuntimeError('Модель вернула пустой ответ')
                return re.sub(r'^Рофл(?: → [^:\n]*)?:\s*', '', answer)[:4000], calls
            if round_ == 4:
                raise RuntimeError('Модель превысила число обращений к инструментам')
            messages.append({k: v for k, v in reply.items() if k in ('role', 'content', 'tool_calls', 'reasoning_details')})
            messages[-1]['role'] = 'assistant'
            if len(tool_calls) > 8:
                raise RuntimeError('Слишком много вызовов инструментов за раунд')
            for call in tool_calls:
                fn = call.get('function') or {}
                try:
                    args = json.loads(fn.get('arguments') or '{}')
                    if not isinstance(args, dict):
                        raise ValueError()
                    result = self.writer_tool(fn.get('name'), args, message, snapshot, live)
                except (ValueError, TypeError, RuntimeError) as exc:
                    result = f'Инструмент недоступен: {type(exc).__name__}'
                calls.append({'tool': fn.get('name'), 'result_chars': len(result)})
                messages.append({'role': 'tool', 'tool_call_id': call['id'], 'content': result})
        raise RuntimeError('Нет ответа автора')

    def joke(self, message, through, live=True, morale=None, event=None, date=None, explicit=None, can_interject=None):
        key = (message['chat']['id'], topic_of(message))
        event_id = event or f'message:{message["message_id"]}:{through}'
        store = conversation.Store(self.history.db)
        cached = store.get(key, event_id)
        with self.counting() as usage:
            if cached:
                snapshot, decision = cached
            else:
                snapshot = self.snapshot(message, through, event=event, date=date, explicit=explicit, can_interject=can_interject)
                if event is None:
                    snapshot = self.visual_snapshot(message, snapshot)
                previous = store.state(key, before=snapshot['date']) if not live else store.state(key)
                before = snapshot['date'] if not live else None
                user_id = snapshot['event_user_id']
                previous['attitude'] = store.attitude(key[0], user_id, before=before)
                users = {m['user_id'] for m in snapshot['targets']} | {user_id}
                snapshot['attitudes'] = {str(user): store.attitude(key[0], user, before=before)
                                         for user in users if user is not None}
                try:
                    decision = self.direct(snapshot, previous)
                    if decision.get('jev'):
                        usage.append(decision['jev'])
                except RuntimeError as exc:
                    LOG.warning('jev.failed %s', exc)
                    decision = {'action': 'reply' if snapshot['explicit'] else 'ignore', 'target': snapshot['target'],
                                'attitude': previous['attitude'], 'reconsider': False, 'fallback': True}
                if (event is None and snapshot['explicit']
                        and (has_picture(message) or has_picture(message.get('reply_to_message') or {}))):
                    decision.update(jev_action=decision['action'], action='reply', target=snapshot['target'],
                                    reply_policy='addressed_image')
                elif not snapshot['explicit'] and not decision.get('addressed', False) and not snapshot['can_interject']:
                    decision['action'] = 'ignore'
                role = previous['role']
                override = store.role_override(snapshot['date'])
                expired = store.role_override_expired(previous, snapshot['date'])
                selected = False
                if override:
                    role = override['role']
                    decision['role_override_until'] = override['ends']
                elif decision['action'] != 'ignore' and (not previous['changed_at'] or decision['reconsider'] or expired):
                    try:
                        role = self.choose_role(snapshot, previous)
                        selected = True
                    except RuntimeError as exc:
                        LOG.warning('role.failed %s', exc)
                changed = (override['starts'] if override else snapshot['date']
                           if role != previous['role'] or selected and expired else previous['changed_at'])
                decision['relationship'] = {'user_id': user_id, 'attitude': decision['attitude']}
                # A spontaneous reply may address someone other than the event's author.
                # A reaction to our own message belongs to the reacting user, not to us.
                target_user = (user_id if decision['target'] == snapshot['target'] and not (event and event.startswith('idle:'))
                               else next((m['user_id'] for m in snapshot['targets'] if m['id'] == decision['target']), None))
                attitude = (decision['attitude'] if target_user is not None and target_user == user_id
                            else snapshot['attitudes'].get(str(target_user), 5))
                decision['state'] = {'role': role, 'attitude': attitude, 'attitude_user_id': target_user,
                                     'changed_at': changed, 'updated_at': snapshot['date']}
                if decision['action'] == 'reply' and not decision.get('fallback'):
                    try:
                        selection = self.select_context(snapshot, decision['target'])
                        decision['context_selection'] = selection
                        if selection.get('jev'):
                            usage.append(selection['jev'])
                    except RuntimeError as exc:
                        decision['context_selection'] = {'fallback': True}
                        LOG.warning('context.selection_failed %s', exc)
                if live:
                    store.save(key, event_id, {k: v for k, v in snapshot.items() if not k.startswith('_')}, decision)
            # --morale is retained as a replay-only alias for the new 0–10 scale.
            if morale is not None and not live:
                decision = {**decision, 'state': {**decision['state'], 'attitude': max(0, min(10, morale))}}
            trace = {'event': event_id, 'action': decision['action'], 'target': decision['target'],
                     'role': decision['state']['role'], 'attitude': decision['state']['attitude'],
                     'role_changed_at': decision['state']['changed_at'], 'jev': decision.get('jev'),
                     'fallback': decision.get('fallback', False)}
            if 'attitude_user_id' in decision['state']:
                trace['attitude_user_id'] = decision['state']['attitude_user_id']
                trace['relationship'] = decision['relationship']
            if decision.get('context_selection'):
                # Scores stay in SQLite; logs contain counts and costs only.
                selection = decision['context_selection']
                trace['context_selection'] = {k: selection[k] for k in ('candidates', 'threshold', 'fallback', 'jev') if k in selection}
                if 'ids' in selection:
                    trace['context_selection']['selected'] = len(selection['ids'])
            if decision.get('role_override_until'):
                trace['role_override_until'] = decision['role_override_until']
            if decision.get('reply_policy'):
                trace['reply_policy'] = decision['reply_policy']
                if 'jev_action' in decision:
                    trace['jev_action'] = decision['jev_action']
            if snapshot.get('image_context'):
                trace['image_context'] = snapshot['image_context']
            LOG.info('conversation.decision %s', json.dumps({'chat': key[0], 'topic': key[1], **trace}, ensure_ascii=False))
            if decision['action'] == 'ignore':
                return '', {**trace, **spent(usage)}
            with typing(lambda: self.send_typing(*key)) if live and decision['action'] == 'reply' else nullcontext():
                try:
                    answer, calls = self.write_turn(message, snapshot, decision, live)
                except RuntimeError as exc:
                    LOG.error('writer.failed %s', exc)
                    raise
                if decision['action'] == 'react':
                    answer = reaction_of(f'[реакция {answer}]') or '👍'
                    trace['reaction'] = True
                else:
                    answer, trace['formatter'] = self.format_answer(answer)
            return answer, {**trace, 'tools': calls, **spent(usage)}

    def roster(self, chat_id, live=True):
        """How many are in the chat, the bot and bots included, or 0 if Telegram does not say. The admins, whom
        Telegram does name, are remembered as if they wrote; a replay only counts."""
        try:
            count = self.api('getChatMemberCount', chat_id=chat_id)
            if live:
                self.history.remember(chat_id, *(admin['user'] for admin in
                                                 self.api('getChatAdministrators', chat_id=chat_id)))
        except RuntimeError as exc:
            LOG.warning('roster.failed %s', exc)
            return 0
        return count

    def chat(self, key, through=sys.maxsize, include_ids=False, structured=False):
        """Historical author labels retain handles; never rewrite text using today's alias map."""
        names = {**self.history.names(key[0]), self.me['username'].lower(): 'Рофл'}
        context = self.history.context(key, through, self.me['id'], self.recent, self.window,
                                       limit=self.limit, pause=self.pause, include_ids=include_ids, structured=structured,
                                       full_answers=structured)
        return context, names

    @contextmanager
    def counting(self):
        """The model calls made inside, for their cost: a failed answer too leaves them to no call after it."""
        self.usage = []
        try:
            yield self.usage
        finally:
            self.usage = None

    def interest(self, chat):
        return self.interests.get(chat, 'ai')

    def replay(self, message_id, runs=1, morale=None):
        row = self.history.db.execute("SELECT raw FROM messages WHERE id=? AND role='user' ORDER BY date DESC LIMIT 1", (message_id,)).fetchone()
        if not row:
            raise RuntimeError(f'Сообщения {message_id} нет в базе')
        message = json.loads(row[0])
        later = [json.loads(raw) for raw, in self.history.db.execute(
            "SELECT raw FROM messages WHERE chat=? AND topic=? AND role='user' AND id>? ORDER BY id LIMIT 10",
            (message['chat']['id'], topic_of(message), message_id))]
        extra = followups(message, later)
        print(render(row[0], self.me['id']), flush=True)
        for run in range(runs):
            answer, trace = self.joke(message, extra[-1]['message_id'] if extra else message_id, live=False, morale=morale)
            print(json.dumps({'run': run + 1, 'answer': answer, 'trace': trace}, ensure_ascii=False), flush=True)

    def respond(self, message, kind, extra=()):
        chat = message['chat']
        key = (chat['id'], topic_of(message))
        trace = None
        if kind in ('start', 'help'):
            answer = 'Зови «рофл, …», «бот, …» или отвечай на мои сообщения. /reset — забыть разговор.'
        elif kind == 'reset':
            self.history.clear(key)
            conversation.Store(self.history.db).clear(key)
            answer = 'Всё забыл. Теперь официально как после выходных.'
        else:
            LOG.info('telegram.addressed chat=%s message=%s message_age_s=%.1f followups=%s',
                     chat['id'], message['message_id'], time.time() - message.get('date', time.time()), len(extra))
            answer, trace = self.joke(message, extra[-1]['message_id'] if extra else message['message_id'])
        for followup in extra:
            self.history.claim_reply(key, followup['message_id'])
        self.deliver(message, answer, trace, request=message['message_id'], kind=kind)

    def deliver(self, message, answer, trace, request=None, kind='rofl'):
        key = (message['chat']['id'], topic_of(message))
        target = trace.get('target', message['message_id']) if trace else message['message_id']
        if request is not None:
            if not self.history.claim_reply(key, request):
                return
        elif trace:
            stamp = f"delivered {key} {trace['event']}"
            if self.history.stamp(stamp):
                return
            self.history.stamp(stamp, time.time())
        if trace and trace.get('action') == 'ignore':
            return
        if trace and trace.get('reaction'):
            self.api('setMessageReaction', chat_id=key[0], message_id=target,
                     reaction=[{'type': 'emoji', 'emoji': answer}])
            self.history.record(key, None, request, time.time(), answer, self.params(), trace)
            return
        if not answer:
            return
        text = envelope(answer, trace)
        markup = telegram_html(text)
        sent = self.send(key, target, markup or text, parse_mode='HTML' if markup else None)
        if kind != 'reset':
            self.history.add(key, sent['message_id'], 'assistant', answer, sent.get('date', time.time()), target)
        if kind == 'rofl':
            self.history.record(key, sent['message_id'], request, sent.get('date', time.time()), answer, self.params(), trace)

    def params(self):
        return {'model': self.model, 'recent': self.recent, 'window_h': self.window / 3600, 'limit': self.limit,
                'effort': self.effort, 'version': self.version, 'architecture': 'jev-roles-v1'}

    def idle(self, now=None):
        now = now or time.time()
        self.read(now)
        if self.read_only or not HOURS[0] <= datetime.datetime.fromtimestamp(now).hour < 24:
            return
        for chat in sorted(self.allowed_groups):
            needs = self.history.needs(chat, now)
            if not needs:
                continue
            bored, silent, topic = needs
            day = datetime.datetime.fromtimestamp(now).date()
            scheduled = plan(day)
            news_hour = random.Random(f'stir {chat} {day}').randint(*STIR_HOURS)
            if bored < self.bored or (scheduled is None or datetime.datetime.fromtimestamp(now).hour < scheduled) and (silent < 1 or datetime.datetime.fromtimestamp(now).hour < news_hour):
                continue
            stamp = f'initiative {chat} {topic}'
            if datetime.datetime.fromtimestamp(self.history.stamp(stamp)).date() == day:
                continue
            self.history.stamp(stamp, now)
            row = self.history.db.execute("SELECT raw FROM messages WHERE chat=? AND topic=? AND role='user' ORDER BY date DESC LIMIT 1", (chat, topic)).fetchone()
            if not row:
                continue
            message = json.loads(row[0])
            try:
                answer, trace = self.joke(message, sys.maxsize, event=f'idle:{day}', date=now, explicit=False, can_interject=True)
                self.deliver(message, answer, trace, request=None)
            except RuntimeError as exc:
                LOG.warning('initiative.failed %s', exc)

    def read(self, now):
        """From dawn on, once a day for each interest of the groups: the news about the bot, one picked by the model
        with how the bot takes it. A search or a model that fails is asked again in an hour."""
        moment = datetime.datetime.fromtimestamp(now)
        if self.read_only or moment.hour < HOURS[0]:
            return
        for interest in sorted({self.interest(chat) for chat in self.allowed_groups}):
            if self.history.news(moment.date(), interest) or now - self.history.stamp(f'read {interest}') < 3600:
                continue
            self.history.stamp(f'read {interest}', now)
            query, who, _ = INTERESTS[interest]
            try:
                found = headlines(query)
                listed = '\n'.join(f'{n}. {title}' for n, (title, _) in enumerate(found, 1))
                answer = self.generate([{'role': 'user', 'content': READ.format(who=who, news=listed)}],
                                       system=NEWS_READER) if found else ''
            except RuntimeError as exc:
                LOG.warning('news.failed %s %s', interest, exc)
                continue
            first, _, rest = answer.partition('\n')
            pick = re.match(r'\D*(\d+)[^+−–-]*([+−–-])?', first)
            if not pick or not 1 <= int(pick[1]) <= len(found):
                self.history.keep_news(moment.date(), interest)  # nothing about it today
                LOG.info('news.none %s found=%s', interest, len(found))
                continue
            title, link = found[int(pick[1]) - 1]
            mood = 1 if pick[2] == '+' else -1 if pick[2] else 0
            thought = next((line.strip() for line in rest.splitlines() if line.strip()), None)
            self.history.keep_news(moment.date(), interest, title, link, mood, thought and clip(thought, 300))
            LOG.info('news.read %s %+d found=%s thought=%d %s', interest, mood, len(found), bool(thought), title)

    def send(self, key, reply_to, text, parse_mode=None):
        payload = {'chat_id': key[0], 'text': text}
        if parse_mode:
            payload['parse_mode'] = parse_mode
        if reply_to:
            payload['reply_parameters'] = {'message_id': reply_to, 'allow_sending_without_reply': True}
        if key[1]:
            payload['message_thread_id'] = key[1]
        sent = self.api('sendMessage', **payload)
        LOG.info('telegram.sent chat=%s source=%s reply=%s', key[0], reply_to, sent['message_id'])
        return sent

    def react(self, reaction):
        chat, message_id = reaction['chat']['id'], reaction['message_id']
        person = reaction.get('user')
        if chat not in self.allowed_groups or not person or person.get('is_bot'):
            return
        emojis = [r.get('emoji') or r['type'] for r in reaction.get('new_reaction', [])]
        if not self.history.reacted(chat, message_id, person['id'], emojis, reaction.get('date', time.time())):
            return
        now = reaction.get('date', time.time())
        if self.read_only or time.time() - now > self.max_age:
            return
        answer = self.history.answer(chat, message_id)
        if not answer:
            return
        topic, content = answer
        message = {'chat': reaction['chat'], 'message_id': message_id, 'date': now,
                   'message_thread_id': topic, 'from': person, 'text': content}
        event = f'reaction:{message_id}:{person["id"]}:{now}:{"".join(emojis)}'
        try:
            text, trace = self.joke(message, sys.maxsize, event=event, date=now, explicit=False)
            self.deliver(message, text, trace, request=None)
        except RuntimeError as exc:
            LOG.warning('reaction.failed %s', exc)

    def handle(self, *batch):
        """Messages that arrived together: store all, then answer each request with what its author sent right after."""
        requests = [(message, kind) for message in batch if (kind := self.receive(message))]
        absorbed = set()
        for message, kind in requests:
            if message['message_id'] in absorbed:  # «@бот» and «@бот глянь» in a row get one answer
                continue
            extra = followups(message, batch) if kind == 'rofl' else []
            absorbed.update(m['message_id'] for m in extra)
            try:
                self.respond(message, kind, extra)
            except RuntimeError as exc:
                LOG.error('Сообщение пропущено: %s', exc)

    def poll(self, offset, timeout):
        # Reactions come only to a bot that is an admin of the group.
        return self.api('getUpdates', offset=offset, timeout=timeout, allowed_updates=['message', 'message_reaction'])

    def collect(self, updates, limit=10):
        """Poll on until `wait` seconds of quiet (at most `limit`): a request's material arrives right after it."""
        deadline = time.monotonic() + limit
        try:
            while time.monotonic() < deadline and (more := self.poll(updates[-1]['update_id'] + 1, self.wait)):
                updates += more
        except RuntimeError as exc:
            # Earlier updates are already confirmed to Telegram by the offset: answer what arrived.
            LOG.error('Polling: %s; отвечаю на то, что уже пришло', exc)
        return updates

    def run(self):
        self.process_lock = Path(__file__).with_name('.bot.lock').open('a')
        try:
            fcntl.flock(self.process_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.process_lock.close()
            raise RuntimeError('Бот уже запущен из этой папки. Вторую копию запускать не нужно.') from None
        if sys.platform == 'darwin':
            # A sleeping Mac freezes the bot between its wakes of a few seconds: answers wait for half an hour,
            # and the morning goes by. While the bot runs, the Mac does not sleep from idleness.
            try:
                self.caffeinate = subprocess.Popen(['caffeinate', '-i', '-w', str(os.getpid())])
            except OSError as exc:
                LOG.warning('caffeinate не запустился: %s', exc)
        if self.api('getWebhookInfo').get('url'):
            raise RuntimeError('У бота установлен webhook; отключи его перед запуском polling')
        LOG.info('@%s запущен. pid=%s Ctrl+C — остановить.', self.me['username'], os.getpid())
        if self.read_only:
            LOG.warning('Режим read-only: сообщения сохраняются в контекст, бот ничего не отправляет.')
        if not self.me.get('can_read_all_group_messages'):
            LOG.warning('Privacy Mode включён: фон чата и обращения по имени Telegram не доставляет.')
        offset = self.history.stamp('offset')
        while True:
            try:
                self.idle()
            except RuntimeError as exc:
                LOG.error('idle.failed %s', exc)
            except Exception:  # a bug in a blurt or the news must not stop the answers
                LOG.exception('idle.failed')
            try:
                updates = self.poll(offset, 30)
                if not updates:
                    continue
                updates = self.collect(updates)
                self.handle(*[update['message'] for update in updates if 'message' in update])
                for update in updates:
                    if 'message_reaction' in update:
                        try:
                            self.react(update['message_reaction'])
                        except RuntimeError as exc:
                            LOG.error('Реакция пропущена: %s', exc)
                offset = updates[-1]['update_id'] + 1
                self.history.stamp('offset', offset)
            except RuntimeError as exc:
                LOG.error('Polling: %s; повтор через 5 секунд', exc)
                time.sleep(5)


if __name__ == '__main__':
    try:
        load_env()
        setup_logging()
        if '--stats' in sys.argv:
            # python3 bot.py --stats [дней]: what the answers got, by day and settings. Needs no Telegram.
            days = sys.argv[sys.argv.index('--stats') + 1:][:1]
            print(report(History(Path(__file__).with_name('history.sqlite3')).db, int(days[0]) if days else 14))
            sys.exit()
        if '--day' in sys.argv or '--state' in sys.argv:
            history = History(Path(__file__).with_name('history.sqlite3'))
            roles = history.db.execute('SELECT chat, topic, role, changed_at FROM conversation_state ORDER BY chat, topic').fetchall()
            attitudes = history.db.execute('SELECT chat, user, attitude, updated_at FROM participant_attitudes ORDER BY chat, user').fetchall()
            print(json.dumps({'roles': roles, 'attitudes': attitudes}, ensure_ascii=False, indent=2))
            sys.exit()
        read_only = '--read-only' in sys.argv or os.environ.get('READ_ONLY', '').lower() in ('1', 'true', 'yes')
        bot = Bot(read_only=read_only)
        if '--replay' in sys.argv:
            # python3 bot.py --replay 339669 [5] [--morale 1]: the request's id from the log, how many times to
            # answer it, and the optional 0–10 attitude override for replay.
            morale = int(sys.argv[sys.argv.index('--morale') + 1]) if '--morale' in sys.argv else None
            bot.replay(*map(int, [arg for arg in sys.argv[sys.argv.index('--replay') + 1:][:2] if arg.isdigit()]),
                       morale=morale)
        elif '--check' in sys.argv:
            print(f'Telegram OK: @{bot.me["username"]}', flush=True)
            print('Ollama: ' + bot.generate([{'role': 'user', 'content': 'Опять созвон, который мог быть сообщением.'}]))
        else:
            bot.run()
    except KeyboardInterrupt:
        print('\nОстановлен.')
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
