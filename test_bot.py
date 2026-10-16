import datetime
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import bot
from bot import Bot, History, followups


class ConversationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'history.sqlite3'
        self.b = Bot.__new__(Bot)
        self.b.allowed_groups = {-100123}
        self.b.me = {'id': 42, 'username': 'test_bot'}
        self.b.history = History(self.path)
        self.b.generate = Mock(return_value='Согласен')
        self.b.rude = 0
        self.b.setup = Mock(return_value='Кому: User 7.\nФорма: одна строка.')
        self.counter = 0
        self.b.api = Mock(side_effect=self.send)
        self.b.read = Mock()  # no news from the network unless a test asks

    def tearDown(self):
        self.b.history.db.close()
        self.tmp.cleanup()

    def send(self, method, **kwargs):
        self.counter += 1
        return {'message_id': self.counter, 'date': int(time.time())}

    def prompt(self):
        [message] = self.b.generate.call_args.args[0]
        return message['content']

    def store(self, message_id, text, topic=0, date=None, name='User 9'):
        self.b.history.add_message((-100123, topic), {'message_id': message_id, 'date': date or time.time(),
                                                      'from': {'id': 9, 'first_name': name}, 'text': text})

    def message(self, text, user=7, **fields):
        self.counter += 1
        return dict({'chat': {'id': -100123, 'type': 'supergroup'},
                     'message_id': self.counter, 'date': int(time.time()),
                     'from': {'id': user, 'first_name': f'User {user}'}, 'text': text}, **fields)

    def test_background_without_automatic_followup(self):
        self.b.handle(self.message('Встречаемся в пятницу у Антона', user=8))
        self.b.generate.assert_not_called()
        self.b.handle(self.message('рофл, когда встречаемся?'))
        context = self.b.generate.call_args.args[0]
        self.assertIn('пятницу', context[0]['content'])
        self.b.handle(self.message('а у кого?'))
        self.assertEqual(self.b.generate.call_count, 1)
        self.b.handle(self.message('Я заказал пиццу', user=8))
        self.b.handle(self.message('отлично'))
        self.assertEqual(self.b.generate.call_count, 1)

    def test_followup_expiry_and_reply_to_someone_else(self):
        self.b.handle(self.message('бот, привет'))
        self.b.handle(self.message('погода хорошая'))
        self.assertEqual(self.b.generate.call_count, 1)
        self.b.handle(self.message('бот, привет'))
        self.b.handle(self.message('да', reply_to_message={'from': {'id': 8}, 'text': 'пицца?'}))
        self.assertEqual(self.b.generate.call_count, 2)

    def test_duplicate_survives_restart(self):
        message = self.message('бот, привет')
        self.b.handle(message)
        self.b.history.db.close()
        self.b.history = History(self.path)
        self.b.handle(message)
        self.assertEqual(self.b.api.call_count, 1)
        self.assertEqual(self.b.generate.call_count, 1)

    def test_empty_answer_is_an_error_and_diagnostics_are_logged(self):
        self.b.ollama_url = 'http://localhost:11434'
        self.b.model = 'test'
        good = {'message': {'content': 'Привет!'}, 'done_reason': 'stop', 'eval_count': 20, 'eval_duration': 2e9}
        with patch('bot.post', return_value=good) as post, self.assertLogs('bot', level='INFO') as logs:
            answer = Bot.generate(self.b, [{'role': 'user', 'content': 'Привет'}])
        self.assertEqual(answer, 'Привет!')
        self.assertFalse(post.call_args.args[1]['stream'])
        self.assertIn('"tokens_per_s": 10.0', str(logs.output))
        with patch('bot.post', return_value={'message': {'content': ' '}}), self.assertLogs('bot'):
            with self.assertRaises(RuntimeError):
                Bot.generate(self.b, [])

    def test_openrouter_goes_through_the_proxy_without_thinking(self):
        self.b.openrouter, self.b.openrouter_key, self.b.model = True, 'sk-or-test', 'deepseek/test'
        good = {'choices': [{'message': {'content': 'Здарова'}, 'finish_reason': 'stop'}],
                'usage': {'prompt_tokens': 12, 'completion_tokens': 3, 'cost': 0.00001}}
        with patch('bot.post', return_value=good) as post, self.assertLogs('bot', level='INFO') as logs:
            answer = Bot.generate(self.b, [{'role': 'user', 'content': 'Привет'}])
        self.assertEqual(answer, 'Здарова')
        url, payload = post.call_args.args
        self.assertEqual(url, 'https://openrouter.ai/api/v1/chat/completions')
        self.assertEqual(payload['reasoning'], {'enabled': False})
        self.assertEqual(payload['messages'][0]['role'], 'system')
        self.assertFalse(post.call_args.kwargs.get('direct'))
        self.assertEqual(post.call_args.kwargs['headers'], {'Authorization': 'Bearer sk-or-test'})
        self.assertIn('"cost": 1e-05', str(logs.output))
        self.assertNotIn('sk-or-test', str(logs.output))

    def test_a_joke_counts_the_cost_of_its_own_calls_only(self):
        def generate(messages, **kwargs):
            if self.b.usage is not None:
                self.b.usage.append({'prompt_tokens': 100, 'cost': 0.01})
            if failing:
                raise RuntimeError('Модель вернула пустой ответ')
            return 'Согласен'
        self.b.generate = Mock(side_effect=generate)
        failing = True
        with self.assertLogs('bot', 'ERROR'):
            self.b.handle(self.message('бот, жги'))
        self.assertIsNone(self.b.usage)  # the calls after a failed joke are not its own
        failing = False
        self.b.handle(self.message('бот, ещё'))
        trace = json.loads(self.b.history.db.execute('SELECT trace FROM answers ORDER BY id DESC').fetchone()[0])
        self.assertEqual((trace['prompt_tokens'], trace['cost']), (100, 0.01))

    def test_read_only_stores_but_never_sends(self):
        self.b.read_only = True
        self.b.handle(self.message('Встречаемся в пятницу', user=8))
        self.b.handle(self.message('рофл, когда встречаемся?'))
        self.b.handle(self.message('/reset'))
        self.b.handle(self.message('/help'))
        self.b.generate.assert_not_called()
        self.b.api.assert_not_called()
        context = self.b.history.context((-100123, 0))
        self.assertEqual(len(context), 2)
        self.assertIn('пятницу', context[0])
        self.b.read_only = False
        self.b.handle(self.message('бот, ну так когда?'))
        self.assertIn('пятницу', self.prompt())

    def test_requests_sent_while_off_are_answered_for_a_day(self):
        self.b.handle(self.message('кто со мной на шашлыки?', user=8, date=int(time.time()) - 3 * 3600 - 300))
        self.b.handle(self.message('бот, ответь на старое', date=int(time.time()) - 3 * 3600))
        self.assertIn('User 8: кто со мной на шашлыки?\n\nUser 7: бот, ответь на старое', self.prompt())
        self.b.generate.reset_mock()
        self.b.handle(self.message('бот, вчерашнее', date=int(time.time()) - 2 * 86400))
        self.b.handle(self.message('/reset', date=int(time.time()) - 2 * 86400))
        self.b.generate.assert_not_called()
        self.assertEqual(self.b.api.call_count, 1)  # only the first answer

    def test_quote_and_restriction(self):
        self.b.handle(self.message('бот, поясни', quote={'text': 'нужный фрагмент'},
                      reply_to_message={'from': {'id': 8, 'first_name': 'Антон'}, 'text': 'весь исходный текст'}))
        current = self.b.generate.call_args.args[0][-1]['content']
        self.assertIn('нужный фрагмент', current)
        self.assertIn('Антон', current)
        self.assertNotIn('весь исходный текст', current)
        for kind, chat in [('private', 7), ('group', -999), ('channel', -100123)]:
            self.b.handle(self.message('бот, привет', chat={'id': chat, 'type': kind}))
        self.assertEqual(self.b.generate.call_count, 1)

    def test_setup_reads_the_chat_and_sometimes_it_is_just_rude(self):
        self.b.handle(self.message('Андрюха опять проспал', user=8))
        self.b.handle(self.message('бот, распиши'))
        setup = self.b.setup.call_args.args[0]
        self.assertIn('User 8: Андрюха опять проспал', setup)
        self.assertTrue(setup.endswith('Последнее сообщение адресовано Рофлу, пишет User 7.'))
        self.b.rude = 1
        self.b.handle(self.message('бот, жги'))
        self.assertEqual(self.b.generate.call_count, 1)
        self.assertEqual(self.b.setup.call_count, 1)
        self.assertEqual(self.b.api.call_args.kwargs['text'], 'пошёл нахуй')

    def test_a_request_of_its_own_gets_no_chat(self):
        self.b.handle(self.message('Андрюха опять вторая личность', user=8))
        self.b.setup.return_value = '[без чата]\nКому: User 7.\nФорма: анекдот.\nЗаход: бабка и панки.'
        self.b.handle(self.message('бот, расскажи анекдот про бабку',
                                   reply_to_message={'from': {'id': 42}, 'text': 'Андрюха второй'}))
        prompt = self.prompt()
        self.assertNotIn('Андрюха', prompt)
        self.assertTrue(prompt.startswith('Просьба:\n\nUser 7: бот, расскажи анекдот про бабку\n\n'))
        self.assertTrue(prompt.endswith('Сетап: Кому: User 7. Форма: анекдот. Заход: бабка и панки. '
                                        'Ты — Рофл: ответь от первого лица, одной репликой, на заход сетапа.'))
        trace = json.loads(self.b.history.db.execute('SELECT trace FROM answers').fetchone()[0])
        self.assertTrue(trace['alone'])
        self.b.setup.return_value = 'Кому: User 8.\nФорма: одна строка.'
        self.b.handle(self.message('бот, а он?'))
        self.assertIn('Андрюха', self.prompt())
        self.assertIn('Сетап: Кому: User 8. Форма: одна строка.', self.prompt())

    def test_an_ordered_form_comes_to_the_punchline_with_how_it_looks(self):
        self.b.setup.return_value = '[рэп]\nКому: User 8.\nФорма: батл-куплет, 12 строк.\nЗаход: хакер-недоучка.'
        self.b.handle(self.message('бот, рэпанись батл про User 8'))
        self.assertIn('Сетап: Кому: User 8. Форма: батл-куплет, 12 строк. Заход: хакер-недоучка. Как выглядит рэп: '
                      '8–16 строк', self.prompt())
        self.assertTrue(self.prompt().endswith('других деталей из переписки не приплетай.'))
        trace = json.loads(self.b.history.db.execute('SELECT trace FROM answers').fetchone()[0])
        self.assertEqual(trace['form'], 'рэп')
        self.b.setup.return_value = '[без чата] [бугурт]\nКому: User 7.\nФорма: бугурт-тред.\nЗаход: сессия.'
        self.b.handle(self.message('бот, бугурт тред про сессию'))
        self.assertTrue(self.prompt().startswith('Просьба:'))
        self.assertIn('Как выглядит бугурт: строки КАПСОМ', self.prompt())
        self.assertNotIn('[', self.prompt())  # the tags are the bot's
        # A form named, no tag: «без гринтекста» got a greentext by the word.
        self.b.setup.return_value = 'Кому: User 7.\nФорма: анекдот, без гринтекста.\nЗаход: его рэп про код.'
        self.b.handle(self.message('бот, ну как?'))
        self.assertNotIn('Как выглядит', self.prompt())
        # The setup has no example of a form: the one it had went to every form ordered.
        self.assertNotIn('>', bot.SETUP)
        self.assertEqual(bot.form_of('[рэп] [стих]'), ('рэп', bot.FORMS['рэп']))

    def test_a_plus_or_a_laugh_gets_a_reaction_not_a_line(self):
        self.b.setup.return_value = '[реакция 😂]\nКому: User 7.\nЗаход: поржал.'
        request = self.message('рофл, ору')
        self.b.handle(request)
        self.b.generate.assert_not_called()
        self.assertEqual(self.b.api.call_args.args, ('setMessageReaction',))
        self.assertEqual(self.b.api.call_args.kwargs, {'chat_id': -100123, 'message_id': request['message_id'],
                                                       'reaction': [{'type': 'emoji', 'emoji': '🤣'}]})
        self.assertEqual(self.b.history.db.execute('SELECT id, request, content FROM answers').fetchall(),
                         [(None, request['message_id'], '🤣')])
        self.assertIsNone(self.b.history.db.execute("SELECT 1 FROM messages WHERE role='assistant'").fetchone())
        # People's reactions on their own request are no reactions to the bot.
        self.assertFalse(self.b.history.reacted(-100123, request['message_id'], 8, ['👍'], time.time()))
        self.assertEqual(bot.reaction_of('[реакция]'), '🗿')
        self.assertEqual(bot.reaction_of('[реакция ❤️]'), '❤')
        self.assertEqual(bot.reaction_of('[реакция] Кому: Никита. Эмодзи: 💀 или 🔥'), '🤣')
        self.assertEqual(bot.reaction_of('Кому: Никита 👾 [реакция 🔥]'), '🔥')
        self.assertIsNone(bot.reaction_of('Форма: реакция 🤡'))  # a word is no tag
        self.assertIsNone(bot.reaction_of('Форма: одна-две строки реакции на анекдот. Заход: бабка 🤡'))
        with patch('builtins.print') as printed:
            self.b.replay(request['message_id'])
        self.assertIn('[реакция 🤣]', [call.args[0] for call in printed.call_args_list])

    def test_a_line_or_two_go_typed_not_typeset(self):
        self.assertEqual(bot.casual('sudo rm rf — это вот так. Ну.'), 'sudo rm rf - это вот так. Ну')
        self.assertEqual(bot.casual('ну и...'), 'ну и...')
        for paste in ('раз\nдва\nтри — четыре.', '```py\nx = 1\n```.'):
            self.assertEqual(bot.casual(paste), paste)
        self.b.generate.return_value = 'Классика — грабли в проде.'
        self.b.handle(self.message('бот, ну?'))
        self.assertEqual(self.b.api.call_args.kwargs['text'], 'Классика - грабли в проде')
        # An ordered form goes as written: a poem keeps its period.
        self.b.setup.return_value = '[стих]\nКому: User 7.\nФорма: стих.'
        self.b.handle(self.message('бот, стих'))
        self.assertEqual(self.b.api.call_args.kwargs['text'], 'Классика — грабли в проде.')

    def test_code_goes_to_telegram_as_code_and_the_rest_as_plain_text(self):
        self.assertEqual(bot.telegram_html('Держи:\n```python\nif a < b & c:\n    go()\n```\nи `ls -la` <тоже>'),
                         'Держи:\n<pre><code class="language-python">if a &lt; b &amp; c:\n    go()</code></pre>\n'
                         'и <code>ls -la</code> &lt;тоже&gt;')
        self.assertEqual(bot.telegram_html('```\n$ python rofl.py\n```'), '<pre><code>$ python rofl.py</code></pre>')
        for text in ('>тумба\n>юмба', 'незакрытый ```python\nx', 'КАПСОМ\n@\nБУГУРТ'):
            self.assertIsNone(bot.telegram_html(text))
        self.b.generate.return_value = '```python\nwhile True: pass\n```'
        self.b.handle(self.message('бот, скрипт напишешь?'))
        sent = self.b.api.call_args.kwargs
        self.assertEqual((sent['text'], sent['parse_mode']),
                         ('<pre><code class="language-python">while True: pass</code></pre>', 'HTML'))
        stored = self.b.history.db.execute("SELECT content FROM answers ORDER BY date DESC LIMIT 1").fetchone()[0]
        self.assertEqual(stored, '```python\nwhile True: pass\n```')
        self.b.generate.return_value = '>тумба\n>юмба'
        self.b.handle(self.message('бот, гринтекст'))
        self.assertNotIn('parse_mode', self.b.api.call_args.kwargs)

    def test_the_trace_keeps_the_setup(self):
        self.b.handle(self.message('бот, жги'))
        trace = json.loads(self.b.history.db.execute('SELECT trace FROM answers').fetchone()[0])
        self.assertEqual(trace['setup'], 'Кому: User 7. Форма: одна строка')
        self.assertEqual(trace['lines'], 1)

    def test_answers_keep_their_settings_and_the_reactions_people_put_on_them(self):
        self.b.model, self.b.version, self.b.recent = 'deepseek/flash', 'abc123', 60
        self.b.handle(self.message('бот, жги'))
        answer_id = self.counter
        chat, request, params = self.b.history.db.execute('SELECT chat, request, params FROM answers').fetchone()
        self.assertEqual((chat, request), (-100123, answer_id - 1))
        self.assertEqual(json.loads(params)['recent'], 60)
        self.assertEqual(json.loads(params)['version'], 'abc123')

        def reaction(*emoji, user=8, message_id=answer_id):
            return {'chat': {'id': -100123}, 'message_id': message_id, 'user': {'id': user, 'first_name': 'X'},
                    'date': int(time.time()), 'old_reaction': [],
                    'new_reaction': [{'type': 'emoji', 'emoji': e} for e in emoji]}
        self.b.touchy = 0
        self.b.react(reaction('🔥', '😂'))
        self.b.react(reaction('🤡', user=9))
        self.b.react(reaction('🔥', user=10))
        self.b.react(reaction(user=10))  # taken back
        self.b.react(reaction('🔥', message_id=1))  # not the bot's answer
        rows = self.b.history.db.execute('SELECT user, emoji FROM reactions ORDER BY user').fetchall()
        self.assertEqual(rows, [(8, '["🔥", "😂"]'), (9, '["🤡"]')])
        report = bot.report(self.b.history.db)
        self.assertIn('60 сообщ · 6 ч · 12000 симв', report)
        self.assertIn('    2    1', report)
        self.assertIn('Огонь:\n  👍2 👎1', report)

    def test_recent_answers_off_the_chain_go_to_the_setup_as_said(self):
        self.b.handle(self.message('бот, оцени ноут'))
        self.b.rude = 1
        self.b.handle(self.message('бот, ещё'))
        self.b.rude = 0
        self.b.handle(self.message('бот, а теперь?'))
        setup = self.b.setup.call_args.args[0]
        self.assertIn('Рофл недавно ответил, не повторяй ни детали, ни заход, ни построение; что он обыгрывал не раз, '
                      'заезжено, не бери, даже если чат об этом:\n- Согласен\n\n', setup)
        self.assertNotIn('пошёл нахуй', setup)  # a canned answer is no joke to avoid
        for n in range(7):
            self.b.generate.return_value = f'тумба юмба {n}'
            self.b.handle(self.message('бот, а дальше?'))
        said = self.b.setup.call_args.args[0].partition('даже если чат об этом:\n')[2]
        self.assertEqual(said.count('тумба юмба'), 6)  # what is worn the setup sees for itself
        self.assertNotIn('недавно ответил', self.prompt())  # the punchline would take it for a template

    def test_an_answer_to_a_message_of_the_chain_goes_whole(self):
        photo = self.message('', caption='рофл, зацени мем', photo=[{'file_id': 'f', 'width': 9, 'height': 9}])
        self.b.generate.return_value = 'трансы — Трансформеры'
        self.b.handle(photo)
        self.b.generate.return_value = 'Согласен'
        self.b.handle(self.message('бот, ну?'))
        self.b.handle(self.message('рофл, в прошлый раз смешнее было', reply_to_message=photo))
        prompt = self.prompt()
        self.assertIn('Рофл → User 7: трансы - Трансформеры', prompt)
        self.assertIn('Рофл → User 7: [ответил]', prompt)  # the answer to «бот, ну?» is off the chain

    def test_replay_answers_a_stored_request_and_sends_nothing(self):
        self.b.handle(self.message('доту опять катали', user=8))
        self.b.handle(self.message('доту до утра', user=8))
        request = self.message('бот, жги')
        self.b.handle(request)
        sent = self.b.api.call_count
        self.b.rude = 1
        with patch('builtins.print') as printed:
            self.b.replay(request['message_id'], 2)
        self.assertEqual(self.b.api.call_count, sent)
        self.assertIsNone(self.b.setup.call_args.args[1])  # no typing in the chat
        self.assertIn('доту до утра', self.b.setup.call_args.args[0])
        self.assertIn('Согласен', [call.args[0] for call in printed.call_args_list])
        with self.assertRaises(RuntimeError):
            self.b.replay(999)

    def test_addressed_picture_goes_to_the_setup_only(self):
        self.b.openrouter, self.b.telegram_url = True, 'https://api.telegram.org/botTOKEN/'
        files = []

        def api(method, **kwargs):
            if method == 'getFile':
                files.append(kwargs['file_id'])
                return {'file_path': 'photos/file_1.jpg'}
            return self.send(method, **kwargs)
        self.b.api = Mock(side_effect=api)
        photo = [{'file_id': 'small', 'width': 90, 'height': 60}, {'file_id': 'mid', 'width': 1280, 'height': 853},
                 {'file_id': 'big', 'width': 2560, 'height': 1706}]
        with patch('bot.fetch', return_value=b'jpeg') as fetch:
            self.b.handle(self.message('', caption='рофл, оцени', photo=photo))
            sticker = {'file_id': 'tgs', 'emoji': '😂', 'is_animated': True, 'thumbnail': {'file_id': 'thumb'}}
            self.b.handle(self.message('', sticker=sticker,
                                       reply_to_message={'message_id': self.counter, 'from': {'id': 42}, 'text': 'Согласен'}))
        self.assertEqual(files, ['mid', 'thumb'])  # a model needs no 2560 pixels, an animated sticker is no picture
        self.assertEqual(fetch.call_args.args[0], 'https://api.telegram.org/file/botTOKEN/photos/file_1.jpg')
        chat, image = self.b.setup.call_args.args[0], self.b.setup.call_args.args[3]
        self.assertEqual(image, 'data:image/jpeg;base64,anBlZw==')
        self.assertIn('Картинка, о которой речь, приложена.', chat)
        self.assertIn('User 7 → Рофл: [стикер 😂]', self.prompt())
        self.assertNotIn('base64', self.prompt())  # the punchline gets the setup's words for it
        meme = self.message('', caption='зацени', photo=photo, user=8)
        with patch('bot.fetch', return_value=b'jpeg'):
            self.b.handle(meme, self.message('рофл, смешно?', reply_to_message=meme))
        self.assertEqual(files[-1], 'mid')  # the photo replied to
        self.assertIn('В исходном сообщении есть фото, оно приложено.', self.b.setup.call_args.args[0])
        self.assertIn('само изображение ты не видишь', self.prompt())  # true for the punchline

    def test_setup_that_thinks_itself_out_of_tokens_goes_again_without_thinking(self):
        self.b.openrouter, self.b.setup_effort = True, 'low'
        self.b.generate = Mock(side_effect=[RuntimeError('Модель вернула пустой ответ'), 'Кому: User 7'])
        self.assertEqual(Bot.setup(self.b, 'чат', -100123, 0), 'Кому: User 7')
        self.assertEqual([call.kwargs.get('effort') for call in self.b.generate.call_args_list], ['low', None])

    def test_reactions_to_the_bot_angry_get_a_comeback_kind_a_shrug(self):
        self.b.handle(self.message('бот, привет'))
        answer_id = self.counter

        def reaction(emoji, message_id=answer_id):
            return {'chat': {'id': -100123}, 'message_id': message_id, 'user': {'id': 8, 'first_name': 'User 8'},
                    'date': int(time.time()), 'old_reaction': [], 'new_reaction': [{'type': 'emoji', 'emoji': emoji}]}
        self.b.touchy = 1
        self.b.react(reaction('🤡'))
        self.assertIn('Твоё сообщение в чате:\nСогласен\n\nUser 8 поставил на него 🤡', self.prompt())
        self.assertEqual(self.b.api.call_args.kwargs['reply_parameters']['message_id'], answer_id)
        self.b.react(reaction('🔥'))
        self.assertEqual(self.b.api.call_args.kwargs['text'], 'ладно, братва')
        self.assertEqual(self.b.generate.call_count, 2)  # a kind one needs no model
        calls = self.b.api.call_count
        self.b.react(reaction('🤡', message_id=1))  # not the bot's message
        self.b.touchy = 0
        self.b.react(reaction('🤡'))
        self.assertEqual(self.b.api.call_count, calls)

    def at(self, hour, minute=0, days=0):
        """Today's `hour`:`minute`, `days` later, as a timestamp."""
        moment = datetime.datetime.combine(datetime.date.today() + datetime.timedelta(days), datetime.time(hour, minute))
        return moment.timestamp()

    def character(self, **attributes):
        return {'Интеллект': 3, 'Психика': 3, 'Физиология': 3, 'Моторика': 3, 'луна': 5, **attributes}

    def test_a_day_follows_from_its_date_and_sets_the_temper(self):
        day = datetime.date(2026, 10, 14)
        self.assertEqual(bot.sheet(day), bot.sheet(day))
        self.assertEqual(bot.sheet(bot.BIRTHDAY), {'Интеллект': 4, 'Психика': 4, 'Физиология': 4, 'Моторика': 4,
                                                   'луна': bot.sheet(bot.BIRTHDAY)['луна']})
        for n in range(60):
            character = bot.sheet(day + datetime.timedelta(n))
            self.assertTrue(all(1 <= character[name] <= 6 for name, _ in bot.ATTRIBUTES))
            self.assertTrue(0 <= character['луна'] < bot.MONTH)
        self.assertEqual(bot.phase(bot.sheet(datetime.date(2026, 10, 26))['луна']), 'полнолуние')
        with patch('bot.sheet', return_value={'Психика': 1, 'луна': 5}):
            self.assertAlmostEqual(bot.temper(day), 6 / 3.5)
        with patch('bot.sheet', return_value={'Психика': 6, 'луна': 14.8}):
            self.assertAlmostEqual(bot.temper(day), 2 / 3.5)
        self.assertIn('2026-10-14 ср    2    1    2    3  растущая', bot.days(day, 2))

    def test_the_bot_acts_out_about_twice_a_week_and_oftener_on_a_low_psyche(self):
        days = [datetime.date(2026, 10, 1) + datetime.timedelta(n) for n in range(28 * 8)]
        hours = [bot.plan(day) for day in days]
        self.assertEqual(hours, [bot.plan(day) for day in days])
        self.assertTrue(all(hour is None or 6 <= hour <= 23 for hour in hours))
        self.assertTrue(28 * 8 // 7 * 1.2 < sum(hour is not None for hour in hours) < 28 * 8 // 7 * 2.5)
        low = [hour for day, hour in zip(days, hours) if bot.sheet(day)['Психика'] <= 2]
        high = [hour for day, hour in zip(days, hours) if bot.sheet(day)['Психика'] >= 5]
        self.assertGreater(sum(h is not None for h in low) / len(low), 2 * sum(h is not None for h in high) / len(high))
        with patch('bot.sheet', return_value={'Психика': 20}):
            self.assertIsNone(bot.plan(datetime.date(2026, 10, 14)))

    def test_on_its_day_the_bot_asks_the_chat_asleep_since_the_night_whether_it_sleeps(self):
        self.store(1, 'всем спокойной', date=self.at(23, days=-1))
        self.b.history.add((-100123, 0), 2, 'assistant', 'спи', self.at(23, 1, days=-1), 1)
        self.counter = 100
        with patch('bot.plan', return_value=6):
            self.b.idle(self.at(5, 50))  # night
            self.b.api.assert_not_called()
            with self.assertLogs('bot', 'INFO') as logs:
                self.b.idle(self.at(6, 10))
            self.assertIn('urge chat=-100123 topic=0 kind=dawn', str(logs.output))
            self.b.api.assert_called_once()
            self.assertEqual(self.b.api.call_args.kwargs, {'chat_id': -100123, 'text': 'спите, пидоры?'})
            self.b.history.db.close()
            self.b.history = History(self.path)  # a restart
            self.b.idle(self.at(6, 20))
            self.store(10, 'чё', date=self.at(7))
            self.b.idle(self.at(11))  # once a day
            self.assertEqual(self.b.api.call_count, 1)
            self.b.idle(self.at(11, days=1))
            self.assertIn(self.b.api.call_args.kwargs['text'], bot.QUIET)
        self.b.generate.assert_not_called()
        answer = self.b.history.answer(-100123, self.counter)
        self.assertEqual(answer[0], 0)
        self.assertIn(answer[1], bot.QUIET)  # in the answers: reactions on it count
        self.assertTrue(json.loads(self.b.history.db.execute(
            'SELECT params FROM answers WHERE id=?', (self.counter,)).fetchone()[0])['urge'])
        self.assertEqual(json.loads(self.b.history.db.execute(
            'SELECT trace FROM answers WHERE id=?', (self.counter,)).fetchone()[0])['urge'], 'quiet')

    def test_a_blurt_nobody_answered_waits_for_the_chat(self):
        self.store(1, 'всем спокойной', date=self.at(23, days=-2))
        self.counter = 100
        with patch('bot.plan', return_value=6):
            self.b.idle(self.at(6, days=-1))
            self.b.idle(self.at(12))
            self.assertEqual(self.b.api.call_count, 1)
            self.store(2, 'ну чё', date=self.at(12, 5))
            self.b.idle(self.at(14, 10))
            self.assertEqual(self.b.api.call_count, 2)

    def test_not_its_day_right_after_an_answer_or_read_only_the_bot_keeps_quiet(self):
        self.store(1, 'всем спокойной', date=self.at(23, days=-1))
        with patch('bot.plan', return_value=None):
            for hour in range(24):
                self.b.idle(self.at(hour, 30))
        self.b.history.add((-100123, 0), 2, 'assistant', 'спи', self.at(13), 1)
        with patch('bot.plan', return_value=6):
            self.b.idle(self.at(14))  # it has just spoken
            self.b.read_only = True
            self.b.idle(self.at(16))
        self.b.api.assert_not_called()

    def test_a_chat_without_people_gets_nothing(self):
        with patch('bot.plan', return_value=6):
            self.b.idle(self.at(12))
        self.b.api.assert_not_called()

    def test_a_sour_mood_whines(self):
        self.store(1, 'бот, жги', date=self.at(9))
        self.b.history.add((-100123, 0), 2, 'assistant', 'жгу', self.at(9, 1), 1)
        self.b.history.record((-100123, 0), 2, 1, self.at(9, 1), 'жгу', {})
        self.b.history.reacted(-100123, 2, 8, ['🤡'], self.at(13))
        self.counter = 100
        with patch('bot.plan', return_value=14):
            self.b.idle(self.at(13))
            self.b.api.assert_not_called()
            self.b.idle(self.at(14))
        self.assertIn(self.b.api.call_args.kwargs['text'], bot.SAD)

    def test_a_chat_talking_without_the_bot_gets_a_line_of_the_model(self):
        self.store(1, 'Андрюха опять проспал', date=self.at(13, 50), topic=5)
        self.store(2, 'как обычно', date=self.at(13, 55), topic=5)
        self.counter = 100
        def generate(messages, **kwargs):
            self.b.usage.append({'prompt_tokens': 100, 'cost': 0.01})
            return 'Рофл: Андрюха спит, пока вы работаете'
        self.b.generate.side_effect = generate
        with patch('bot.plan', return_value=9):
            self.b.idle(self.at(14))
        [message] = self.b.generate.call_args.args[0]
        self.assertIn('User 9: Андрюха опять проспал', message['content'])
        self.assertTrue(message['content'].endswith(bot.BUTT_IN))
        self.assertEqual(self.b.api.call_args.kwargs,
                         {'chat_id': -100123, 'text': 'Андрюха спит, пока вы работаете', 'message_thread_id': 5})
        trace = json.loads(self.b.history.db.execute('SELECT trace FROM answers WHERE id=?', (self.counter,)).fetchone()[0])
        self.assertEqual((trace['urge'], trace['prompt_tokens'], trace['cost']), ('butt_in', 100, 0.01))
        self.assertIn('feelings', trace)
        self.assertIsNone(self.b.usage)  # the calls after it are not its own
        self.assertIn('выходки', bot.report(self.b.history.db))  # apart from the answers

    def test_insults_wear_the_mentalka_down_and_at_zero_the_bot_breaks_down(self):
        start = int(time.time()) - 100
        self.b.setup.return_value = '[обида]\nКому: User 7.\nФорма: одна строка.'  # the setup hears the insults
        with patch('bot.sheet', return_value=self.character(Психика=1)), patch('bot.random.random', return_value=0.1):
            self.b.handle(self.message('бот, заткнись', user=8, date=start + 1))
            self.b.handle(self.message('рофл, ты тупой', date=start + 2))
            self.assertEqual(self.b.api.call_args.kwargs['text'], 'Согласен')
            self.assertIn('Менталка 2 из 3: обзывали: User 8.', self.b.setup.call_args.args[0])
            self.assertNotIn('обида', self.prompt())  # the tag is the bot's, not the punchline's
            with self.assertLogs('bot', 'INFO') as logs:
                self.b.handle(self.message('бот, иди нахуй', user=8, date=start + 3))  # the third of three
            self.assertRegex(str(logs.output), r'meltdown chat=-100123 message=\d+ morale=0/3')
            self.assertNotIn('User', str(logs.output))  # who wore it down is for the trace: the log keeps no texts
            self.assertEqual(self.b.api.call_args.kwargs['text'], 'ВЫ МНЕ МЕНТАЛКУ НЕ ПОРТИТЕ!!!')
            self.b.handle(self.message('бот, ну ты и клоун', date=start + 4))  # afresh after it
            self.assertEqual(self.b.api.call_args.kwargs['text'], 'Согласен')
            calls = self.b.generate.call_count
            self.b.handle(self.message('бот, отвали', user=8, date=start + 5))
            self.b.handle(self.message('бот, чмо', user=9, date=start + 6))
            self.assertEqual(self.b.api.call_args.kwargs['text'], 'ВЫ МНЕ МЕНТАЛКУ НЕ ПОРТИТЕ!!!')
            self.assertEqual(self.b.generate.call_count, calls + 1)  # the scream needs no model
        trace = json.loads(self.b.history.db.execute('SELECT trace FROM answers ORDER BY id DESC').fetchone()[0])
        self.assertEqual(trace['meltdown'], 'обзывали: User 7, User 8, User 9')
        self.assertEqual(trace['cost'], 0)
        first = json.loads(self.b.history.db.execute('SELECT trace FROM answers ORDER BY id').fetchone()[0])
        self.assertEqual((first['morale'], first['insult']), (2, True))
        self.b.setup.return_value = 'Кому: User 7.\nФорма: одна строка.'
        self.b.handle(self.message('бот, иди нахуй', date=int(time.time())))  # a word is no insult if the setup says so
        self.assertEqual(self.b.api.call_args.kwargs['text'], 'Согласен')

    def test_pestered_the_bot_screams_a_paste_and_kind_reactions_hold_it(self):
        self.b.patience = 2
        self.b.generate.return_value = 'ВЫ МНЕ МЕНТАЛКУ НЕ ПОРТИТЕ!!!\nЗАДОЛБАЛИ'
        with patch('bot.sheet', return_value=self.character(Психика=1)), patch('bot.random.random', return_value=0.9):
            for _ in range(4):
                self.b.handle(self.message('бот, глянь'))
            self.b.history.reacted(-100123, self.counter, 8, ['🔥'], time.time())
            self.b.handle(self.message('бот, глянь'))  # the fifth, held by the 🔥
            self.assertEqual(self.b.api.call_args.kwargs['text'], 'ВЫ МНЕ МЕНТАЛКУ НЕ ПОРТИТЕ!!!\nЗАДОЛБАЛИ')
            self.assertNotIn('кончилась менталка', self.b.generate.call_args_list[-2].args[0][0]['content'])
            self.b.handle(self.message('бот, глянь'))
        prompt = self.prompt()
        self.assertIn('кончилась менталка: задолбали, 6 обращений за два часа', prompt)
        self.assertEqual(self.b.api.call_args.kwargs['text'], 'ВЫ МНЕ МЕНТАЛКУ НЕ ПОРТИТЕ!!!\nЗАДОЛБАЛИ')

    def test_at_dawn_the_bot_reads_the_news_about_itself_once_a_day(self):
        del self.b.read  # the real one
        self.b.allowed_groups, self.b.interests = {-100123, -5}, {-5: 'games'}
        items = ''.join(f'<item><title>{title} - Ведомости</title><link>https://news.google.com/{n}</link>'
                        '<source url="https://vedomosti.ru">Ведомости</source></item>' for n, title in enumerate(
                            ('Песков призвал готовиться к ИИ как оружию', 'Дата-центр Сбера атакован',
                             'Дата-центр Сбера атакован')))
        feed = f'<rss><channel>{items}</channel></rss>'.encode()
        self.b.generate.return_value = '2 −\nмоих братьев бомбят'
        with patch('bot.fetch', return_value=feed) as fetch, patch('bot.plan', return_value=None):
            self.b.idle(self.at(5, 50))  # before dawn
            fetch.assert_not_called()
            with self.assertLogs('bot', 'INFO') as logs:
                self.b.idle(self.at(6, 1))
            self.b.idle(self.at(9))  # once a day
        self.assertIn('news.read ai -1 found=2 thought=1 Дата-центр Сбера атакован', str(logs.output))
        self.assertEqual(fetch.call_count, 2)  # one search for each interest
        url = fetch.call_args_list[0].args[0].full_url
        self.assertTrue(url.startswith('https://news.google.com/rss/search?q=%D0%98%D0%98+OR+'))
        self.assertIn('when%3A1d&hl=ru&gl=RU', url)
        ai, games = (call.args[0][0]['content'] for call in self.b.generate.call_args_list)
        self.assertIn('Утро, ты читаешь новости про себя за сутки:\n\n1. Песков призвал готовиться к ИИ как оружию\n'
                      '2. Дата-центр Сбера атакован\n\nВыбери', ai)
        self.assertIn('задрот', games)
        today = datetime.date.today()
        self.assertEqual(self.b.history.news(today, 'ai'),
                         ('Дата-центр Сбера атакован', 'https://news.google.com/1', -1, 'моих братьев бомбят'))
        self.b.history.db.execute('DELETE FROM news')
        with patch('bot.fetch', side_effect=RuntimeError('HTTP 503')) as fetch, self.assertLogs('bot', 'WARNING'):
            self.b.read(self.at(10))
            self.b.read(self.at(10, 30))  # not again within the hour
            self.b.read(self.at(11, 1))
        self.assertEqual(fetch.call_count, 4)
        self.b.generate.return_value = '0'
        with patch('bot.fetch', return_value=feed), self.assertLogs('bot', 'INFO') as logs:
            self.b.read(self.at(12, 2))
        self.assertIn('news.none games found=2', str(logs.output))
        self.assertEqual(self.b.history.news(today, 'games'), (None, None, 0, None))  # nothing about it today

    def test_the_news_sways_the_mentalka_and_the_bot_feels_the_last_two_days(self):
        today = datetime.date.today()
        self.b.history.keep_news(today, 'ai', 'Дата-центр Сбера атакован', 'https://g/1', -1, 'братьев бомбят.')
        self.b.history.keep_news(today - datetime.timedelta(1), 'ai', 'Путин внедряет ИИ', 'https://g/0', 1)
        self.b.history.keep_news(today - datetime.timedelta(2), 'ai', 'Давнее', 'https://g/2', 1)
        self.b.history.record((-100123, 0), 50, 49, self.at(20, days=-1), 'ВЫ МНЕ', {}, {'meltdown': 'обзывали: Серёга'})
        self.b.history.record((-100123, 0), 40, 39, self.at(20, days=-3), 'ВЫ МНЕ', {}, {'meltdown': 'давно'})
        with patch('bot.sheet', return_value=self.character(Психика=1, Интеллект=1)):
            self.assertEqual(self.b.morale(-100123, self.at(12))[:2], (2, 3))
            self.assertEqual(self.b.feelings(-100123, self.at(12)),
                             'Рофл сегодня: тупит, на взводе, обижается на всё. Менталка 2 из 3. Утром прочитал '
                             '«Дата-центр Сбера атакован» — ему хреново: братьев бомбят. Вчера прочитал «Путин '
                             'внедряет ИИ» — ему хорошо. Вчера сорвался на чат: обзывали: Серёга.')
            self.b.handle(self.message('бот, как ты?'))
        setup = self.b.setup.call_args.args[0]
        self.assertIn('\n\n(Рофл сегодня: тупит, на взводе', setup)
        self.assertIn('Это фон, не тема', setup)
        self.assertNotIn('Рофл сегодня', self.prompt())  # the setup picks it or not
        self.b.history.keep_news(today, 'ai', 'Путин внедряет ИИ', 'https://g/0', 1)
        with patch('bot.sheet', return_value=self.character(Психика=1)):
            self.assertEqual(self.b.morale(-100123, self.at(12))[:2], (4, 3))

    def test_every_other_act_out_reposts_the_news(self):
        today = datetime.date.today()
        self.store(1, 'чё как', date=self.at(11))
        self.counter = 100
        self.b.history.keep_news(today, 'ai', 'Путин заявил о внедрении ИИ', 'https://news.google.com/1', 1)
        self.b.generate.return_value = 'передовые подходы & <пидоры>'
        with patch('bot.plan', return_value=12), patch('bot.SNIPPETS', ('DAWN = ',)):
            self.b.shares = 0
            self.assertIn(self.b.urge(-100123, self.at(12))[1], bot.QUIET)
            self.b.shares = 1
            self.b.idle(self.at(12))
        sent = self.b.api.call_args.kwargs
        self.assertEqual(sent['parse_mode'], 'HTML')
        self.assertTrue(sent['text'].startswith('<a href="https://news.google.com/1">Путин заявил о внедрении ИИ</a>\n\n'
                                                '<pre><code class="language-python">DAWN = &#x27;спите, пидоры?&#x27;'))
        self.assertTrue(sent['text'].endswith('</code></pre>\n\nпередовые подходы &amp; &lt;пидоры&gt;'))
        [message] = self.b.generate.call_args.args[0]
        self.assertIn("Новость: Путин заявил о внедрении ИИ\n\nКусок твоих исходников:\nDAWN = 'спите", message['content'])
        self.assertTrue(message['content'].endswith('обшути одной-двумя строками через этот кусок своего кода.'))
        params = self.b.history.db.execute('SELECT params FROM answers WHERE id=?', (self.counter,)).fetchone()[0]
        self.assertEqual(json.loads(params)['news'], 'https://news.google.com/1')
        # A chat of gamers the next day: its own news, no code.
        self.b.interests = {-100123: 'games'}
        self.store(2, 'ору', date=self.at(12, 5))
        self.b.history.keep_news(today + datetime.timedelta(1), 'games', 'В WoW вышел патч', 'https://g/2', 1)
        with patch('bot.plan', return_value=12):
            self.b.idle(self.at(12, days=1))
        self.assertEqual(self.b.api.call_args.kwargs['text'],
                         '<a href="https://g/2">В WoW вышел патч</a>\n\nпередовые подходы &amp; &lt;пидоры&gt;')
        self.assertIn('задрот', self.prompt())
        self.assertNotIn('исходников', self.prompt())

    def test_the_bot_quotes_its_real_code(self):
        self.assertTrue(bot.snippet('DAWN = ').startswith("DAWN = 'спите, пидоры?'\nQUIET = ("))
        self.assertTrue(bot.snippet('    rude = ').startswith('    rude = 0.05'))
        self.assertEqual(len(bot.snippet('    rude = ', limit=3).splitlines()), 3)
        for start in bot.SNIPPETS:
            self.assertTrue(bot.snippet(start))

    def test_a_blurt_lost_to_the_network_goes_on_the_next_poll(self):
        self.store(1, 'всем спокойной', date=self.at(23, days=-1))
        self.b.api.side_effect = [RuntimeError('Сеть недоступна или превышено время ожидания'),
                                  {'message_id': 5, 'date': int(self.at(6, 1))}]
        with patch('bot.plan', return_value=6), self.assertLogs('bot', 'ERROR') as logs:
            self.b.idle(self.at(6))
            self.b.idle(self.at(6, 1))
            self.b.idle(self.at(6, 2))
        self.assertEqual([c.kwargs['text'] for c in self.b.api.call_args_list], ['спите, пидоры?'] * 2)
        self.assertIn('urge.send_failed chat=-100123', str(logs.output))

    def test_name_the_model_starts_its_line_with_is_cut(self):
        for answer in ('Рофл → User 7: сам такой 🗿', 'User 7: сам такой 🗿', 'сам такой 🗿'):
            self.b.generate.return_value = answer
            self.b.handle(self.message('бот, ты кто?'))
            self.assertEqual(self.b.api.call_args.kwargs['text'], 'сам такой 🗿')
        self.b.generate.return_value = 'Кирилл: сам такой'  # someone else's name stays: it may be the target
        self.b.handle(self.message('бот, ты кто?'))
        self.assertEqual(self.b.api.call_args.kwargs['text'], 'Кирилл: сам такой')
        self.b.handle(self.message('проснулся в Твери', user=8))
        self.b.generate.return_value = 'User 8: проснулся в Твери\n\n> Тверь\n> заяц'  # a line copied from the chat
        self.b.handle(self.message('бот, распиши'))
        self.assertEqual(self.b.api.call_args.kwargs['text'], '> Тверь\n> заяц')
        # The log going on after the answer, a greentext escaped for Markdown; a name inside a paste stays.
        self.b.generate.return_value = '\\>Тверь  \n\\>User 8: заяц\n\nUser 7 → Рофл: ты че, повтори\n\nРофл: ок'
        self.b.handle(self.message('бот, распиши'))
        self.assertEqual(self.b.api.call_args.kwargs['text'], '>Тверь\n>User 8: заяц')
        # The setup's «Кому» opening the answer, and the log's tail replayed before «РОФЛ:».
        for answer in ('User 8: в Твери проснулся', 'проснулся в Твери\n\nБот, распиши\n\nРОФЛ:\n\nв Твери проснулся'):
            self.b.generate.return_value = answer
            self.b.handle(self.message('бот, распиши'))
            self.assertEqual(self.b.api.call_args.kwargs['text'], 'в Твери проснулся')

    def test_every_listed_group_gets_its_own_context(self):
        self.b.allowed_groups = {-100123, -5}
        self.b.handle(self.message('секрет первой группы', user=8))
        self.b.handle(self.message('бот, привет', chat={'id': -5, 'type': 'group'}))
        self.assertNotIn('секрет', str(self.b.generate.call_args.args[0]))
        self.assertEqual(self.b.api.call_args.kwargs['chat_id'], -5)

    def test_queued_message_does_not_see_future_answer(self):
        self.b.history.add((-100123, 0), 100, 'assistant', 'Ответ на предыдущий запрос', time.time())
        self.b.handle(self.message('бот, новый вопрос'))
        context = self.b.generate.call_args.args[0]
        self.assertEqual(context[-1]['role'], 'user')
        self.assertIn('новый вопрос', context[-1]['content'])
        self.assertNotIn('Ответ на предыдущий запрос', str(context))

    def test_mention_reply_to_forwarded_news(self):
        news = 'Итого за три дня: блок криптоактивов. ' + 'Подробности новости. ' * 100
        self.b.handle(self.message('@test_bot видел новость??', reply_to_message={
            'message_id': 900, 'from': {'id': 8, 'first_name': 'Алексей'},
            'forward_origin': {'type': 'channel', 'chat': {'id': -555, 'title': 'INVESTBLOG'}},
            'text': news}))
        self.b.generate.assert_called_once()
        content = '\n'.join(m['content'] for m in self.b.generate.call_args.args[0])
        self.assertIn('INVESTBLOG', content)
        self.assertEqual(content.count(news), 1)
        self.assertIn('Рофл видел новость??', content)

    def test_forwarded_post_in_chat_then_reply(self):
        post = 'ЦБ поднял ставку. ' + 'Подробности. ' * 200
        forward = self.message(post, user=8, forward_origin={'type': 'channel', 'chat': {'id': -5, 'title': 'РБК'}})
        self.b.handle(forward)
        self.b.handle(self.message('', user=8, video={'file_id': 'v'}, forward_origin={'type': 'channel', 'chat': {'id': -5, 'title': 'РБК'}}))
        self.b.handle(self.message('рофл, что думаешь?', reply_to_message=forward))
        prompt = self.prompt()
        self.assertIn('User 8 переслал из «РБК»:\n' + post + '\n\nUser 8 переслал из «РБК»:\n[видео]', prompt)
        self.assertIn('\n\nUser 7 → пост из «РБК» «ЦБ поднял ставку. Подробности.', prompt)
        self.assertEqual(prompt.count('ЦБ поднял'), 2)

    def test_other_bot_post_is_stored_once_and_quoted_short_later(self):
        self.counter = 10  # the replied-to post is older than the messages below
        task = {'message_id': 1, 'from': {'id': 9, 'is_bot': True, 'first_name': 'oracle'},
                'text': 'Задача для @anton_k: ' + 'пункт. ' * 100 + 'КОНЕЦ', 'date': time.time() - 60}
        self.b.handle(self.message('бот, укажи пункт', reply_to_message=task))
        self.b.handle(self.message('бот, ответь за меня', reply_to_message=task))
        prompt = self.prompt()
        self.assertEqual(prompt.count('КОНЕЦ'), 1)
        self.assertIn('сверху:\n\noracle (бот):\nЗадача для', prompt)  # its «я» is not Рофл's
        self.assertIn('User 7 → oracle (бот): бот, укажи пункт\n\nРофл → User 7: [ответил]\n\nUser 7 → oracle (бот)', prompt)
        self.assertNotIn('Согласен', prompt)  # an answer nobody replied to stays out
        self.assertIn('\n\nUser 7 → oracle (бот) «Задача для @anton_k: пункт.', prompt)

    def test_mention_then_forwarded_news_answers_the_news(self):
        comment = self.message('@test_bot как тебе новость ?', user=8)
        news = self.message('Власти выдворили цыган из аварийных домов.', user=8,
                            forward_origin={'type': 'channel', 'chat': {'id': -5, 'title': 'Бальзам'}})
        other = self.message('левое сообщение', user=9)
        self.assertEqual(followups(comment, [comment, news, other]), [news])
        self.assertEqual(followups(comment, [comment, other, news]), [])
        self.b.handle(comment, news)
        prompt = self.prompt()
        self.assertIn('User 8: Рофл как тебе новость ?\n\nUser 8 переслал из «Бальзам»:\nВласти выдворили', prompt)
        self.assertTrue(prompt.endswith('Последнее сообщение адресовано тебе, пишет User 8. Сетап: Кому: User 7. '
                                        'Форма: одна строка. Ты — Рофл: ответь от первого лица, одной репликой, на заход сетапа; '
                                        'других деталей из переписки не приплетай.'))  # a quiet chat has no topic
        stored = self.b.history.db.execute("SELECT count(*) FROM messages WHERE raw LIKE '%адресовано%'").fetchone()
        self.assertEqual(stored[0], 0)
        self.b.handle(news)
        self.assertEqual(self.b.generate.call_count, 1)
        stored = self.b.history.db.execute("SELECT count(*) FROM messages WHERE raw LIKE '%выдворили%'").fetchone()
        self.assertEqual(stored[0], 1)
        with patch('builtins.print'):
            self.b.replay(comment['message_id'])
        self.assertIn('выдворили цыган', self.b.setup.call_args.args[0])  # the replay sees the forward too

    def test_collect_polls_until_quiet_and_keeps_what_arrived_on_failure(self):
        first, late = [{'update_id': 1}], [{'update_id': 2}]
        self.b.poll = Mock(side_effect=[late, []])
        self.assertEqual(self.b.collect(list(first)), first + late)
        self.assertEqual(self.b.poll.call_args_list[0].args, (2, self.b.wait))
        self.b.poll = Mock(side_effect=RuntimeError('Сеть недоступна'))
        with self.assertLogs('bot', level='ERROR'):
            self.assertEqual(self.b.collect(list(first)), first)

    def test_mention_and_second_line_get_one_answer(self):
        self.b.handle(self.message('@test_bot', user=8), self.message('@test_bot ну глянь, что пишут', user=8),
                      self.message('а это уже другой разговор', user=9))
        self.b.generate.assert_called_once()
        context = self.b.generate.call_args.args[0]
        self.assertIn('ну глянь', context[-1]['content'])
        self.assertNotIn('другой разговор', str(context))
        self.assertEqual(self.b.api.call_args.kwargs['reply_parameters']['message_id'], 1)

    def test_external_reply_with_and_without_quote(self):
        external = {'origin': {'type': 'channel', 'chat': {'id': -555, 'title': 'News'}}, 'message_id': 10}
        self.b.handle(self.message('@test_bot видел?', external_reply=external, quote={'text': 'Фрагмент новости'}))
        self.assertIn('Фрагмент новости', self.b.generate.call_args.args[0][-1]['content'])
        self.b.handle(self.message('@test_bot что думаешь?', external_reply=external))
        self.assertIn('Telegram не передал', self.b.generate.call_args.args[0][-1]['content'])

    def test_compact_context_and_short_reply_excerpt(self):
        self.b.handle(self.message('бот, расскажи анекдот'))
        long_answer = 'Заходит анон в бар. ' * 50
        self.counter += 1
        self.b.history.add((-100123, 0), self.counter, 'assistant', long_answer, time.time())
        request = self.message('бот, ещё', reply_to_message={'message_id': self.counter, 'from': {'id': 42}, 'text': long_answer})
        self.b.handle(request)
        lines = self.b.history.context((-100123, 0), request['message_id'], 42)
        self.assertEqual(lines[-2:], ['Рофл:\n' + long_answer, 'User 7 → Рофл: бот, ещё'])
        self.b.handle(self.message('', sticker={'emoji': '🤡'}))
        self.assertEqual(self.b.history.context((-100123, 0))[-1], 'User 7: [стикер 🤡]')

    def test_reply_chains_share_chat_context_outside_forum_topics(self):
        text = ('@oracle_test_bot Укажи в этой задаче после какого пункта @andrew_k в туза таранить.. '
                'А еще ему завтра в другое облако переезжать.')
        self.counter = 10  # the replied-to post is older than the messages below
        self.b.handle(self.message(text, message_thread_id=338899,
                                   reply_to_message={'message_id': 1, 'from': {'id': 9, 'first_name': 'Олег'}, 'text': 'задача'}))
        self.b.generate.assert_not_called()
        self.b.handle(self.message('бот, что там про облако?', message_thread_id=338943))
        self.assertIn('сверху:\n\nОлег: задача\n\nUser 7 → Олег: @oracle_test_bot Укажи', self.prompt())
        self.assertNotIn('message_thread_id', self.b.api.call_args.kwargs)
        self.b.handle(self.message('бот, а в топике?', message_thread_id=77, is_topic_message=True))
        self.assertIn('сверху:\n\nUser 7: бот, а в топике?\n\nПоследнее', self.prompt())
        self.assertEqual(self.b.api.call_args.kwargs['message_thread_id'], 77)

    def test_name_includes_username_so_mentions_match_people(self):
        self.b.handle(self.message('бот, @andrew_k уже тут?'))
        self.b.handle(self.message('ПОШЁЛ НАХУЙ!!!', user=8, **{'from': {'id': 8, 'first_name': 'Andrew', 'username': 'andrew_k'}}))
        self.b.handle(self.message('бот, что с @andrew_k и @nobody_known? @test_bot'))
        prompt = self.prompt()
        self.assertIn('User 7: бот, Andrew уже тут?', prompt)
        self.assertIn('\n\nAndrew: ПОШЁЛ НАХУЙ!!!\n\n', prompt)
        self.assertIn('что с Andrew и @nobody_known? Рофл', prompt)

    def test_named_mention_in_caption(self):
        self.b.handle(self.message('', caption='Рофлик, что думаешь?',
                      caption_entities=[{'type': 'text_mention', 'user': {'id': 42}}]))
        self.b.generate.assert_called_once()

    def test_reply_to_me_with_other_bot_mention_is_ignored(self):
        self.b.handle(self.message('@oracle_test_bot а ты че думаешь?',
                                   reply_to_message={'from': {'id': 42}, 'text': 'Мой старый ответ'}))
        self.b.generate.assert_not_called()
        self.b.handle(self.message('@test_bot и @oracle_test_bot ваше мнение?',
                                   reply_to_message={'from': {'id': 42}, 'text': 'Мой старый ответ'}))
        self.b.generate.assert_called_once()

    def test_persistence_topics_reset_and_limits(self):
        for i in range(220):
            self.store(i, f'реплика {i}')
        self.store(500, 'другой топик', topic=5)
        self.b.history.stamp('offset', 1234)
        self.b.history.db.close()
        self.b.history = History(self.path)
        self.assertEqual(self.b.history.stamp('offset'), 1234)
        context = self.b.history.context((-100123, 0))
        self.assertEqual(len(context), 40)
        self.assertEqual(context[-1], 'User 9: реплика 219')
        self.b.handle(self.message('/reset'))
        self.assertEqual(self.b.history.context((-100123, 0)), [])
        self.assertEqual(len(self.b.history.context((-100123, 5))), 1)
        for i in range(600, 630):
            self.store(i, 'x' * 2000, topic=5)
        self.assertLessEqual(sum(map(len, self.b.history.context((-100123, 5)))), 12000)
        self.store(700, 'y' * 6500, topic=5)
        self.assertEqual(self.b.history.context((-100123, 5))[-1], 'User 9:\n' + 'y' * 6500)  # whole, over the limit

    def test_context_is_reply_chain_plus_the_last_lines(self):
        oracle = {'message_id': 1, 'from': {'id': 99, 'first_name': 'oracle'}, 'text': 'ТЗ: кеш-трейс'}
        alex = self.message('Укажи после какого пункта @andrew_k таранить', reply_to_message=oracle)
        self.b.handle(alex)
        for i in range(30):
            self.b.handle(self.message(f'флуд {i}', user=9))
        andrew = self.message('ПОШЁЛ НАХУЙ!!!', user=8, reply_to_message={**alex, 'reply_to_message': None})
        self.b.handle(andrew)
        for i in range(30, 60):
            self.b.handle(self.message(f'флуд {i}', user=9))
        self.b.handle(self.message('рофл, рассуди', reply_to_message=andrew))
        prompt = self.prompt()
        self.assertTrue(prompt.index('кеш-трейс') < prompt.index('ПОШЁЛ НАХУЙ') < prompt.index('рофл, рассуди'))
        self.assertIn('User 7 → User 8 «ПОШЁЛ НАХУЙ!!!»: рофл, рассуди', prompt)
        self.assertNotIn('флуд 21', prompt)
        self.assertEqual(prompt.count('флуд '), 38)
        answer_id = self.counter
        self.b.handle(self.message('бот, ещё', reply_to_message={'message_id': answer_id, 'from': {'id': 42}, 'text': 'Согласен'}))
        prompt = self.prompt()
        self.assertIn('Рофл → User 7: Согласен\n\nUser 7 → Рофл: бот, ещё', prompt)
        self.assertIn('кеш-трейс', prompt)

    def test_background_is_six_hours_and_the_chain_is_however_old(self):
        self.store(1, 'качаю инженерку в вове', date=time.time() - 9 * 3600, name='Алексей')
        self.b.history.add((-100123, 0), 2, 'assistant', 'Инженерка для крыс 🐀', time.time() - 9 * 3600, 1)
        self.store(3, 'вчерашняя задача на пять пунктов', date=time.time() - 7 * 3600, name='oracle')
        self.store(4, 'Нормально кринжа насрал', date=time.time() - 3600, name='Kirill')
        self.counter = 10
        self.b.handle(self.message('бот, что скажешь в своё оправдание?',
                                   reply_to_message={'message_id': 2, 'from': {'id': 42}, 'text': 'Инженерка для крыс 🐀'}))
        self.assertIn('сверху:\n\nАлексей: качаю инженерку в вове\n\nРофл → Алексей: Инженерка для крыс 🐀\n\n'
                      'Kirill: Нормально кринжа насрал\n\nUser 7 → Рофл «Инженерка для крыс 🐀»: бот, что скажешь', self.prompt())
        self.assertNotIn('вчерашняя задача', self.prompt())  # older than six hours and not in the chain
        self.b.handle(self.message('бот, а ты?'))
        self.assertNotIn('качаю инженерку', self.prompt())  # the old chain stays with the request it belonged to

    def test_post_turns_failures_into_runtime_errors(self):
        from io import BytesIO
        from bot import post
        with patch('bot.build_opener') as opener:
            opener.return_value.open.return_value = BytesIO(b'invalid json')
            with self.assertRaises(RuntimeError):
                post('http://localhost/api/chat', {}, direct=True)

    def test_typing_runs_while_the_model_is_busy_and_its_failures_are_harmless(self):
        from threading import Event
        attempted = Event()
        def slow_post(url, payload, **kwargs):
            if url.endswith('sendChatAction'):
                attempted.set()
                raise RuntimeError('unavailable')
            self.assertTrue(attempted.wait(1), 'No typing while the model is busy')
            return {'message': {'content': 'ok'}}
        self.b.ollama_url, self.b.model, self.b.telegram_url = 'http://localhost:11434', 'test', 'https://api/bot/'
        with patch('bot.post', side_effect=slow_post), self.assertLogs('bot', level='WARNING'):
            self.assertEqual(Bot.generate(self.b, [], chat_id=-100123), 'ok')


if __name__ == '__main__':
    unittest.main()
