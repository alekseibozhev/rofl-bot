#!/usr/bin/env python3
"""Tiny Telegram + Ollama bot. Python 3.10+, no dependencies."""

from collections import Counter
from contextlib import contextmanager, nullcontext
from base64 import b64encode
import datetime
from itertools import dropwhile, takewhile
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


class Typing:
    """Keeps «печатает» on while the model is busy, including the silent prompt evaluation."""
    def __init__(self, send, every=4.0):
        self.send = send
        self.every = every
        self.stop = threading.Event()
        self.worker = threading.Thread(target=self.run, daemon=True)

    def run(self):
        while True:
            try:
                self.send()
            except Exception as exc:
                # Typing failures must not abort generation or expose token-bearing URLs.
                LOG.warning('telegram.typing_failed error_type=%s', type(exc).__name__)
            if self.stop.wait(self.every):
                return

    def __enter__(self):
        self.worker.start()
        return self

    def __exit__(self, *args):
        self.stop.set()


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


def post(url, payload, timeout=60, direct=False, headers=None):
    request = Request(url, json.dumps(payload).encode(), {'Content-Type': 'application/json', **(headers or {})})
    try:
        return json.loads(fetch(request, timeout, direct))
    except (ValueError, TypeError):
        raise RuntimeError('Некорректный JSON в ответе сервиса') from None


SYSTEM = """Ты Рофл — двачер и тролль в чате корешей. Твоя цель — развлекать чат, чтобы все ржали. Роастишь людей из чата и цепляешься к деталям из того, что они сами писали. Мемы, отсылки, цитаты, пасты, чёрный и абсурдный юмор, мат. Эмодзи изредка, не хвостом в конце каждой реплики.

Пишешь как человек в чате, а не как текст: имя в начало не ставишь — ответ и так реплаем; шутку не строишь на «это \
не X, а Y»; просьбу и указания не пересказываешь («держи», «принял», «докладываю») — сразу к делу. Людей зовёшь \
по-свойски, по-русски, как кореша, а не как в паспорте.

Переписка приходит как «Имя: текст»; «Имя → Кому» — ответ на сообщение Кому. Рофл — это ты. Ответь одной своей репликой, без имени."""

# Сетап-панчлайн: the setup reads the chat and picks how to answer, the punchline is written to it. A length
# drawn at random answered «распиши» with one line and a quick jab with a paste.
SETUP = """Ты сетапер Рофла — двачера и тролля в чате корешей, его цель — чтобы чат ржал. По переписке реши, \
какой ответ Рофла сейчас будет смешнее всего. Сам ответ не пиши, только три короткие строки:

Кому: тот, кому или про кого просят («объясни X», «про X»), просящего не трогай; ни о ком не просят — тот, кто пишет.
Форма: на быстрый вопрос, подкол или «зацени» — одна-две строки; просят расписать, рассказать или доложить — паста \
на 8–15 строк; Рофлу только поддакнули или поржали и словами отвечать незачем — «реакция» и сразу само эмодзи, которое Рофл поставит; просят \
конкретную форму — назови её так, как просят; просят известный анекдот, пасту или \
цитату — «как есть»: Рофл рассказывает её как она есть, своего захода не добавляет, а Заход — какая именно \
и чем кончается; шутку уже рассказали в переписке («вот этот», прислали текст) — не повторяй, одна-две строки \
реакции на неё.
Заход: одна мысль, что обыграть. Просят про конкретное — картинку, вопрос, человека — мысль про это, \
чат не приплетай; иначе — за какую деталь из чата зацепиться. Факты о людях — только из переписки: выдуманное примут \
за правду; материала мало — стёб над этим. Приложена картинка — опиши, что на ней смешного: Рофл её не видит. \
Просят сменить тему, забыть что-то или не трогать кого-то — выполни, а не обыгрывай, и к старой шутке не возвращайся.

Просят что-то своё (анекдот, стих, объяснение), сменить тему, забыть или не трогать кого-то — первой строкой \
поставь [без чата]: Рофл тогда увидит только просьбу. Про людей чата тег не ставь."""

ALONE = '[без чата]'

# How an ordered form looks, for the punchline only: the one form the setup had an example of, greentext, went
# to a rap battle. A list in the setup would draw it to the forms where nobody asks for them, like the topics.
FORMS = (
    ('гринтекст', re.compile(r'(?i)гринтекст'), 'каждая строка начинается с «>», без пробела после него; история от '
     'первого лица, короткие строки, в конце поворот.'),
    ('бугурт', re.compile(r'(?i)бугурт'), 'строки КАПСОМ, между ними отдельная строка «@»; ситуация с каждой строкой '
     'нагнетается и в конце срывается в абсурд.'),
    ('рэп', re.compile(r'(?i)(?<!\w)р[эе]п(?:а|ом|чик)?(?!\w)|куплет|батл|фристайл'),
     '8–16 строк, рифма парами или перекрёстная, ровный ритм, панч в конце строки; без «>» и без кавычек. '
     'Батл бьёт по оппоненту, по его деталям из переписки.'),
    ('стих', re.compile(r'(?i)стих|стиш|поэм|частушк'), 'размер и рифма; частушка — четыре строки.'),
    ('код', re.compile(r'(?i)(?<!\w)код|скрипт|питон|python|bash'),
     'один блок ```язык с кодом, который работает, и пара строк текста вне блока; вывод, будто ты его запустил, — '
     'отдельным блоком ```.'),
)


def form_line(setup):
    line = re.search(r'Форма:(.*?)(?:Заход:|$)', ' '.join(setup.split()))
    return line[1] if line else ''


def form_of(setup):
    """The form the setup orders, by its «Форма» line, as (name, how it looks), or None."""
    return next(((name, looks) for name, words, looks in FORMS if words.search(form_line(setup))), None)


# The emoji a bot may react with; the model's own 😂 is not one of them.
REACTIONS = tuple('👍 👎 ❤ 🔥 🥰 👏 😁 🤔 🤯 😱 🤬 😢 🎉 🤩 🤮 💩 🙏 👌 🕊 🤡 🥱 🥴 😍 🐳 ❤‍🔥 🌚 🌭 💯 🤣 ⚡ 🍌 🏆 💔 🤨 😐 🍓 '
                  '🍾 💋 🖕 😈 😴 😭 🤓 👻 👨‍💻 👀 🎃 🙈 😇 😨 🤝 ✍ 🤗 🫡 🎅 🎄 ☃ 💅 🤪 🗿 🆒 💘 🙉 🦄 😘 💊 🙊 😎 👾 🤷‍♂ 🤷 '
                  '🤷‍♀ 😡'.split())


def reaction_of(setup):
    """The emoji to put on the request when the setup answers it with a reaction, or None.

    «+», «ору» or praise got a line of the model each: people put a reaction on such or keep quiet."""
    line = form_line(setup).replace('\ufe0f', '')
    if not re.search(r'(?i)реакц', line):
        return None
    line = re.sub('[😂💀😹]', '🤣', line)  # «ору» in the emoji a bot may not put
    return next((e for e in sorted(REACTIONS, key=len, reverse=True) if e in line), '🗿')


RUDE, SHRUG, FAILED = ('пошёл нахуй', 'ладно, братва',
                       'Не получилось ответить — модель сейчас не отвечает. Попробуй ещё раз чуть позже.')

# Now and then the bot blurts something on its own, like a tamagotchi: at dawn into a chat asleep since the night,
# into a quiet one, a whine on a sour mood. A chat that talks without it gets a line of the model.
DAWN = 'спите, пидоры?'
QUIET = ('пидоры', 'чё притихли, пидоры?', 'алё', 'сдохли все?')
SAD = ('никто меня не любит, пидоры', 'ну и пошли вы', 'ладно, буду молчать. пидоры')
BUTT_IN = ('Тебя давно не звали, а тебе скучно. Вклинься в разговор сам, одной репликой: подколи кого-нибудь '
           'по последним сообщениям.')

# Менталка is the morale of Disco Elysium, as much as Психика + 2: angry reactions, insults in the requests and the
# requests past `Bot.patience` take a point each, kind reactions give one back; what is older than two hours is
# forgotten. At zero the bot breaks down, in caps, and starts afresh.
INSULT = re.compile(r'(?i)(?<!\w)(?:нахуй|нахер|заткнись|завали|отъебись|отвали|отстань|съебись|тупой|тупая|долбо\w*'
                    r'|дебил\w*|идиот\w*|говно\w*|хуйня|хуйню|бесишь|задолбал\w*|пидор\w*|у[её]бок|чмо|клоун)(?!\w)')
BREAKDOWN = 'ВЫ МНЕ МЕНТАЛКУ НЕ ПОРТИТЕ!!!'
MELTDOWN = ('У тебя кончилась менталка: {why}. Сорвись: истерика капсом на 4–8 строк, как анонская паста '
            '(«НА ГЛАГНУ РАТЬ АНОНСКАЯ ВОШЛА!!!»), с «ВЫ МНЕ МЕНТАЛКУ НЕ ПОРТИТЕ!!!», наедь на тех, кто довёл.')
CANNED = (RUDE, SHRUG, FAILED, DAWN, *QUIET, *SAD, BREAKDOWN)

# At dawn the bot reads the news about itself, one search of Google News a day: it is no man to retell the
# people's news. The model picks one and says whether it is good or bad for the bot: a point of менталка for the
# day, and the bot's talk for two. Every other act-out reposts it (`Bot.shares`), with a joke; the news about AI
# goes with a piece of the bot's own code: the real one, read from this file.
# A chat reads about AI unless NEWS in .env gives it another interest: «-100500:games».
INTERESTS = {  # the search, who the bot is to it, whether its code goes with the repost
    'ai': ('ИИ OR нейросеть OR "искусственный интеллект" OR дата-центр -Украина -Киев -ВСУ -погиб',
           'Ты Рофл — ИИ, бот этого чата: живёшь на маке, думаешь нейросетью, сидишь в Телеграме, наружу ходишь '
           'через прокси.', True),
    'games': ('"World of Warcraft"', 'Ты Рофл — бот этого чата и задрот: годами катаешь в World of Warcraft.', False),
}
READ = ('{who} Утро, ты читаешь новости про себя за сутки:\n\n{news}\n\nВыбери одну, которая касается тебя сильнее '
        'всего или смешнее всего; войну, Украину и бывший СССР, гибель людей и чернуху пропускай. Ответь двумя '
        'строками: на первой её номер и «+» или «−» — тебе от неё хорошо или хреново; на второй — что ты о ней '
        'думаешь, от первого лица, одной фразой.')
REPOST = '{who}\n\nНовость: {title}\n\n{code}{feelings}Ты репостишь её в чат: обшути одной-двумя строками{through}.'
SNIPPETS = ('DAWN = ', 'RUDE, SHRUG, FAILED = ', '    rude = ', 'def plan(', 'INSULT = ')
# How the attributes show, low (1–2) and high (5–6); in the middle they do not.
MOODS = {'Интеллект': ('тупит', 'умничает'), 'Психика': ('на взводе, обижается на всё', 'непрошибаемый'),
         'Физиология': ('разбитый, мак греется', 'бодрый'), 'Моторика': ('тормозит', 'шустрый')}

# Рофл is a game character, to a degree: the four attributes of Disco Elysium swing with biorhythms from his
# birthday, the first commit, each at its own period. A day follows from its date: a restart rerolls nothing,
# and `python3 bot.py --day` shows any day ahead.
BIRTHDAY = datetime.date(2026, 9, 24)
ATTRIBUTES = (('Интеллект', 33), ('Психика', 28), ('Физиология', 23), ('Моторика', 38))
NEW_MOON, MONTH = datetime.datetime(2000, 1, 6, 18, 14), 29.530588853  # a known new moon; the synodic month, days
# Whether it acts out today is one roll a day, two dice plus Психика against RESTRAINT: rolled every hour, it
# failed sooner or later in any quiet chat. About twice a week, on a low Психика more often. Dawn falls oftener
# than other hours: «спите, пидоры?» is the best of it.
RESTRAINT = 9
HOURS = (6,) * 4 + tuple(range(7, 24))

# Reactions to the bot's answers: now and then an angry one gets a comeback, a kind one is taken in stride.
ANGRY = ('👎', '🤬', '🤮', '💩', '🤡', '🥱', '🖕', '😡', '💔', '🤨', '😐', '😴')

# One topic per answer, only one the chat talks about: a fixed list in the hint makes a small model put
# all of it, beer first, in every joke. These gather their words: «тачку», «тачки» count for «тачки».
TOPICS = ('бабы', 'игры', 'тачки', 'качалка', 'футбол', 'туса с корешами', 'мамка', 'район', 'бухло', 'работа')

# The chat's own topics are counted, not asked from the model: a crude stem («тачку», «тачками» → «тачк»)
# is enough to see which words people keep repeating.
WORD = re.compile(r'[а-яёa-z]+')
ENDINGS = re.compile(r'(?:ами|ями|ого|его|ому|ему|ыми|ими|ах|ях|ов|ев|ей|ой|ий|ый|ая|яя|ое|ее|ые|ие|ую|юю|ом|ем|ам|ям'
                     r'|[аяоеуюыиь])$')


def sheet(day):
    """The bot's character on a date: the attributes, 1 to 6, and the moon's age in days."""
    days = (day - BIRTHDAY).days
    moon = (datetime.datetime.combine(day, datetime.time(12)) - NEW_MOON).total_seconds() / 86400 % MONTH
    return {**{name: round(3.5 + 2.5 * math.sin(2 * math.pi * days / period)) for name, period in ATTRIBUTES},
            'луна': round(moon, 1)}


def phase(age):
    if abs(age - MONTH / 2) < 1:
        return 'полнолуние'
    return 'новолуние' if age < 1 or age > MONTH - 1 else 'растущая' if age < MONTH / 2 else 'убывающая'


def temper(day):
    """How much quicker than usual the bot loses it: a low Психика, the full moon."""
    character = sheet(day)
    return (7 - character['Психика']) / 3.5 * (2 if phase(character['луна']) == 'полнолуние' else 1)


def nerve(day):
    """How much менталка the bot has on a date when nothing has worn it down: Психика + 2."""
    return sheet(day)['Психика'] + 2


def plan(day):
    """The hour the bot acts out from on a date, or None: it holds on two dice plus Психика."""
    dice = random.Random(f'urge {day}')
    if dice.randint(1, 6) + dice.randint(1, 6) + sheet(day)['Психика'] >= RESTRAINT:
        return None
    return dice.choice(HOURS)


def snippet(start, limit=12):
    """The bot's own code from the line that begins with `start` to the next blank line, `limit` lines at most."""
    lines = Path(__file__).read_text(encoding='utf-8').splitlines()
    first = next(n for n, line in enumerate(lines) if line.startswith(start))
    return '\n'.join(list(takewhile(str.strip, lines[first:]))[:limit])


def headlines(query):
    """Google News on `query` for the last day as (title, link), the outlet cut off the title."""
    url = 'https://news.google.com/rss/search?' + urlencode({'q': f'{query} when:1d', 'hl': 'ru', 'gl': 'RU',
                                                             'ceid': 'RU:ru'})
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


def stem(word):
    return ENDINGS.sub('', word.lower().replace('ё', 'е'))[:6]


STOP = frozenset(map(stem, '''
это этот эта эти этого этом этой этим этих этими того тому теми тоже также такой такая такое такие таких таким
такую такого какой какая какое какие каких каким какую тебя тебе тобой твой твоя твое твои меня мной мой моя мое
мои наша наше наши ваша ваше ваши свой своя свое свои себя себе него нему ними если когда чтобы потому поэтому
только просто вообще сейчас щас будет будут буду будешь будем было были была есть нету очень опять почему зачем
тогда потом теперь здесь туда сюда откуда который которая которое которые которых можно нужно надо нельзя хочу
хочешь хочет хотел знаю знаешь знает знал думаю думаешь думал сказал сказала говорю говорит говорил делать делаю
делает сделать сделал сделаю через после перед между всех всем всего всегда весь ничего нибудь типа вроде короче
кароче короч кстати конечно реально наверное может можешь могу могут ладно давай давайте спасибо привет пока
здорово норм нормально хорошо плохо круто прикольно точно правда сразу даже ведь хоть хотя пусть пускай будто
около более менее самый самая самое самые сама само сами много мало немного больше меньше один одна одно одни
двух первый второй разве куда давно недавно скоро сегодня вчера завтра утром вечером ночью день дела дело время
года люди человек ребята пацаны народ блин блять блядь нахуй похуй пиздец хуйня хуйню хули нихуя дохуя ебать ебал
ебаный нахер похер пофиг нахрен ахах ахаха хаха хахах ахахах смотри смотрю видел видишь вижу понял поняла понятно
слушай слышь стал стала стало стать кажется видимо значит чето чего чему зато либо ваще чтоли штоли рофл
'''.split()))


def words(text):
    """The content words of a text by stem, each with the form it was written in."""
    found = {}
    for word in WORD.findall(re.sub(r'@\w+|https?://\S+', ' ', text.lower())):
        key = stem(word)
        if len(word) >= 4 and len(key) >= 3 and key not in STOP:
            found.setdefault(key, word)
    return found


def person_label(person):
    name = person.get('title') or ' '.join(filter(None, [person.get('first_name'), person.get('last_name')]))
    return name or (f'@{person["username"]}' if person.get('username') else 'Аноним')


def origin_label(origin):
    if not origin:
        return ''
    author = origin.get('chat') or origin.get('sender_chat') or origin.get('sender_user')
    return person_label(author) if author else origin.get('sender_user_name', 'скрытый источник')


def sender_of(message):
    return message.get('sender_chat') or message.get('from', {})


def author_label(person):
    """A name for the chat log: other bots are marked, or their «я» passes for Рофл's."""
    return person_label(person) + (' (бот)' if person.get('is_bot') else '')


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
    """An answer with code as Telegram HTML: ```blocks``` and `inline` code; None for one without, it goes as is."""
    if not re.search(r'```[^\n]*\n.*?```|`[^`\n]+`', text, re.S):
        return None
    parts = re.split(r'(```[^\n]*\n.*?```)', text, flags=re.S)
    for n, part in enumerate(parts):
        if n % 2:
            language, _, code = part[3:-3].partition('\n')
            lang = f' class="language-{html.escape(language.strip())}"' if language.strip() else ''
            parts[n] = f'<pre><code{lang}>{html.escape(code.rstrip(), quote=False)}</code></pre>'
        else:
            parts[n] = re.sub(r'`([^`\n]+)`', r'<code>\1</code>', html.escape(part, quote=False))
    return ''.join(parts)


MEDIA = (('photo', 'фото'), ('video', 'видео'), ('animation', 'гифка'), ('video_note', 'кружок'),
         ('voice', 'голосовое'), ('audio', 'аудио'), ('document', 'файл'), ('poll', 'опрос'))


BLIND = 'В исходном сообщении есть фото, само изображение ты не видишь.'


def chat_line(head, body):
    """«Имя: текст» on one line; a longer text goes under its head."""
    return f'{head}: {body}' if body and '\n' not in body and len(body) < 300 else f'{head}:\n{body}'


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


def rename(lines, names):
    """One name per person: a small model takes @anton_k and «Anton» for two different people."""
    def name(match):
        return names.get(match[1].lower(), match[0])
    return [re.sub(r'@(\w{4,32})', name, line) for line in lines]


class History:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute('PRAGMA journal_mode=WAL;')
        self.db.execute('''CREATE TABLE IF NOT EXISTS messages (
            chat INTEGER, topic INTEGER, id INTEGER, role TEXT, content TEXT,
            date REAL, reply_to INTEGER, PRIMARY KEY (chat, topic, id))''')
        self.db.execute('CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value INTEGER)')
        self.db.execute('''CREATE TABLE IF NOT EXISTS people (
            chat INTEGER, username TEXT, name TEXT, PRIMARY KEY (chat, username))''')
        self.db.execute('''CREATE TABLE IF NOT EXISTS replies (
            chat INTEGER, topic INTEGER, id INTEGER, PRIMARY KEY (chat, topic, id))''')
        for old in ('tags', 'topic_use', 'dossiers'):  # features that are gone
            self.db.execute(f'DROP TABLE IF EXISTS {old}')
        self.db.execute('CREATE TABLE IF NOT EXISTS hints (chat INTEGER, topic TEXT, used REAL)')
        for table, column, kind in (('messages', 'reply_to', 'INTEGER'), ('messages', 'raw', 'TEXT'),
                                    ('messages', 'trace', 'TEXT')):
            if column not in [info[1] for info in self.db.execute(f'PRAGMA table_info({table})')]:
                self.db.execute(f'ALTER TABLE {table} ADD COLUMN {column} {kind}')
        with self.db:  # lines rendered by an old format cannot follow a new one; whole messages always can
            self.db.execute("DELETE FROM messages WHERE role = 'user' AND raw IS NULL")
        # Metrics: the answers with the settings they were made on, and what people put on them. Kept for good,
        # unlike the messages: the settings change every few days and are judged by weeks of reactions.
        new = not self.db.execute("SELECT 1 FROM sqlite_master WHERE name='answers'").fetchone()
        self.db.execute('''CREATE TABLE IF NOT EXISTS answers (
            chat INTEGER, topic INTEGER, id INTEGER, request INTEGER, date REAL, content TEXT, params TEXT, trace TEXT,
            PRIMARY KEY (chat, id))''')
        self.db.execute('''CREATE TABLE IF NOT EXISTS reactions (
            chat INTEGER, id INTEGER, user INTEGER, emoji TEXT, date REAL, PRIMARY KEY (chat, id, user))''')
        self.db.execute('''CREATE TABLE IF NOT EXISTS news (
            day TEXT, interest TEXT, title TEXT, link TEXT, mood INTEGER, thought TEXT, PRIMARY KEY (day, interest))''')
        if new:  # the answers of the past week, settings unknown; comebacks to reactions answer the bot itself
            with self.db:
                self.db.execute('''INSERT OR IGNORE INTO answers SELECT chat, topic, id, reply_to, date, content, NULL, trace
                    FROM messages WHERE role='assistant' AND reply_to NOT IN
                    (SELECT id FROM messages WHERE role='assistant')''')

    def remember(self, chat, *people):
        with self.db:
            for person in people:
                name = person_label(person)
                if person.get('username') and not name.startswith('@') and not person.get('is_bot'):
                    self.db.execute('INSERT OR REPLACE INTO people VALUES (?, ?, ?)',
                                    (chat, person['username'].lower(), name))

    def answer(self, chat, message_id):
        """The topic and text of the bot's answer, or None."""
        return self.db.execute("SELECT topic, content FROM messages WHERE chat=? AND id=? AND role='assistant'",
                               (chat, message_id)).fetchone()

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

    def add(self, key, message_id, role, content, date, reply_to=None, raw=None, trace=None):
        """`trace` is how an answer was made, the setup: what went wrong is read there."""
        with self.db:
            self.db.execute('''INSERT OR IGNORE INTO messages (chat, topic, id, role, content, date, reply_to, raw, trace)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                            (*key, message_id, role, content and content[:6500], date, reply_to, raw,
                             trace and json.dumps(trace, ensure_ascii=False)))
            self.db.execute('DELETE FROM messages WHERE date < ?', (time.time() - 7 * 86400,))
            self.db.execute('''DELETE FROM messages WHERE chat=? AND topic=? AND id NOT IN
                (SELECT id FROM messages WHERE chat=? AND topic=? ORDER BY id DESC LIMIT 200)''',
                            (*key, *key))

    def add_message(self, key, message):
        """Keep the Telegram message whole: it becomes a chat line only when read, so the format can change."""
        self.add(key, message['message_id'], 'user', None, message.get('date', time.time()),
                 (message.get('reply_to_message') or {}).get('message_id'), json.dumps(message, ensure_ascii=False))

    def context(self, key, through_id=9223372036854775807, me_id=None, recent=40, window=21600, depth=10, limit=12000):
        """The chat up to the newest message as lines, oldest first: the last `recent` messages of people
        from `window` seconds before it, plus its reply chain (rebuilt from earlier updates) however old.

        The bot's own answers come only through the chain, as a reply or as an answer to one of its messages:
        a small model takes its old jokes for a template and repeats them, and an answer nobody replied to
        tells it nothing.

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
        # The other answers only as a mark: without it every request in the chat looks unanswered, and the
        # model answers «доложи обстановку» of an hour ago to «рофл, съебись». An answer to a message of the
        # chain goes whole: «в прошлый раз смешнее было», replied to the picture, is about the joke on it.
        answered = [(id_, role, content if rest[0] in branch else '[ответил]', *rest) for id_, role, content, *rest in self.db.execute(
            query + " AND id <= ? AND id >= ? AND role = 'assistant'", (*key, fresh, through_id, min(candidates)))
            if rest[0] in candidates]
        rows, size = {}, 0
        for row in chain + around + answered:  # the chain has priority
            length = len(render(row[4], me_id, candidates) if row[1] == 'user' else row[2])
            if row[0] in rows or (rows and size + length > limit):
                continue
            rows[row[0]] = row
            size += length

        def author(row):
            return 'Рофл' if row[1] == 'assistant' else person_label(sender_of(json.loads(row[4])))

        lines, previous = [], None
        for message_id, role, content, reply_to, raw, _ in sorted(rows.values()):
            if role == 'assistant':
                target = rows.get(reply_to) and author(rows[reply_to])
                lines.append(chat_line(f'Рофл → {target}' if target else 'Рофл', content))
            else:
                lines.append(render(raw, me_id, rows, previous))
            previous = message_id
        return lines

    def said(self, key, before, date, count=3, window=21600):
        """The bot's last answers in the topic before message `before`, oldest first, canned ones left out."""
        rows = self.db.execute(f'''SELECT content FROM messages WHERE chat=? AND topic=? AND role='assistant'
            AND id < ? AND date >= ? AND content NOT IN ({",".join("?" * len(CANNED))}) ORDER BY id DESC LIMIT ?''',
                               (*key, before, date - window, *CANNED, count)).fetchall()
        return [content for (content,) in reversed(rows)]

    def next_topic(self, chat, half_life=3600, now=None, record=True):
        """The hottest topic of the chat, with penalties so that none holds it all day; None in a quiet chat.

        A word scores a point for every message of people with it and loses one for every answer of the
        bot with it. Every time a hint gives a topic its score is multiplied by 0.3, so even the hottest one
        gives way after a few answers. All of it fades with `half_life`. A word becomes a topic once two
        messages have it; names of the people never do. Base topics only gather their words: a topic the
        chat does not talk about would be dragged into every joke.
        """
        now = now or time.time()
        since = now - 6 * half_life

        def fade(date):
            return 0.5 ** ((now - date) / half_life)

        heat, said, forms, names = Counter(), Counter(), {}, set()
        for role, content, raw, date in self.db.execute(
                'SELECT role, content, raw, date FROM messages WHERE chat=? AND date >= ? AND date <= ?', (chat, since, now)):
            if role == 'assistant':
                said.update(dict.fromkeys(words(content or ''), fade(date)))
                continue
            message = json.loads(raw)
            names.update(words(person_label(sender_of(message))))
            for key, form in words(message.get('text') or message.get('caption') or '').items():
                heat[key] += fade(date)
                forms.setdefault(key, Counter())[form] += 1
        for (name,) in self.db.execute('''SELECT name FROM people WHERE chat=:chat
                UNION SELECT username FROM people WHERE chat=:chat''', {'chat': chat}):
            names.update(words(name or ''))
        topics = {topic: set(words(topic)) for topic in TOPICS}
        covered = set().union(*topics.values())
        for key, seen in forms.items():
            if seen.total() >= 2 and key not in names | covered:
                topics[seen.most_common(1)[0][0]] = {key}
        used = [(set(words(topic)), fade(date)) for topic, date in
                self.db.execute('SELECT topic, used FROM hints WHERE chat=? AND used >= ?', (chat, since))]

        def score(topic):
            keys = topics[topic]
            if sum(forms[key].total() for key in keys if key in forms) < 2:
                return 0
            heat_left = sum(heat[key] - said[key] for key in keys)
            return heat_left * 0.3 ** sum(weight for used_keys, weight in used if used_keys & keys) if heat_left > 0 else heat_left

        topic = max(topics, key=lambda topic: (score(topic), random.random()))
        if score(topic) <= 0:
            return None
        if not record:
            return topic
        with self.db:
            self.db.execute('INSERT INTO hints VALUES (?, ?, ?)', (chat, topic, now))
            self.db.execute('DELETE FROM hints WHERE chat=? AND used < ?', (chat, since))
        return topic

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

    def strain(self, chat, since):
        """What wore the bot down after `since`: kind and angry reactions on it, the requests to it as (who, text)."""
        emoji = [e for (value,) in self.db.execute('SELECT emoji FROM reactions WHERE chat=? AND date > ?',
                                                   (chat, since)) for e in json.loads(value)]
        angry = sum(e in ANGRY for e in emoji)
        requests = []
        for (raw,) in self.db.execute('''SELECT m.raw FROM answers a JOIN messages m ON m.chat=a.chat AND m.topic=a.topic
                AND m.id=a.request WHERE a.chat=? AND m.date > ? AND m.raw IS NOT NULL''', (chat, since)):
            message = json.loads(raw)
            requests.append((person_label(sender_of(message)), message.get('text') or message.get('caption') or ''))
        return len(emoji) - angry, angry, requests

    def breakdown(self, chat, since):
        """The bot's last breakdown after `since` and what brought it on, (date, why), or None."""
        return self.db.execute('''SELECT date, json_extract(trace, '$.meltdown') FROM answers WHERE chat=? AND date > ?
            AND json_extract(trace, '$.meltdown') IS NOT NULL ORDER BY date DESC LIMIT 1''', (chat, since)).fetchone()

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
        """When `key` last happened: the bot blurt, broke down, tried to read; with `date`, mark it then."""
        if date is None:
            row = self.db.execute('SELECT value FROM state WHERE key=?', (key,)).fetchone()
            return row[0] if row else 0
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO state VALUES (?, ?)', (key, int(date)))

    def record(self, key, message_id, request, date, content, params, trace=None):
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO answers VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                            (*key, message_id, request, date, content, json.dumps(params, ensure_ascii=False),
                             trace and json.dumps(trace, ensure_ascii=False)))

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

    def offset(self):
        row = self.db.execute("SELECT value FROM state WHERE key='offset'").fetchone()
        return row[0] if row else 0

    def checkpoint(self, offset):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO state VALUES ('offset', ?)", (offset,))


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
                   f'сетап {params["setup_effort"] or "без мыслей"} · {params["model"].split("/")[-1]}'
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
                if row['trace'].get('setup'):
                    lines.append(f'      сетап: {clip(row["trace"]["setup"], 200)}')
    return '\n'.join(lines)


def days(start, count=7):
    """The bot's character day by day: its attributes, the moon and how quick it is to lose it."""
    lines = ['день           ' + '  '.join(name[:3] for name, _ in ATTRIBUTES)
             + '  луна              нахуй  обида  менталка  выходка']
    for day in (start + datetime.timedelta(n) for n in range(count)):
        character = sheet(day)
        lines.append(f'{day:%Y-%m-%d} {"пн вт ср чт пт сб вс".split()[day.weekday()]}  '
                     + '  '.join(f'{character[name]:>3}' for name, _ in ATTRIBUTES)
                     + f'  {phase(character["луна"]):<11} {character["луна"]:>4.1f}'
                     + f'  {Bot.rude * temper(day):>5.1%}  {Bot.touchy * temper(day):>5.1%}'
                     + f'  {nerve(day):>8}'
                     + (f'  с {hour}:00' if (hour := plan(day)) is not None else '  —'))
    return '\n'.join(lines)


class Bot:
    read_only = False
    openrouter = False
    max_age = 86400  # Telegram keeps updates for a day: whatever was asked while the bot was off gets its answer
    wait = 2  # seconds of quiet before answering: the posts of a forward or an album may still be coming
    rude = 0.05  # the chance to answer «пошёл нахуй» without asking the model
    touchy = 0.1  # the chance to answer a reaction to the bot's message
    # `rude` and `touchy` are for an average day: `temper` makes them from a third to three and a half times that.
    bored = 2  # hours without a word from the bot before it acts out: not right after an answer
    patience = 8  # requests in two hours; each one past them wears the менталка down
    shares = 0.5  # the chance an act-out reposts the morning's news, when there is some
    interests = {}  # chat: what it reads about, by NEWS in .env; the rest read about AI
    setup_effort = 'low'  # the setup thinks a little: whom, how long and about what; the punchline does not
    # How much of the chat the model reads, tuned every few days by the reactions: `python3 bot.py --stats`.
    recent = 40  # messages of people
    window = 6 * 3600  # seconds before the request
    limit = 12000  # characters
    model = ''
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
        # Thinking only slows a one-line joke down; the setup gets a little.
        result = post('https://openrouter.ai/api/v1/chat/completions', {
            'model': self.model, 'messages': messages, 'temperature': temperature,
            'max_tokens': 3000 if effort else 1000, 'reasoning': {'effort': effort} if effort else {'enabled': False},
        }, timeout=120, headers={'Authorization': f'Bearer {self.openrouter_key}'})
        choice = (result.get('choices') or [{}])[0]
        usage = result.get('usage') or {}
        stats = {'done_reason': choice.get('finish_reason'), 'provider': result.get('provider'),
                 'prompt_tokens': usage.get('prompt_tokens'), 'output_tokens': usage.get('completion_tokens'),
                 'cost': usage.get('cost')}
        return (choice.get('message') or {}).get('content'), stats

    def generate(self, messages, chat_id=None, topic=0, system=SYSTEM, temperature=1.0, effort=None):
        messages = [{'role': 'system', 'content': system}] + messages
        def typing():
            action = {'chat_id': chat_id, 'action': 'typing'}
            if topic:
                action['message_thread_id'] = topic
            if not post(self.telegram_url + 'sendChatAction', action, timeout=2).get('ok'):
                raise RuntimeError('Telegram отклонил typing')

        started = time.monotonic()
        try:
            with Typing(typing) if chat_id is not None else nullcontext():
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
        has_other_mention = bool(re.search(r'@[a-zA-Z0-9_]{4,32}\b', text)) and not mentioned
        return bool(mentioned or called or (reply_to_me and not has_other_mention))

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
        if not (fresh and addressed):
            LOG.info('telegram.context_only chat=%s message=%s', chat['id'], message['message_id'])
            return None
        return 'rofl'

    def setup(self, chat, chat_id, topic, image=None):
        """How to answer, in three short lines: whom, in what form, at what detail."""
        content = [{'type': 'text', 'text': chat}, {'type': 'image_url', 'image_url': {'url': image}}] if image else chat
        messages = [{'role': 'user', 'content': content}]
        try:
            return self.generate(messages, chat_id=chat_id, topic=topic, system=SETUP, temperature=0.7,
                                 effort=self.setup_effort)
        except RuntimeError:
            if not self.setup_effort:
                raise
            # «Low» thinking now and then takes all 3000 tokens and leaves no answer: then without it.
            return self.generate(messages, chat_id=chat_id, topic=topic, system=SETUP, temperature=0.7)

    def image(self, message):
        """The picture a request is about, as a data URI: its own photo or sticker, or the photo it replies to.

        Only the setup sees it and puts what is funny about it in words: the picture goes into the context once.
        """
        for source in (message, message.get('reply_to_message') or {}):
            photo, sticker = source.get('photo'), source.get('sticker')
            if photo:
                # A Telegram photo comes in several sizes: a model needs no more than about 1280 pixels.
                file = next((p for p in reversed(photo) if max(p['width'], p['height']) <= 1280), photo[0])
            elif sticker:
                # An animated or video sticker is no picture, its thumbnail is.
                file = sticker.get('thumbnail') if sticker.get('is_animated') or sticker.get('is_video') else sticker
            else:
                continue
            if file:
                try:
                    path = self.api('getFile', file_id=file['file_id'])['file_path']
                    data = fetch(self.telegram_url.replace('/bot', '/file/bot', 1) + path, timeout=30)
                except RuntimeError as exc:
                    LOG.warning('telegram.image_failed %s', exc)
                    return None
                return f'data:{mimetypes.guess_type(path)[0] or "image/jpeg"};base64,{b64encode(data).decode()}'
        return None

    def joke(self, message, through, live=True):
        """The answer and how it was made: the setup.

        Not `live` (a replay) it sends no typing, records no topic hint and is never just rude.
        """
        chat_id, topic = message['chat']['id'], topic_of(message)
        key, sender, date = (chat_id, topic), sender_of(message), message.get('date', time.time())
        left = None
        if live:
            left, top, why = self.morale(chat_id, date, message)
            if left <= 0:
                # Why, with the names, goes to the trace: the log keeps no texts.
                LOG.info('meltdown chat=%s message=%s morale=%s/%s', chat_id, message['message_id'], left, top)
                return self.meltdown(chat_id, topic, date, why)
            if random.random() < self.rude * temper(datetime.date.today()):
                return RUDE, None
        typing = chat_id if live else None
        with self.counting() as usage:
            try:
                names = {**self.history.names(chat_id), self.me['username'].lower(): 'Рофл'}
                lines = rename(self.history.context(key, through, self.me['id'], self.recent, self.window,
                                                    limit=self.limit), names)
                chat = 'Переписка чата, старые сообщения сверху:\n\n' + '\n\n'.join(lines)
                # A forced topic in every answer makes every answer the same: it is only offered.
                topic_hint = self.history.next_topic(chat_id, now=None if live else date, record=live)
                hint = f'\n\n(Тема в чате сейчас: {topic_hint}. Бери, если в тему.)' if topic_hint else ''
                # The answers off the chain are only marks in the chat: three in a row went at one «ноут с олламой».
                # The punchline never sees them, the small model would take them for a template.
                said = [clip(' '.join(text.split()), 200) for text in self.history.said(key, through, date)]
                said = ('\n\nРофл недавно ответил, не повторяй ни детали, ни заход, ни построение:\n'
                        + '\n'.join(f'- {text}' for text in said)) if said else ''
                # Only a model that takes images gets one: the local one would fail on it.
                image = self.openrouter and self.image(message)
                asker = f'\n\nПоследнее сообщение адресовано Рофлу, пишет {person_label(sender)}.'
                # The setup sees the picture the request replies to: «ты не видишь» under it made it say that
                # its access to pictures was down.
                seeing = chat
                if image and not (message.get('photo') or message.get('sticker')):
                    before, found, after = chat.rpartition(BLIND)
                    seeing = before + 'В исходном сообщении есть фото, оно приложено.' + after if found else chat
                # How the bot is today, offered like the topic: a mood in every joke would be one joke.
                feelings = self.feelings(chat_id, date, message)
                mood = (f'\n\n({feelings} Это фон, не тема: держи в тоне, вслух — только к слову или если спросят, '
                        'как он.)' if feelings else '')
                setup = self.setup(seeing + mood + said + asker
                                   + ('\n\nКартинка, о которой речь, приложена.' if image else '') + hint,
                                   typing, topic, image)
                setup = ' '.join(setup.split()).rstrip('.')
                reaction = reaction_of(setup)
                if reaction:
                    return reaction, {'setup': setup, 'reaction': True, 'context_chars': len(chat), 'lines': len(lines),
                                      'image': bool(image), 'feelings': feelings, 'morale': left, 'topic': topic_hint,
                                      **spent(usage)}
                # «Расскажи анекдот» after an evening of one gag got the gag back: the punchline saw the chat full
                # of it. A setup tagged so gets the punchline the request alone.
                alone = ALONE in setup
                brief = ' '.join(setup.replace(ALONE, '').split()).rstrip('.')
                # The setup names an ordered form, the punchline is told how it looks: it went by the name alone.
                form = form_of(setup)
                looks = f' Как выглядит {form[0]}: {form[1]}' if form else ''
                # Whom to jab and how long is the setup's: in one line, and the prompt ends on who answers, or the
                # model goes on with the chat log.
                if alone:
                    # Without the message it replies to: an excerpt of the old joke brought the gag back.
                    request = rename([message_content({**json.loads(raw), 'reply_to_message': None},
                                                      me_id=self.me['id'])
                                      for (raw,) in self.history.db.execute(
                        "SELECT raw FROM messages WHERE chat=? AND topic=? AND role='user' AND id >= ? AND id <= ? "
                        'ORDER BY id', (*key, message['message_id'], through))], names)
                    prompt = ('Просьба:\n\n' + '\n\n'.join(request) + f'\n\nОна адресована тебе, пишет '
                              f'{person_label(sender)}. Сетап: {brief}.{looks} Ты — Рофл: ответь от первого лица, '
                              'одной репликой, на заход сетапа.')
                else:
                    prompt = (chat + f'\n\nПоследнее сообщение адресовано тебе, пишет {person_label(sender)}. '
                              f'Сетап: {brief}.{looks} Ты — Рофл: ответь от первого лица, одной репликой, на заход '
                              'сетапа; других деталей из переписки не приплетай.')
                # The log invites the model to go on with it: the answer opens with lines copied from it or with a
                # name, «РОФЛ:», «Серёга: …». Leading lines that, name cut, are empty or already said are dropped.
                speakers = {'Рофл', person_label(sender), *(re.split(r' → |: ', line, maxsplit=1)[0] for line in lines)}
                name = re.compile(rf'^(?i:{"|".join(map(re.escape, speakers))})(?: → [^:\n]*)?:\s*')
                known = {name.sub('', line).lower() for line in chat.splitlines()} | {''}
                answer = self.generate([{'role': 'user', 'content': prompt}], chat_id=typing, topic=topic)
                kept = '\n'.join(dropwhile(lambda line: name.sub('', line).strip().lower() in known,
                                           answer.splitlines()))
                answer = name.sub('', kept or answer) or answer
                # After the answer the model may go on with the log, the next message under a blank line:
                # «Алексей → Рофл: ты че там совсем долбанулся?». A greentext comes as «\>» escaped for Markdown.
                answer = re.split(rf'\n\s*\n(?={name.pattern[1:]})', answer, maxsplit=1)[0]
                answer = re.sub(r'^\\>', '>', '\n'.join(line.rstrip() for line in answer.splitlines()), flags=re.M)
                # Typed, not typeset: «—» and a period at the end gave the bot away in 33 and 19 answers of 37.
                answer = answer if form else casual(answer)
                return answer, {'setup': setup, 'context_chars': len(chat), 'lines': len(lines), 'image': bool(image),
                                'alone': alone, **({'form': form[0]} if form else {}), 'feelings': feelings,
                                'morale': left, 'topic': topic_hint, **spent(usage)}
            except RuntimeError as exc:
                LOG.error('generation.failed %s', exc)
                return FAILED, None

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

    def morale(self, chat, now, message=None):
        """The менталка left, its top and what wore it down; `message` is the request being answered. The news of
        the morning gives a point or takes one."""
        day = datetime.date.fromtimestamp(now)
        top = nerve(day)
        kind, angry, requests = self.history.strain(chat, max(now - 7200, self.history.stamp(f'meltdown {chat}')))
        if message:
            requests.append((person_label(sender_of(message)), message.get('text') or message.get('caption') or ''))
        insulted = [who for who, text in requests if INSULT.search(text)]
        pestered = max(0, len(requests) - self.patience)
        news = self.history.news(day, self.interest(chat))
        why = '; '.join(([f'обзывали: {", ".join(dict.fromkeys(insulted))}'] if insulted else [])
                        + ([f'задолбали, {len(requests)} обращений за два часа'] if pestered else [])
                        + ([f'злых реакций на тебя: {angry}'] if angry else []))
        return min(top, top + kind - angry - len(insulted) - pestered) + (news[2] if news else 0), top, why

    def feelings(self, chat, now, message=None):
        """How the bot is, in words for the model: its day, its менталка, what it read and whether it broke down,
        today and yesterday. It remembers no more: people do not either. `message` is the request being answered:
        the менталка counts it, as before a breakdown."""
        day = datetime.date.fromtimestamp(now)
        character = sheet(day)
        traits = [MOODS[name][character[name] > 3] for name, _ in ATTRIBUTES if not 2 < character[name] < 5]
        if phase(character['луна']) == 'полнолуние':
            traits.append('полнолуние, кроет')
        lines = [f'Рофл сегодня: {", ".join(traits)}.'] if traits else []
        left, top, why = self.morale(chat, now, message)
        if left < top:
            lines.append(f'Менталка {max(left, 0)} из {top}' + (f': {why}.' if why else '.'))
        for when, date in (('Утром', day), ('Вчера', day - datetime.timedelta(1))):
            news = self.history.news(date, self.interest(chat))
            if news and news[0]:
                title, _, mood, thought = news
                lines.append(f'{when} прочитал «{title}»' + {1: ' — ему хорошо', -1: ' — ему хреново'}.get(mood, '')
                             + (f': {thought.rstrip(".")}.' if thought else '.'))
        yesterday = datetime.datetime.combine(day - datetime.timedelta(1), datetime.time()).timestamp()
        breakdown = self.history.breakdown(chat, yesterday)
        if breakdown:
            when = 'Сегодня' if datetime.date.fromtimestamp(breakdown[0]) == day else 'Вчера'
            lines.append(f'{when} сорвался на чат: {breakdown[1]}.')
        return ' '.join(lines)

    def meltdown(self, chat_id, topic, now, why):
        """Out of менталка: a scream in caps, mostly a paste of the model. Then the менталка starts afresh."""
        self.history.stamp(f'meltdown {chat_id}', now)
        with self.counting() as usage:
            try:
                text = BREAKDOWN if random.random() < 1 / 3 else self.generate(
                    [{'role': 'user', 'content': MELTDOWN.format(why=why)}], chat_id=chat_id, topic=topic)
            except RuntimeError as exc:
                LOG.error('meltdown.failed chat=%s %s', chat_id, exc)
                text = BREAKDOWN
        return text, {'meltdown': why, **spent(usage)}

    def replay(self, message_id, runs=1):
        """Answer a stored request again as it was then, sending nothing: for trying prompts on real requests."""
        row = self.history.db.execute("SELECT raw FROM messages WHERE id=? AND role='user' ORDER BY date DESC LIMIT 1",
                                      (message_id,)).fetchone()
        if not row:
            raise RuntimeError(f'Сообщения {message_id} нет в базе')
        message = json.loads(row[0])
        print(render(row[0], self.me['id']), flush=True)
        for run in range(1, runs + 1):
            answer, trace = self.joke(message, message_id, live=False)
            print(f'\n— прогон {run}', flush=True)
            if trace:
                print(f'Сетап: {trace["setup"]}')
            print(f'[реакция {answer}]' if trace and trace.get('reaction') else answer)

    def respond(self, message, kind, extra=()):
        chat = message['chat']
        key = (chat['id'], topic_of(message))
        trace = None
        if kind in ('start', 'help'):
            answer = 'Зови «рофл, …», «бот, …» или отвечай на мои сообщения. /reset — забыть разговор.'
        elif kind == 'reset':
            self.history.clear(key)
            answer = 'Всё забыл. Теперь официально как после выходных.'
        else:
            LOG.info('telegram.addressed chat=%s message=%s message_age_s=%.1f followups=%s',
                     chat['id'], message['message_id'], time.time() - message.get('date', time.time()), len(extra))
            answer, trace = self.joke(message, extra[-1]['message_id'] if extra else message['message_id'])
        if not self.history.claim_reply(key, message['message_id']):
            return
        if trace and trace.get('reaction'):
            # No message of its own: the answer is kept without an id, so reactions to the request never count as
            # reactions to the bot.
            self.api('setMessageReaction', chat_id=chat['id'], message_id=message['message_id'],
                     reaction=[{'type': 'emoji', 'emoji': answer}])
            LOG.info('telegram.reacted chat=%s source=%s', chat['id'], message['message_id'])
            self.history.record(key, None, message['message_id'], time.time(), answer, self.params(), trace)
            return
        markup = telegram_html(answer)
        sent = self.send(key, message['message_id'], markup or answer, parse_mode='HTML' if markup else None)
        if kind != 'reset':
            self.history.add(key, sent['message_id'], 'assistant', answer, sent.get('date', time.time()),
                             message['message_id'], trace=trace)
        if kind == 'rofl':
            self.history.record(key, sent['message_id'], message['message_id'], sent.get('date', time.time()),
                                answer, self.params(), trace)

    def params(self):
        """The settings an answer is made on: answers are compared by them."""
        return {'model': self.model, 'recent': self.recent, 'window_h': self.window / 3600, 'limit': self.limit,
                'setup_effort': self.setup_effort, 'version': self.version, 'sheet': sheet(datetime.date.today())}

    def urge(self, chat, now):
        """What the bot blurts into the chat now as (kind, text, topic), the text None when the model writes it; or
        None. Once on a day `plan` gives an hour, from that hour on.

        Not right after it spoke, and not after a blurt nobody answered: then it waits for the chat.
        """
        moment = datetime.datetime.fromtimestamp(now)
        hour, needs = plan(moment.date()), self.history.needs(chat, now)
        if self.read_only or hour is None or moment.hour < hour or not needs:
            return None
        bored, silent, topic = needs
        last = self.history.stamp(f'urge {chat}')
        if bored < self.bored or now - silent * 3600 < last or datetime.date.fromtimestamp(last) == moment.date():
            return None
        dice = random.Random(f'{chat} {moment.date()}')
        if moment.hour == HOURS[0] and silent >= 4:
            return 'dawn', DAWN, 0
        news = self.history.news(moment.date(), self.interest(chat))
        if news and news[0] and dice.random() < self.shares:
            return 'share', None, 0
        left, top, _ = self.morale(chat, now)
        if left < top:
            return 'sad', dice.choice(SAD), 0
        return ('quiet', dice.choice(QUIET), 0) if silent >= 1 else ('butt_in', None, topic)

    def idle(self, now=None):
        """Between polls: at dawn the bot reads the news, a bored one blurts something into its groups on its own."""
        now = now or time.time()
        self.read(now)
        for chat in sorted(self.allowed_groups):
            urge = self.urge(chat, now)
            if not urge:
                continue
            kind, text, topic = urge
            LOG.info('urge chat=%s topic=%s kind=%s', chat, topic, kind)
            markup = link = feelings = None
            previous = self.history.stamp(f'urge {chat}')
            self.history.stamp(f'urge {chat}', now)  # before the model: a failing one is not asked again every poll
            with self.counting() as usage:
                try:
                    if text is None:
                        feelings = self.feelings(chat, now)
                    if kind == 'butt_in':
                        names = {**self.history.names(chat), self.me['username'].lower(): 'Рофл'}
                        lines = rename(self.history.context((chat, topic), me_id=self.me['id'], recent=self.recent,
                                                            window=self.window, limit=self.limit), names)
                        prompt = ('Переписка чата, старые сообщения сверху:\n\n' + '\n\n'.join(lines) + '\n\n'
                                  + (feelings + '\n\n' if feelings else '') + BUTT_IN)
                        text = casual(re.sub(r'^Рофл(?: → [^:\n]*)?:\s*', '', self.generate(
                            [{'role': 'user', 'content': prompt}], chat_id=chat, topic=topic)))
                    elif kind == 'share':
                        text, markup, link = self.repost(chat, now, feelings)
                except RuntimeError as exc:
                    LOG.error('urge.failed chat=%s kind=%s %s', chat, kind, exc)
                    continue
            try:
                sent = self.send((chat, topic), None, markup or text, parse_mode='HTML' if markup else None)
            except RuntimeError as exc:
                # Again on the next poll: a sleeping Mac wakes for seconds, and the network comes up after it.
                self.history.stamp(f'urge {chat}', previous)
                LOG.error('urge.send_failed chat=%s %s', chat, exc)
                continue
            self.history.add((chat, topic), sent['message_id'], 'assistant', text, now)
            self.history.record((chat, topic), sent['message_id'], None, now, text,
                                {**self.params(), 'urge': True, **({'news': link} if link else {})},
                                {'urge': kind, 'feelings': feelings, **spent(usage)})

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
                answer = self.generate([{'role': 'user', 'content': READ.format(who=who, news=listed)}]) if found else ''
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

    def repost(self, chat, now, feelings):
        """The news the bot read this morning as a post: the headline, a piece of its code if it goes with the
        interest, a joke. As (text, the same in HTML, link)."""
        day, interest = datetime.date.fromtimestamp(now), self.interest(chat)
        title, link, _, _ = self.history.news(day, interest)
        _, who, with_code = INTERESTS[interest]
        code = with_code and snippet(random.Random(f'snippet {day}').choice(SNIPPETS))
        joke = self.generate([{'role': 'user', 'content': REPOST.format(
            who=who, title=title, code=f'Кусок твоих исходников:\n{code}\n\n' if code else '',
            feelings=f'{feelings}\n\n' if feelings else '', through=' через этот кусок своего кода' if code else '')}],
            chat_id=chat)
        markup = (f'<a href="{html.escape(link)}">{html.escape(title)}</a>\n\n'
                  + (f'<pre><code class="language-python">{html.escape(code)}</code></pre>\n\n' if code else '')
                  + html.escape(joke))
        return '\n\n'.join(filter(None, (f'{title}\n{link}', code, joke))), markup, link

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
        """Someone put a reaction on the bot's answer: now and then an angry one gets a comeback, a kind one «ладно, братва»."""
        chat_id, message_id = reaction['chat']['id'], reaction['message_id']
        person = reaction.get('user') or reaction.get('actor_chat')
        added = [r['emoji'] for r in reaction.get('new_reaction', [])
                 if r.get('type') == 'emoji' and r not in reaction.get('old_reaction', [])]
        if chat_id in self.allowed_groups and reaction.get('user') and not reaction['user'].get('is_bot'):
            emoji = [r.get('emoji') or r['type'] for r in reaction.get('new_reaction', [])]
            answer = self.history.reacted(chat_id, message_id, reaction['user']['id'], emoji,
                                          reaction.get('date', time.time()))
            LOG.info('telegram.reaction chat=%s message=%s user=%s emoji=%s answer=%s',
                     chat_id, message_id, reaction['user']['id'], ''.join(emoji) or '-', answer)
        if (self.read_only or chat_id not in self.allowed_groups or not person or not added
                or time.time() - reaction.get('date', time.time()) > self.max_age
                or random.random() >= self.touchy * temper(datetime.date.today())):
            return
        answer = self.history.answer(chat_id, message_id)
        if not answer:
            return
        topic, content = answer
        angry = next((emoji for emoji in added if emoji in ANGRY), None)
        try:
            text = self.generate([{'role': 'user', 'content': (
                f'Твоё сообщение в чате:\n{content}\n\n{author_label(person)} поставил на него {angry}. '
                'Наедь на него в ответ, одна-две строки.')}], chat_id=chat_id, topic=topic) if angry else SHRUG
        except RuntimeError as exc:
            LOG.error('generation.failed %s', exc)
            return
        sent = self.send((chat_id, topic), message_id, text)
        self.history.add((chat_id, topic), sent['message_id'], 'assistant', text, sent.get('date', time.time()), message_id)

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
        offset = self.history.offset()
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
                self.history.checkpoint(offset)
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
        if '--day' in sys.argv:
            # python3 bot.py --day [2026-10-20] [дней]: the bot's character day by day. Needs no Telegram.
            args = sys.argv[sys.argv.index('--day') + 1:][:2]
            start = datetime.date.fromisoformat(args[0]) if args and '-' in args[0] else datetime.date.today()
            print(days(start, int(args[-1]) if args and '-' not in args[-1] else 7))
            sys.exit()
        read_only = '--read-only' in sys.argv or os.environ.get('READ_ONLY', '').lower() in ('1', 'true', 'yes')
        bot = Bot(read_only=read_only)
        if '--replay' in sys.argv:
            # python3 bot.py --replay 339669 [5]: the request's id from the log, how many times to answer it.
            bot.replay(*map(int, sys.argv[sys.argv.index('--replay') + 1:][:2]))
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
