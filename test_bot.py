import datetime
from io import BytesIO
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import bot
from bot import Bot, History


class BotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'history.sqlite3'
        self.b = Bot.__new__(Bot)
        self.b.allowed_groups = {-100123}
        self.b.me = {'id': 42, 'username': 'test_bot'}
        self.b.history = History(self.path)
        self.b.openrouter = True
        self.b.openrouter_key = 'sk-or-test'
        self.b.model = 'qwen/qwen4-max'
        self.b.effort = 'none'
        self.b.chat_step = Mock(return_value={'role': 'assistant', 'content': 'Согласен'})
        self.counter = 0
        self.b.api = Mock(side_effect=self.send)
        self.b.send_typing = Mock()

    def tearDown(self):
        self.b.history.db.close()
        self.tmp.cleanup()

    def send(self, method, **kwargs):
        self.counter += 1
        return {'message_id': self.counter, 'date': int(time.time())}

    def prompt(self):
        return '\n\n'.join(bot.text_of(m['content']) for m in self.b.chat_step.call_args.args[0])

    def store(self, message_id, text, topic=0, date=None, name='User 9', reply_to=None):
        msg = {'message_id': message_id, 'date': date or time.time(),
               'from': {'id': 9, 'first_name': name}, 'text': text}
        if reply_to is not None:
            msg['reply_to_message'] = {'message_id': reply_to, 'from': {'id': 9, 'first_name': name}, 'text': 'prev'}
        self.b.history.add_message((-100123, topic), msg)

    def message(self, text, user=7, **fields):
        self.counter += 1
        return dict({'chat': {'id': -100123, 'type': 'supergroup'},
                     'message_id': self.counter, 'date': int(time.time()),
                     'from': {'id': user, 'first_name': f'User {user}'}, 'text': text}, **fields)

    def at(self, hour, minute=0, days=0):
        moment = datetime.datetime.combine(datetime.date.today() + datetime.timedelta(days), datetime.time(hour, minute))
        return moment.timestamp()

    def test_empty_ollama_answer_is_error_and_diagnostics_logged(self):
        self.b.openrouter = False
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

    def test_chat_step_uses_openrouter_without_repetition_penalty_and_falls_back_on_400(self):
        self.b.chat_step = Bot.chat_step.__get__(self.b)
        good = {'choices': [{'message': {'content': 'Здарова'}, 'finish_reason': 'stop'}],
                'usage': {'prompt_tokens': 12, 'completion_tokens': 3, 'cost': 0.00001}}
        with patch('bot.post', side_effect=[RuntimeError('HTTP 400'), good, good]) as post, self.assertLogs('bot'):
            r1 = self.b.chat_step([{'role': 'user', 'content': 'Привет'}])
            r2 = self.b.chat_step([{'role': 'user', 'content': 'Привет'}])
        self.assertEqual(r1['content'], 'Здарова')
        self.assertEqual(r2['content'], 'Здарова')
        for call_args in post.call_args_list:
            self.assertNotIn('repetition_penalty', call_args.args[1])
        self.assertEqual([c.args[1]['reasoning'] for c in post.call_args_list],
                         [{'effort': 'none'}, {'effort': 'minimal'}, {'effort': 'minimal'}])

    def test_background_messages_are_stored_without_calling_model(self):
        self.b.handle(self.message('Встречаемся в пятницу', user=8))
        self.b.handle(self.message('ок, договорились', user=9))
        self.b.chat_step.assert_not_called()
        self.b.api.assert_not_called()
        self.b.handle(self.message('рофл, когда встречаемся?'))
        self.b.chat_step.assert_called_once()
        self.assertIn('пятницу', self.prompt())

    def test_read_only_stores_but_never_sends(self):
        self.b.read_only = True
        self.b.handle(self.message('Встречаемся в пятницу', user=8))
        self.b.handle(self.message('рофл, когда встречаемся?'))
        self.b.handle(self.message('/reset'))
        self.b.chat_step.assert_not_called()
        self.b.api.assert_not_called()
        self.b.read_only = False
        self.b.handle(self.message('бот, ну так когда?'))
        self.assertIn('пятницу', self.prompt())

    def test_untag_cleans_legacy_prefixed_answers_once(self):
        key, line = (-100123, 0), '@antoha_gs @oxgrb\n'
        for id_, content, trace in ((2, line + 'подъём', {'tagged': ['antoha_gs', 'oxgrb']}),
                                    (4, '@oxgrb\nне тот', {'tagged': ['antoha_gs']}),
                                    (6, '@antoha_gs\nсам', None)):
            self.b.history.add(key, id_, 'assistant', content, time.time(), id_ - 1)
            self.b.history.record(key, id_, id_ - 1, time.time(), content, {}, trace)
        self.assertEqual(self.b.history.untag(), 2)
        self.assertEqual(self.b.history.untag(), 0)

    def test_collect_polls_until_quiet_and_keeps_what_arrived_on_failure(self):
        first, late = [{'update_id': 1}], [{'update_id': 2}]
        self.b.poll = Mock(side_effect=[late, []])
        self.assertEqual(self.b.collect(list(first)), first + late)
        self.b.poll = Mock(side_effect=RuntimeError('Сеть недоступна'))
        with self.assertLogs('bot', level='ERROR'):
            self.assertEqual(self.b.collect(list(first)), first)

    def test_external_reply_with_and_without_quote(self):
        external = {'origin': {'type': 'channel', 'chat': {'id': -555, 'title': 'News'}}, 'message_id': 10}
        self.b.handle(self.message('@test_bot видел?', external_reply=external, quote={'text': 'Фрагмент новости'}))
        self.assertIn('Фрагмент новости', self.prompt())
        self.b.handle(self.message('@test_bot что думаешь?', external_reply=external))
        self.assertIn('Telegram не передал', self.prompt())

    def test_persistence_topics_reset_and_window_limits(self):
        for i in range(1, 50):
            self.store(i, f'реплика {i}')
        self.store(500, 'другой топик', topic=5)
        self.b.history.stamp('offset', 1234)
        self.b.history.db.close()
        self.b.history = History(self.path)
        self.assertEqual(self.b.history.stamp('offset'), 1234)
        turns = self.b.history.recent_window((-100123, 0), limit=20)
        self.assertEqual(len(turns), 20)
        self.assertEqual(turns[-1]['text'], 'реплика 49')
        self.b.handle(self.message('/reset'))
        self.assertEqual(self.b.history.recent_window((-100123, 0)), [])
        self.assertEqual(len(self.b.history.recent_window((-100123, 5))), 1)

    def test_window_keeps_old_reply_chain_and_drops_unrelated_old_messages(self):
        self.store(1, 'качаю инженерку в вове', date=self.at(14, days=-2), name='Алексей')
        self.b.history.add((-100123, 0), 2, 'assistant', 'Инженерка для крыс', self.at(14, days=-2), 1)
        self.store(3, 'старая несвязанная реплика', date=self.at(19, days=-2), name='Кирилл')
        self.store(4, 'сегодняшний фон', date=self.at(19), name='Кирилл')
        self.counter = 10
        self.b.handle(self.message('бот, что скажешь?', date=self.at(20),
                                   reply_to_message={'message_id': 2, 'from': {'id': 42}, 'text': 'Инженерка для крыс'}))
        prompt = self.prompt()
        self.assertIn('качаю инженерку в вове', prompt)
        self.assertIn('Инженерка для крыс', prompt)
        self.assertIn('сегодняшний фон', prompt)
        self.assertNotIn('старая несвязанная реплика', prompt)

    def test_post_turns_failures_into_runtime_errors(self):
        with patch('bot.build_opener') as opener:
            opener.return_value.open.return_value = BytesIO(b'invalid json')
            with self.assertRaises(RuntimeError):
                bot.post('http://localhost/api/chat', {}, direct=True)

    def test_typing_runs_while_model_is_busy_and_failures_are_harmless(self):
        self.b.send_typing = Bot.send_typing.__get__(self.b)
        from threading import Event
        attempted = Event()
        def slow_post(url, payload, **kwargs):
            if url.endswith('sendChatAction'):
                attempted.set()
                raise RuntimeError('unavailable')
            self.assertTrue(attempted.wait(1), 'No typing while the model is busy')
            return {'message': {'content': 'ok'}}
        self.b.openrouter = False
        self.b.ollama_url, self.b.model, self.b.telegram_url = 'http://localhost:11434', 'test', 'https://api/bot/'
        with patch('bot.post', side_effect=slow_post), self.assertLogs('bot', level='WARNING'):
            self.assertEqual(Bot.generate(self.b, [], chat_id=-100123), 'ok')


class TelegramFormattingTests(unittest.TestCase):
    def test_plain_chat_is_unchanged(self):
        self.assertIsNone(bot.telegram_html('ну ты <гений> & математик 2 * 2'))

    def test_news_bold_and_links(self):
        text = '**Новости**\n[Первый](https://example.com/a_(b)) [Второй](https://example.org)'
        self.assertEqual(bot.telegram_html(text),
                         '<b>Новости</b>\n<a href="https://example.com/a_(b)">Первый</a> '
                         '<a href="https://example.org">Второй</a>')

    def test_code_escapes_html_and_rejects_javascript_urls(self):
        text = '**Код**\n```python\nprint("**x** < y & z")\n```\n`__inline__ <b>`'
        self.assertEqual(bot.telegram_html(text),
                         '<b>Код</b>\n<pre><code class="language-python">print("**x** &lt; y &amp; z")</code></pre>'
                         '\n<code>__inline__ &lt;b&gt;</code>')
        self.assertIsNone(bot.telegram_html('[опасно](javascript:alert(1))'))

    def test_clean_prefix_strips_leaked_header(self):
        self.assertEqual(bot.clean_prefix('[12] Рофл: ну привет'), 'ну привет')
        self.assertEqual(bot.clean_prefix('Рофл → Вася: сам дурак'), 'сам дурак')
        self.assertEqual(bot.clean_prefix('обычный текст'), 'обычный текст')


if __name__ == '__main__':
    unittest.main()
