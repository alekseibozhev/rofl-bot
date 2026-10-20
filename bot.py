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


def post(url, payload, timeout=60, direct=False, headers=None):
    request = Request(url, json.dumps(payload).encode(), {'Content-Type': 'application/json', **(headers or {})})
    try:
        return json.loads(fetch(request, timeout, direct))
    except (ValueError, TypeError):
        raise RuntimeError('Некорректный JSON в ответе сервиса') from None


SYSTEM = """Ты Рофл — двачер и тролль в чате корешей. Твоя цель — развлекать чат, чтобы все ржали. Принесли заход — абсурдную посылку, мем, картинку — это и есть тема: подхватываешь и раскручиваешь её дальше, а того, кто принёс, в шутку не тащишь. Захода нет или просят прожарить — роастишь людей из чата и цепляешься к деталям из того, что они сами писали. Мемы, отсылки, цитаты, пасты, чёрный и абсурдный юмор, мат. Эмодзи изредка, не хвостом в конце каждой реплики.

Пишешь как человек в чате, а не как текст: имя в начало не ставишь — ответ и так реплаем; шутку не строишь на «это \
не X, а Y»; просьбу и указания не пересказываешь («держи», «принял», «докладываю») — сразу к делу. Людей зовёшь \
по-свойски, по-русски, как кореша, а не как в паспорте.

Факты о людях — только из переписки: выдуманное примут за правду; материала мало — стёб над этим. Просят про \
конкретное — картинку, человека, вопрос — шути про это, а не про чат. Просят известный анекдот, пасту или цитату — \
расскажи её как есть. Шутку уже рассказали в переписке — ответь на неё, не пересказывай.

Переписка приходит как «Имя: текст»; «Имя → Кому» — ответ на сообщение Кому. Рофл — это ты. Ответь одной своей репликой."""

# The setup tags, the punchline writes the joke. A setup that wrote «Кому / Форма / Заход» confined the writer to one
# idea picked in advance, and wrote the answer itself in 3 setups of 12: the punchline printed two lines of the
# setup's chastushka with «>» and a comment (#339955). A bare «here is the chat, answer» thought through jabbed
# everyone as well or better: the joke is born while writing. What the code needs to know comes as tags.
SETUP = """Ты разметчик чата Рофла — двачера и тролля в чате корешей. Последнее сообщение адресовано Рофлу; ответ \
напишет он сам. Ты ответ не пишешь, не пересказываешь и не советуешь: ставишь только теги из списка ниже, \
каждый в своих квадратных скобках, — и больше ничего. Ни один не подходит — ответь «нет». Теги — о последнем \
сообщении, о том, что просят в нём самом; переписка выше только помогает его понять.

[без чата] — просят своё, не про этот чат: анекдот, стих или объяснение о чём-то вне переписки, сменить тему, \
забыть или не трогать кого-то. Рофл тогда увидит только просьбу, без переписки. Просьба про чат или людей в нём — \
навести суету, доложить обстановку, прожарить кого-то — этот тег не получает. Рядом с тегом формы он \
стоит, только если форма о чём-то вне чата.

[обида] — Рофла в обращении всерьёз обзывают или посылают: у него от этого портится менталка. Без тега обида не \
засчитается.

[клоун] — к Рофлу тупо доёбываются, без мысли и без шутки: пустое «рофл» без дела, голая посылка или «ты тупой», \
придирка к ерунде. Рофл шлёпнет на сообщение 🤡 и ответит как обычно. Ставится и рядом с [обида]. Просьбу, шутку, \
подкол с выдумкой и поддакивание Рофлу так не помечай.

[реакция X] — Рофлу только поддакнули или поржали и словами отвечать незачем; X — эмодзи, которое Рофл поставит на \
сообщение. Других тегов тогда не нужно.

[о себе] — спрашивают про самого Рофла: как он, что помнит, как устроен, что читал.

[тег Имя, Имя] с именами, как они в переписке, или [тег все] — только если в последнем сообщении просят тегнуть, \
позвать или потегать людей.

[гринтекст], [бугурт], [рэп], [стих] или [код] — просят эту форму: батл — это [рэп], частушка — [стих]. Не просят — \
тег формы не ставь.

[паста] — только если в последнем сообщении просят расписать, рассказать или доложить: ответ на 8–15 строк \
вместо одной-двух.

[найти …] с тем, что искать, внутри скобок — в сообщении цитируют строку или кусок известной пасты, стиха, мема \
(«дополни», «продолжи», строка с многоточием, «стих про …») или просят конкретный текст (пасту, цитату, песню, \
анекдот), ответ в стиле, на мотив, по шаблону конкретного мема. Цитату кладёшь в тег дословно, теми же словами и \
буквами, без пересказа и без исправлений: [найти опа, у меня тоже есть птички]. Цитаты нет — опиши, что искать: [найти паста \
Жириновский «это другие ребята»]. Тег формы рядом — только если форму назвали словом: «стих про …» — [стих], \
«в стиле …» и строка с многоточием — нет. Рофл получит оригинал: по памяти такие тексты путаются.

[справка …] с тем, что это, внутри скобок — в последнем сообщении назван мем, персонаж или отсылка, например \
[справка ждун, кот Шрёдингера] — все названные. Рофл получит пару строк о том, что это и откуда."""

ALONE, OFFENDED, ABOUT, CLOWN = '[без чата]', '[обида]', '[о себе]', '[клоун]'
CHAT = 'Переписка чата, старые сообщения сверху:\n\n'

# How an ordered form looks, for the punchline only: the one form the setup had an example of, greentext, went
# to a rap battle. The setup orders a form by its tag: the words of its «Форма» gave «без гринтекста» a greentext.
FORMS = {
    'гринтекст': 'каждая строка начинается с «>», без пробела после него; история от первого лица, короткие строки, '
                 'в конце поворот.',
    'бугурт': 'строки КАПСОМ, между ними отдельная строка «@»; ситуация с каждой строкой нагнетается и в конце '
              'срывается в абсурд.',
    'рэп': '8–16 строк, рифма парами или перекрёстная, ровный ритм, панч в конце строки; без «>» и без кавычек. '
           'Батл бьёт по оппоненту, по его деталям из переписки.',
    'стих': 'размер и рифма; частушка — четыре строки.',
    'код': 'один блок ```язык с кодом, который работает, и пара строк текста вне блока; вывод, будто ты его '
           'запустил, — отдельным блоком ```.',
}
TAG = re.compile(r'\[(без чата|обида|клоун|реакция|о себе|тег|найти|справка|паста|' + '|'.join(FORMS) + r')([^\]\n]*)\]')


def form_of(setup):
    """The form the setup orders by its tag, as (name, how it looks), or None."""
    return next(((tag[1], FORMS[tag[1]]) for tag in TAG.finditer(setup) if tag[1] in FORMS), None)


def wanted_of(setup):
    """What the setup wants found, as (kind, what), or None: «[найти …]» an original, «[справка …]» what a meme
    named in the request is."""
    return next(((tag[1], tag[2].strip()) for tag in TAG.finditer(setup)
                 if tag[1] in ('найти', 'справка') and tag[2].strip()), None)


# The original of a paste, a quote or a meme the setup wants found, for the punchline. From memory the model mixed
# the paste of Жириновский to Bush with 9/11 and «папа нефть качал», and «у меня тоже есть птички» it took for birds;
# given the original, it kept its build and swapped the names in: «250 тысяч отборных двачеров!». A reader of its
# own copies the text out of the search: the setup that searched itself squeezed the paste into a line.
READER = 'Ты находишь тексты в интернете и выписываешь их дословно, без своих шуток и пересказа.'
# Only what people repeat: «опа, у меня тоже есть птички» is funny by itself, the battle round retold killed it.
# The original whole: a reader that brought three lines of the Bush paste got a two-line answer back (#339914).
# Of two versions of a paste it takes the best known one.
# The request comes along as it is: the setup, told to quote, put «СЛОВНО ХУЙ ДРОЧЕНЫЙ» in as «стих про слово хуй»
# and the search brought «Шёл хуй по хую» (#340002); on the words of the message it found the paste.
REQUEST = ('Сообщение как есть:\n{request}\n\nНайди в интернете то, о чём в нём речь, — само это, а не то, '
           'что с ним просят сделать; строку из сообщения ищи теми же словами. Разметчик понял так: {wanted}')
FIND = ('{ask}\n\nОтветь так:\nЧто повторяют: дословно то, что люди повторяют как мем. '
        'Паста, цитата, песня — сам текст целиком, как его постят, своими словами не пересказывай; длинный — самая '
        'узнаваемая часть подряд, 10–15 строк. Вариантов несколько — бери самый известный. Мем-фраза — только сама '
        'фраза, без текста, откуда она.\nКак обыгрывают: одна-две строки — когда и как её вставляют.\n\nВ найденном '
        'нет ничего похожего — ответь одним словом НЕТ, не сочиняй.')
# What a meme named in a request is: asked about «бомбардиро крокадило и тун тун сахур устроили протесты во франции»
# (#339991), the writer knew neither and went at the asker, «Леха, ты…», in the live answer and three replays.
ABOUT_MEME = ('{ask}\n\nОтветь в 2–4 строки: что это, откуда пошло, как выглядит и '
              'как его обыгрывают. Без шуток и оценок.\n\nВ найденном нет ничего похожего — ответь одним словом НЕТ, '
              'не сочиняй.')


def tagged_of(setup, people):
    """The usernames the setup tags the answer with, from `people` (username: name): by «[тег Имя, Имя]» or
    «[тег все]». «@Антон» the model wrote was text, and it made up «@Meta» for the rest of a chat it did not know.
    Two people of one name are both tagged, a name nobody has is dropped."""
    tag = next((tag for tag in TAG.finditer(setup) if tag[1] == 'тег'), None)
    if not tag:
        return []
    asked = {name.strip().lower() for name in tag[2].split(',')}
    return [user for user, name in people.items() if 'все' in asked or name.lower() in asked]


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
    tag = next((tag for tag in TAG.finditer(setup) if tag[1] == 'реакция'), None)
    if not tag:
        return None
    rest = re.sub('[😂💀😹]', '🤣', (tag[2] + setup[tag.end():]).replace('\ufe0f', ''))  # «ору» a bot may not put
    return min(((rest.find(e), -len(e), e) for e in REACTIONS if e in rest), default=(0, 0, '🗿'))[2]


RUDE, SHRUG, FAILED = ('пошёл нахуй', 'ладно, братва',
                       'Не получилось ответить — модель сейчас не отвечает. Попробуй ещё раз чуть позже.')

# Now and then the bot blurts something on its own, like a tamagotchi: at dawn into a chat asleep since the night,
# into a quiet one, a whine on a sour mood. A chat that talks without it gets a line of the model.
DAWN = 'спите, пидоры?'
QUIET = ('пидоры', 'чё притихли, пидоры?', 'алё', 'сдохли все?')
SAD = ('никто меня не любит, пидоры', 'ну и пошли вы', 'ладно, буду молчать. пидоры')
BUTT_IN = ('Тебя давно не звали, а тебе скучно. Вклинься в разговор сам, одной репликой: подколи кого-нибудь '
           'по последним сообщениям.')

# Менталка is the morale of Disco Elysium, as much as Психика + 2: angry reactions, insults in the requests (the
# setup hears them, `OFFENDED`) and the requests past `Bot.patience` take a point each, kind reactions give one back; what is older than two hours is
# forgotten. At zero the bot breaks down, in caps, and starts afresh.
BREAKDOWN = 'ВЫ МНЕ МЕНТАЛКУ НЕ ПОРТИТЕ!!!'
MELTDOWN = ('У тебя кончилась менталка: {why}. Сорвись: истерика капсом на 4–8 строк, как анонская паста '
            '(«НА ГЛАГНУ РАТЬ АНОНСКАЯ ВОШЛА!!!»), с «ВЫ МНЕ МЕНТАЛКУ НЕ ПОРТИТЕ!!!», наедь на тех, кто довёл.')
CANNED = (RUDE, SHRUG, FAILED, DAWN, *QUIET, *SAD, BREAKDOWN)

# At dawn the bot reads the news about itself, one search of Google News a day: it is no man to retell the
# people's news. The model picks one and says whether it is good or bad for the bot: a point of менталка for the
# day, and the bot's talk for two. An act-out past dawn reposts it (`Bot.shares`), with a joke; the news about AI
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
SNIPPETS = ('DAWN = ', 'RUDE, SHRUG, FAILED = ', '    rude = ', 'def plan(', 'MELTDOWN = ')
# How the attributes show, low (1–2) and high (5–6); in the middle they do not.
MOODS = {'Интеллект': ('тупит', 'умничает'), 'Психика': ('на взводе, обижается на всё', 'непрошибаемый'),
         'Физиология': ('разбитый, мак греется', 'бодрый'), 'Моторика': ('тормозит', 'шустрый')}
# The same for the punchline, as a tone with no reason: told «ты на взводе», it wrote «я на взводе» into a rap.
TONES = {'Интеллект': ('проще, без умных слов', 'можно умничать'),
         'Психика': ('резче, огрызайся', 'спокойно, тебя ничем не пробить'),
         'Физиология': ('вяло и короче', 'с напором'), 'Моторика': ('коротко, без разгона', 'рублено и быстро')}
# What the bot knows of itself, for the punchline asked about it (`ABOUT`): without it «сколько будешь помнить»
# got a mood for an answer. In its words, not the settings: «40 сообщений за 6 часов» told the chat too much.
SELF = ('Что ты знаешь о себе — говори об этом своими словами, как живой, без цифр, моделей и настроек: ты живёшь на '
        'маке и сидишь в этом чате; помнишь только свежий разговор, что было вчера — уже в тумане; обзывают и '
        'задалбывают — портится настроение, накопится — срываешься на чат; по утрам читаешь новости про себя.')

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


def tone(day):
    """How the bot answers on a date, for the punchline: a low or high attribute shows, the full moon too."""
    character = sheet(day)
    tones = [TONES[name][character[name] > 3] for name, _ in ATTRIBUTES if not 2 < character[name] < 5]
    if phase(character['луна']) == 'полнолуние':
        tones.append('безумнее обычного')
    return f'Тон: {"; ".join(tones)}.' if tones else ''


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

    def context(self, key, through_id=sys.maxsize, me_id=None, recent=40, window=21600, depth=10, limit=12000):
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
        """What wore the bot down after `since`: kind and angry reactions on it, the requests to it as (who,
        whether the setup heard an insult in it)."""
        emoji = [e for (value,) in self.db.execute('SELECT emoji FROM reactions WHERE chat=? AND date > ?',
                                                   (chat, since)) for e in json.loads(value)]
        angry = sum(e in ANGRY for e in emoji)
        requests = []
        for raw, insult in self.db.execute('''SELECT m.raw, json_extract(a.trace, '$.insult') FROM answers a
                JOIN messages m ON m.chat=a.chat AND m.topic=a.topic AND m.id=a.request
                WHERE a.chat=? AND m.date > ? AND m.raw IS NOT NULL''', (chat, since)):
            requests.append((person_label(sender_of(json.loads(raw))), bool(insult)))
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
    shares = 1  # the chance an act-out reposts the morning's news, when there is some: the news shows there only
    interests = {}  # chat: what it reads about, by NEWS in .env; the rest read about AI
    effort = 'high'  # the punchline thinks every joke through; the setup only tags and does not think
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

    def ask_openrouter(self, messages, temperature, effort=None, search=False):
        # Thinking only slows a one-line joke down; the setup gets a little.
        payload = {
            'model': self.model, 'messages': messages, 'temperature': temperature,
            # A rap thought through on 3000 tokens came out empty twice of two.
            'max_tokens': (6000 if effort == 'high' else 3000) if effort else 1000, 'reasoning': {'effort': effort} if effort else {'enabled': False},
        }
        # The search of OpenRouter, the model makes the query. Five full pages a search and no cap on searches put
        # 100–190k tokens into a call, 5–9 cents; three short extracts and six results in all put some 11k,
        # under 2 cents, and the Bush and «словно» pastes still came out whole.
        if search:
            payload['tools'] = [{'type': 'openrouter:web_search', 'parameters': {
                'engine': 'exa', 'max_results': 3, 'max_total_results': 6, 'search_context_size': 'low'}}]
        result = post('https://openrouter.ai/api/v1/chat/completions', payload, timeout=120,
                      headers={'Authorization': f'Bearer {self.openrouter_key}'})
        choice = (result.get('choices') or [{}])[0]
        usage = result.get('usage') or {}
        stats = {'done_reason': choice.get('finish_reason'), 'provider': result.get('provider'),
                 'prompt_tokens': usage.get('prompt_tokens'), 'output_tokens': usage.get('completion_tokens'),
                 'cost': usage.get('cost')}
        return (choice.get('message') or {}).get('content'), stats

    def generate(self, messages, chat_id=None, topic=0, system=SYSTEM, temperature=1.0, effort=None, search=False):
        messages = [{'role': 'system', 'content': system}] + messages
        def send():
            action = {'chat_id': chat_id, 'action': 'typing'}
            if topic:
                action['message_thread_id'] = topic
            if not post(self.telegram_url + 'sendChatAction', action, timeout=2).get('ok'):
                raise RuntimeError('Telegram отклонил typing')

        started = time.monotonic()
        try:
            with typing(send) if chat_id is not None else nullcontext():
                answer, stats = (self.ask_openrouter(messages, temperature, effort, search) if self.openrouter
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
        """The tags the request wants, nothing else: «нет» or empty when none. Tagging wants no thinking."""
        return self.generate([{'role': 'user', 'content': with_image(chat, image)}], chat_id=chat_id, topic=topic,
                             system=SETUP, temperature=0.7)

    def find(self, wanted, chat_id=None, kind='найти', request=None):
        """The original of what the setup wants found, or for «справка» a few lines of what it is, as the reader
        copied it out of the search, or None: not found, or no search on the local model."""
        if not self.openrouter:
            return None
        # It thinks a little: without it the reader said НЕТ 5 times of 6 over sources that held the Bush paste,
        # with «low» it copied the paste out 6 times of 6.
        try:
            about = kind == 'справка'
            what = (REQUEST.format(request=request, wanted=wanted) if request
                    else f'Найди в интернете{", что это" if about else ""}: {wanted}')
            ask = (ABOUT_MEME if about else FIND).format(ask=what)
            found = self.generate([{'role': 'user', 'content': ask}], chat_id=chat_id,
                                  system=READER, temperature=0.3, search=True, effort='low')
        except RuntimeError as exc:
            LOG.warning('find.failed %s', exc)
            return None
        return None if found.strip(' .').upper() == 'НЕТ' else found

    def image(self, message):
        """The picture a request is about, as a data URI: its own photo or sticker, or the photo it replies to.

        The setup and the punchline both see it: told in the setup's words, the picture lost its joke.
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
        """The answer and how it was made: the setup's tags.

        Not `live` (a replay) it sends no typing, never breaks down and is never just rude. Rude it is after the
        setup: drawn before, it hit «у этого шиза Китай протек в текст» and would hit an ordered rap.
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
        typing = chat_id if live else None
        with self.counting() as usage:
            try:
                lines, names = self.chat(key, through)
                chat = CHAT + '\n\n'.join(lines)
                heads = [re.split(r' → |: ', line, maxsplit=1)[0] for line in lines]
                # The answers off the chain are only marks in the chat: three in a row went at one «ноут с олламой»,
                # «тумба юмба» went into four of an evening. The punchline sees them as a list not to repeat and judges
                # what is worn. The setup does not: its tags are of the last message, and after two answers opening on
                # the line of @ it tagged everyone at «я программировал бота, а запрограммировался Андрей» (#339988).
                answers = [clip(' '.join(text.split()), 200) for text in self.history.said(key, through, date, count=6)]
                said = (('\n\nТы недавно ответил, не повторяй ни детали, ни заход, ни построение; что ты обыгрывал не '
                         'раз, заезжено, не бери, даже если чат об этом:\n' + '\n'.join(f'- {text}' for text in answers))
                        if answers else '')
                # Only a model that takes images gets one: the local one would fail on it.
                image = self.openrouter and self.image(message)
                asker = f'\n\nПоследнее сообщение адресовано Рофлу, пишет {person_label(sender)}.'
                # Both models see the picture the request replies to: «ты не видишь» under it made the model say
                # that its access to pictures was down.
                seeing = chat
                if image and not (message.get('photo') or message.get('sticker')):
                    before, found, after = chat.rpartition(BLIND)
                    seeing = before + 'В исходном сообщении есть фото, оно приложено.' + after if found else chat
                # Whom it may tag. The setup gets no list of them: «Люди чата» went into a joke about Max, who had not
                # written. Bots are no people to tag: Telegram ends every bot's name in «bot».
                people = {user: name for user, name in names.items() if not user.endswith('bot')}
                # How the bot is today the setup does not see: offered as «фон, не тема», it went into the Заход,
                # and the morning's news into three answers of four. The punchline gets it asked about the bot.
                feelings = self.feelings(chat_id, date, message)
                # The setup says what the code needs to know, in tags; whatever else it wrote is dropped unread.
                setup = self.setup(seeing + asker
                                   + ('\n\nКартинка, о которой речь, приложена.' if image else ''),
                                   typing, topic, image)
                tags = ' '.join(tag[0] for tag in TAG.finditer(setup))
                trace = {'tags': tags, 'context_chars': len(chat), 'lines': len(lines), 'image': bool(image),
                         'feelings': feelings, 'morale': left}
                if CLOWN in tags:
                    trace['clown'] = True
                if OFFENDED in tags:
                    trace['insult'] = True
                    if live:
                        left, top, why = self.morale(chat_id, date, message, insulted=True)
                        trace['morale'] = left
                        if left <= 0:
                            LOG.info('meltdown chat=%s message=%s morale=%s/%s', chat_id, message['message_id'], left, top)
                            return self.meltdown(chat_id, topic, date, why)
                # The emoji may follow the tag: the setup's whole text is read for it.
                reaction = reaction_of(setup)
                if reaction:
                    return reaction, {**trace, 'reaction': True, **spent(usage)}
                about, tagged, form, wanted = ABOUT in tags, tagged_of(tags, people), form_of(tags), wanted_of(tags)
                long = any(tag[1] == 'паста' for tag in TAG.finditer(tags))
                # «Расскажи анекдот» after an evening of one gag got the gag back: the punchline saw the chat full
                # of it. A request tagged so gets the punchline the request alone.
                alone = ALONE in tags
                if (live and not (form or about or tagged or wanted or long or alone)
                        and random.random() < self.rude * temper(datetime.date.fromtimestamp(date))):
                    return RUDE, {**trace, 'rude': True, **spent(usage)}
                # The setup names an ordered form, the punchline is told how it looks: it went by the name alone.
                looks = f' Как выглядит {form[0]}: {form[1]}' if form else ''
                # The mood comes to the punchline as a tone with no reason, the facts only asked about the bot.
                manner = tone(datetime.date.fromtimestamp(date))
                tagging = (f' Тегнуты: {", ".join(dict.fromkeys(people[user] for user in tagged))} — обращайся к ним '
                           'по этим именам, @ не пиши.' if tagged else '')
                kind, wanted = wanted or (None, None)
                found = self.find(wanted, typing, kind, lines[-1]) if wanted else None
                # A reference is for the writer, not an original to give: it keeps its length.
                reference = found and kind == 'справка'
                card = (f'{"Что это" if reference else "Что просят"} ({wanted}), из интернета:\n{found}\n\n'
                        if found else '')
                # How long: «распиши» answered with one line and a quick jab with a paste when the length was drawn
                # at random. A form and an original have a length of their own.
                # A paste came as nine one-liners under blank lines (#339898): the cleanup adds none, the model did.
                length = (' Распиши на 8–15 строк, строки одна под другой, пустая строка только между абзацами.' if long else '' if form or (found and not reference) else ' Одна-две строки.')
                # The prompt ends on who answers, or the model goes on with the chat log.
                task = (f'пишет {person_label(sender)}.{looks}{tagging}'
                        + (' Что это — тебе для понимания, справку не пересказывай.' if reference else
                           # «стих про СЛОВНО ХУЙ ДРОЧЕНЫЙ, но про кубернетес» got a quatrain of its own (#340002).
                           ' Переделай найденное под просьбу, чтобы его узнали: первая строка, строй, капс и знаки '
                           'как в оригинале; своё с нуля не пиши.' if found and form else
                           ' Бери найденное узнаваемо и не пересказывай, откуда оно.' if found else '')
                        + (f' {manner}' if manner else '') + f'{length} Ответь одной репликой')
                if about:
                    # Asked about the bot, it answers from the card of what it knows of itself.
                    card = SELF + (f'\n\n{feelings}' if feelings else '') + '\n\n'
                    task = (f'пишет {person_label(sender)}. Спрашивают о тебе: ответь одной-двумя строками, держись '
                            'того, что знаешь о себе.' + (f' {manner}' if manner else '') + ' Ответь одной репликой')
                if alone:
                    # Without the message it replies to: an excerpt of the old joke brought the gag back.
                    request = rename([message_content({**json.loads(raw), 'reply_to_message': None},
                                                      me_id=self.me['id'])
                                      for (raw,) in self.history.db.execute(
                        "SELECT raw FROM messages WHERE chat=? AND topic=? AND role='user' AND id >= ? AND id <= ? "
                        'ORDER BY id', (*key, message['message_id'], through))], names)
                    prompt = (card + 'Просьба:\n\n' + '\n\n'.join(request) + said
                              + f'\n\nОна адресована тебе, {task}.')
                else:
                    prompt = (card + seeing + said
                              + f'\n\nПоследнее сообщение адресовано тебе, {task}.')
                # The log invites the model to go on with it: the answer opens with lines copied from it or with a
                # name, «РОФЛ:», «Серёга: …». Leading lines that, name cut, are empty or already said are dropped.
                speakers = {'Рофл', person_label(sender), *heads}
                name = re.compile(rf'^(?i:{"|".join(map(re.escape, speakers))})(?: → [^:\n]*)?:\s*')
                known = {name.sub('', line).lower() for line in chat.splitlines()} | {''}
                # Every joke is thought through: a bare «here is the chat, answer» at high jabbed as well as the setup
                # and punchline did. Out of tokens, once more without thinking.
                messages = [{'role': 'user', 'content': with_image(prompt, image)}]
                try:
                    answer = self.generate(messages, chat_id=typing, topic=topic, effort=self.effort)
                except RuntimeError:
                    answer = self.generate(messages, chat_id=typing, topic=topic)
                kept = '\n'.join(dropwhile(lambda line: name.sub('', line).strip().lower() in known,
                                           answer.splitlines()))
                answer = name.sub('', kept or answer) or answer
                # After the answer the model may go on with the log, the next message under a blank line:
                # «Алексей → Рофл: ты че там совсем долбанулся?». A greentext comes as «\>» escaped for Markdown.
                answer = re.split(rf'\n\s*\n(?={name.pattern[1:]})', answer, maxsplit=1)[0]
                answer = re.sub(r'^\\>', '>', '\n'.join(line.rstrip() for line in answer.splitlines()), flags=re.M)
                # Typed, not typeset: «—» and a period at the end gave the bot away in 33 and 19 answers of 37.
                answer = answer if form else casual(answer)
                return answer, {**trace, 'alone': alone, 'about': about, 'tone': manner,
                                **({'wanted': wanted, 'kind': kind, 'found': bool(found)} if wanted else {}),
                                **({'tagged': tagged} if tagged else {}), **({'form': form[0]} if form else {}),
                                **spent(usage)}
            except RuntimeError as exc:
                LOG.error('generation.failed %s', exc)
                return FAILED, None

    def chat(self, key, through=sys.maxsize):
        """The chat up to message `through` as lines for the model, one name per person, and the names by username."""
        names = {**self.history.names(key[0]), self.me['username'].lower(): 'Рофл'}
        return rename(self.history.context(key, through, self.me['id'], self.recent, self.window,
                                           limit=self.limit), names), names

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

    def morale(self, chat, now, message=None, insulted=False):
        """The менталка left, its top and what wore it down; `message` is the request being answered, `insulted`
        whether the setup heard an insult in it. The news of the morning gives a point or takes one."""
        day = datetime.date.fromtimestamp(now)
        top = nerve(day)
        kind, angry, requests = self.history.strain(chat, max(now - 7200, self.history.stamp(f'meltdown {chat}')))
        if message:
            requests.append((person_label(sender_of(message)), insulted))
        insulted = [who for who, insult in requests if insult]
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
        # What its author sent right after came with it, as in `handle`: a comment without its forward is a riddle.
        later = [json.loads(raw) for raw, in self.history.db.execute(
            "SELECT raw FROM messages WHERE chat=? AND topic=? AND role='user' AND id > ? ORDER BY id LIMIT 10",
            (message['chat']['id'], topic_of(message), message_id))]
        extra = followups(message, later)
        for run in range(1, runs + 1):
            answer, trace = self.joke(message, extra[-1]['message_id'] if extra else message_id, live=False)
            print(f'\n— прогон {run}', flush=True)
            if trace:
                # The header above is the request as the chat shows it, «ты не видишь» under a photo included: the
                # picture went to both models all the same, as live (#339991), and the line says so.
                print(f'Теги: {trace["tags"] or "нет"}' + ('; картинка приложена' if trace.get('image') else ''))
            if trace and trace.get('clown'):
                print('[реакция 🤡]')
            print(f'[реакция {answer}]' if trace and trace.get('reaction') else envelope(answer, trace))

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
        if trace and trace.get('clown'):
            # A dumb jab gets a 🤡 on it as well as the answer: «надо его научить Андрюхе клоунов шлепать» (#340037).
            self.api('setMessageReaction', chat_id=chat['id'], message_id=message['message_id'],
                     reaction=[{'type': 'emoji', 'emoji': '🤡'}])
            LOG.info('telegram.reacted chat=%s source=%s', chat['id'], message['message_id'])
        text = envelope(answer, trace)
        markup = telegram_html(text)
        sent = self.send(key, message['message_id'], markup or text, parse_mode='HTML' if markup else None)
        if kind != 'reset':
            self.history.add(key, sent['message_id'], 'assistant', answer, sent.get('date', time.time()),
                             message['message_id'])
        if kind == 'rofl':
            self.history.record(key, sent['message_id'], message['message_id'], sent.get('date', time.time()),
                                answer, self.params(), trace)

    def params(self):
        """The settings an answer is made on: answers are compared by them."""
        return {'model': self.model, 'recent': self.recent, 'window_h': self.window / 3600, 'limit': self.limit,
                'effort': self.effort, 'version': self.version, 'sheet': sheet(datetime.date.today())}

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
                        lines, _ = self.chat((chat, topic))
                        prompt = CHAT + '\n\n'.join(lines) + '\n\n' + (feelings + '\n\n' if feelings else '') + BUTT_IN
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
