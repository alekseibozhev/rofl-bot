import datetime
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

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
        self.b.setup = Mock(return_value='нет')
        self.counter = 0
        self.b.api = Mock(side_effect=self.send)
        self.b.read = Mock()  # no news from the network unless a test asks
        self.b.direct = Mock(side_effect=lambda snapshot, previous: {"action": "reply" if snapshot["explicit"] else "ignore", "target": snapshot["target"], "attitude": 5, "reconsider": False})
        self.b.model = 'deepseek/deepseek-v4.1-flash'

    def tearDown(self):
        self.b.history.db.close()
        self.tmp.cleanup()

    def send(self, method, **kwargs):
        if method == 'getChatMemberCount':
            return 7
        if method == 'getChatAdministrators':
            return [{'user': {'id': 5, 'first_name': 'Roman', 'username': 'dazorb'}, 'status': 'creator'},
                    {'user': {'id': 6, 'first_name': 'oracle', 'username': 'oracle_bot', 'is_bot': True}}]
        self.counter += 1
        return {'message_id': self.counter, 'date': int(time.time())}

    def prompt(self):
        [message] = self.b.generate.call_args.args[0]
        content = message['content']
        return content if isinstance(content, str) else content[0]['text']  # a picture goes after the text

    def store(self, message_id, text, topic=0, date=None, name='User 9'):
        self.b.history.add_message((-100123, topic), {'message_id': message_id, 'date': date or time.time(),
                                                      'from': {'id': 9, 'first_name': name}, 'text': text})

    def message(self, text, user=7, **fields):
        self.counter += 1
        return dict({'chat': {'id': -100123, 'type': 'supergroup'},
                     'message_id': self.counter, 'date': int(time.time()),
                     'from': {'id': user, 'first_name': f'User {user}'}, 'text': text}, **fields)




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


    def test_read_only_stores_but_never_sends(self):
        self.b.read_only = True
        self.b.handle(self.message('Встречаемся в пятницу', user=8))
        self.b.handle(self.message('рофл, когда встречаемся?'))
        self.b.handle(self.message('/reset'))
        self.b.handle(self.message('/help'))
        self.b.generate.assert_not_called()
        self.b.api.assert_not_called()
        context = self.b.history.context((-100123, 0))
        self.assertEqual(len(context), 3)  # a mark of when, then the two
        self.assertIn('пятницу', context[1])
        self.b.read_only = False
        self.b.handle(self.message('бот, ну так когда?'))
        self.assertIn('пятницу', self.prompt())





    def test_the_line_of_at_comes_off_the_answers_kept_with_it_once(self):
        key, line = (-100123, 0), '@antoha_gs @oxgrb\n'
        for id_, content, trace in ((2, line + 'подъём', {'tagged': ['antoha_gs', 'oxgrb']}),
                                    (4, '@oxgrb\nне тот', {'tagged': ['antoha_gs']}),  # not the line the code put
                                    (6, '@antoha_gs\nсам', None)):  # tagged by the model, not by the code
            self.b.history.add(key, id_, 'assistant', content, time.time(), id_ - 1)
            self.b.history.record(key, id_, id_ - 1, time.time(), content, {}, trace)
        self.assertEqual(self.b.history.untag(), 2)  # the answer and the message of #2
        self.assertEqual(self.b.history.untag(), 0)
        self.b.history.db.close()
        self.b.history = History(self.path)  # it runs on start
        for table in ('answers', 'messages'):
            rows = self.b.history.db.execute(f'SELECT content FROM {table} ORDER BY id').fetchall()
            self.assertEqual(rows, [('подъём',), ('@oxgrb\nне тот',), ('@antoha_gs\nсам',)])









    def test_a_model_that_cannot_stop_thinking_thinks_the_least(self):
        self.b.openrouter, self.b.openrouter_key, self.b.model = True, 'sk-or-test', 'meta/muse-spark-1.3'
        good = {'choices': [{'message': {'content': 'Здарова'}, 'finish_reason': 'stop'}], 'usage': {}}
        with patch('bot.post', side_effect=[RuntimeError('HTTP 400'), good, good]) as post, self.assertLogs('bot'):
            self.assertEqual(Bot.generate(self.b, [{'role': 'user', 'content': 'Привет'}]), 'Здарова')
            Bot.generate(self.b, [{'role': 'user', 'content': 'Привет'}])
        reasoning = [call.args[1]['reasoning'] for call in post.call_args_list]
        self.assertEqual(reasoning, [{'enabled': False}, {'effort': 'minimal'}, {'effort': 'minimal'}])
        with patch('bot.post', side_effect=RuntimeError('HTTP 401')), self.assertLogs('bot'):
            with self.assertRaises(RuntimeError):
                Bot.generate(self.b, [])

















    def at(self, hour, minute=0, days=0):
        """Today's `hour`:`minute`, `days` later, as a timestamp."""
        moment = datetime.datetime.combine(datetime.date.today() + datetime.timedelta(days), datetime.time(hour, minute))
        return moment.timestamp()

    def character(self, **attributes):
        return {'Интеллект': 3, 'Психика': 3, 'Физиология': 3, 'Моторика': 3, 'луна': 5, **attributes}



























    def test_collect_polls_until_quiet_and_keeps_what_arrived_on_failure(self):
        first, late = [{'update_id': 1}], [{'update_id': 2}]
        self.b.poll = Mock(side_effect=[late, []])
        self.assertEqual(self.b.collect(list(first)), first + late)
        self.assertEqual(self.b.poll.call_args_list[0].args, (2, self.b.wait))
        self.b.poll = Mock(side_effect=RuntimeError('Сеть недоступна'))
        with self.assertLogs('bot', level='ERROR'):
            self.assertEqual(self.b.collect(list(first)), first)


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





    def test_persistence_topics_reset_and_limits(self):
        for i in range(220):
            self.store(i, f'реплика {i}')
        self.store(500, 'другой топик', topic=5)
        self.b.history.stamp('offset', 1234)
        self.b.history.db.close()
        self.b.history = History(self.path)
        self.assertEqual(self.b.history.stamp('offset'), 1234)
        context = self.b.history.context((-100123, 0))
        self.assertEqual(len(context), 41)  # a mark of when and 40 messages
        self.assertTrue(context[0].startswith('— '))
        self.assertEqual(context[-1], 'User 9: реплика 219')
        self.b.handle(self.message('/reset'))
        self.assertEqual(self.b.history.context((-100123, 0)), [])
        self.assertEqual(len(self.b.history.context((-100123, 5))), 2)  # a mark of when and the message
        for i in range(600, 630):
            self.store(i, 'x' * 2000, topic=5)
        self.assertLessEqual(sum(map(len, self.b.history.context((-100123, 5)))), 12000)
        self.store(700, 'y' * 6500, topic=5)
        self.assertEqual(self.b.history.context((-100123, 5))[-1], 'User 9:\n' + 'y' * 6500)  # whole, over the limit


    def test_background_is_a_day_and_the_chain_is_however_old(self):
        self.store(1, 'качаю инженерку в вове', date=self.at(14, days=-1), name='Алексей')
        self.b.history.add((-100123, 0), 2, 'assistant', 'Инженерка для крыс 🐀', self.at(14, days=-1), 1)
        self.store(3, 'вчерашняя задача на пять пунктов', date=self.at(19, days=-1), name='oracle')
        self.store(4, 'Нормально кринжа насрал', date=self.at(19), name='Kirill')
        self.counter = 10
        self.b.handle(self.message('бот, что скажешь в своё оправдание?', date=self.at(20),
                                   reply_to_message={'message_id': 2, 'from': {'id': 42}, 'text': 'Инженерка для крыс 🐀'}))
        self.assertIn('сверху:\n\n— вчера, 14:00 —\n\nАлексей: качаю инженерку в вове\n\nРофл → Алексей: Инженерка для '
                      'крыс 🐀\n\n— сегодня, 19:00 —\n\nKirill: Нормально кринжа насрал\n\n— сегодня, 20:00 —\n\n'
                      'User 7 → Рофл «Инженерка для крыс 🐀»: бот, что скажешь', self.prompt())
        self.assertNotIn('вчерашняя задача', self.prompt())  # older than a day and not in the chain
        self.b.handle(self.message('бот, а ты?', date=self.at(20, 1)))
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
