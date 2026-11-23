import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import bot


def tc(call_id, name, args):
    return {'id': call_id, 'type': 'function',
            'function': {'name': name, 'arguments': json.dumps(args, ensure_ascii=False)}}


class AgentLoopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'history.sqlite3'
        self.b = bot.Bot.__new__(bot.Bot)
        self.b.history = bot.History(self.path)
        self.addCleanup(lambda: self.b.history.db.close())
        self.b.allowed_groups = {-100}
        self.b.me = {'id': 42, 'username': 'rofl_bot'}
        self.b.openrouter = True
        self.b.openrouter_key = 'sk-test'
        self.b.model = 'qwen/qwen4-max'
        self.b.effort = 'none'
        self.b.send_typing = Mock()
        self.b.api = Mock(return_value={'message_id': 999, 'date': int(time.time())})

    def store(self, mid, text, date=None, name='Вася', topic=0):
        self.b.history.add_message((-100, topic), {
            'message_id': mid, 'date': date or (int(time.time()) - 1000 + mid),
            'from': {'id': 7, 'first_name': name}, 'text': text,
        })

    def test_direct_answer_takes_single_call_without_tools(self):
        self.store(1, 'рофл, ты тут?')
        msg = {'chat': {'id': -100, 'type': 'supergroup'}, 'message_id': 1, 'date': int(time.time()) - 999,
               'from': {'id': 7, 'first_name': 'Вася'}, 'text': 'рофл, ты тут?'}
        self.b.chat_step = Mock(return_value={'role': 'assistant', 'content': 'Рофл: на месте, чё надо'})
        answer, trace = self.b.agent_reply(msg, 1, live=False)
        self.assertEqual(answer, 'на месте, чё надо')
        self.assertEqual(trace['rounds'], 1)
        self.assertEqual(trace['tools'], [])
        self.assertEqual(self.b.chat_step.call_count, 1)

    def test_read_chat_paginates_backward_and_never_peeks_into_future(self):
        for i in range(1, 45):
            self.store(i, f'сообщение номер {i}')
        msg = {'chat': {'id': -100, 'type': 'supergroup'}, 'message_id': 35, 'date': int(time.time()) - 965,
               'from': {'id': 7, 'first_name': 'Вася'}, 'text': 'рофл, о чём говорили в самом начале?'}
        seen_tool_outputs = []

        calls = [0]
        def fake_step(messages, tools=None):
            calls[0] += 1
            if calls[0] == 1:
                return {'role': 'assistant', 'content': None,
                        'tool_calls': [tc('c1', 'read_chat', {'before_id': 16, 'limit': 10})]}
            seen_tool_outputs.append(messages[-1]['content'])
            return {'role': 'assistant', 'content': 'В начале были сообщения 6..15'}

        self.b.chat_step = Mock(side_effect=fake_step)
        answer, trace = self.b.agent_reply(msg, 35, live=False)
        self.assertIn('6..15', answer)
        self.assertEqual(trace['rounds'], 2)
        self.assertEqual(trace['tools'][0]['tool'], 'read_chat')
        # Even if model asks for before_id=999, read_before is clamped to through=35 (no future #36..#44).
        future_check = self.b.execute_tool('read_chat', {'before_id': 999, 'limit': 5}, msg, 35, int(time.time()) - 965)
        self.assertIn('#34', future_check)
        self.assertNotIn('#35', future_check)
        self.assertNotIn('#36', future_check)

    def test_search_chat_finds_older_topic_in_same_chat(self):
        self.store(1, 'Вчера обсуждали покупку видеокарты RTX 5090', name='Олег')
        self.store(2, 'А сегодня просто флудим', name='Вася')
        self.store(3, 'рофл, про какую видюху говорил Олег?', name='Вася')
        msg = {'chat': {'id': -100, 'type': 'supergroup'}, 'message_id': 3, 'date': int(time.time()) - 997,
               'from': {'id': 7, 'first_name': 'Вася'}, 'text': 'рофл, про какую видюху говорил Олег?'}
        out = self.b.execute_tool('search_chat', {'query': 'видеокарты'}, msg, 3, int(time.time()) - 997)
        self.assertIn('RTX 5090', out)
        self.assertNotIn('#3', out)
        partial = self.b.execute_tool('search_chat', {'query': 'видеокарты капитал'}, msg, 3, int(time.time()) - 997)
        self.assertIn('капитал', partial)
        self.assertIn('web_search', partial)
        miss = self.b.execute_tool('search_chat', {'query': 'несуществующее'}, msg, 3, int(time.time()) - 997)
        self.assertIn('web_search', miss)

    def test_web_search_then_read_url_multi_round_chain(self):
        self.store(1, 'рофл, найди и прочитай про пример')
        msg = {'chat': {'id': -100, 'type': 'supergroup'}, 'message_id': 1, 'date': int(time.time()) - 999,
               'from': {'id': 7, 'first_name': 'Вася'}, 'text': 'рофл, найди и прочитай про пример'}
        steps = [
            {'role': 'assistant', 'content': None, 'tool_calls': [tc('c1', 'web_search', {'query': 'пример'})]},
            {'role': 'assistant', 'content': None, 'tool_calls': [tc('c2', 'read_url', {'url': 'https://example.org/p'})]},
            {'role': 'assistant', 'content': 'На странице написано: секретный факт.'},
        ]
        self.b.chat_step = Mock(side_effect=steps)
        with patch('retrieval.search', return_value={'results': [{'title': 'Ex', 'url': 'https://example.org/p', 'snippet': 'short'}]}), \
             patch('retrieval.read_url', return_value={'title': 'Ex', 'url': 'https://example.org/p', 'text': 'секретный факт'}):
            answer, trace = self.b.agent_reply(msg, 1, live=False)
        self.assertIn('секретный факт', answer)
        self.assertEqual([t['tool'] for t in trace['tools']], ['web_search', 'read_url'])
        self.assertEqual(trace['rounds'], 3)

    def test_news_tool_works_with_and_without_query(self):
        msg = {'chat': {'id': -100, 'type': 'supergroup'}, 'message_id': 1, 'date': int(time.time()) - 999,
               'from': {'id': 7, 'first_name': 'Вася'}, 'text': 'рофл, чё по новостям?'}
        with patch('bot.headlines', return_value=[('Заголовок дня', 'https://news.example/1')]) as hl:
            out_q = self.b.execute_tool('news', {'query': 'игры'}, msg, 1, int(time.time()) - 999)
            out_all = self.b.execute_tool('news', {}, msg, 1, int(time.time()) - 999)
        self.assertIn('Заголовок дня', out_q)
        self.assertIn('Заголовок дня', out_all)
        self.assertEqual(hl.call_count, 3)  # 1 with query + 2 without (world + politics)

    def test_duplicate_calls_and_limits_and_bad_args_are_guarded(self):
        self.store(1, 'рофл, проверь')
        msg = {'chat': {'id': -100, 'type': 'supergroup'}, 'message_id': 1, 'date': int(time.time()) - 999,
               'from': {'id': 7, 'first_name': 'Вася'}, 'text': 'рофл, проверь'}
        self.b.max_tool_rounds = 2
        self.b.max_tool_calls = 2
        steps = [
            {'role': 'assistant', 'content': None, 'tool_calls': [
                tc('c1', 'web_search', {'query': 'abc'}),
                tc('c2', 'web_search', {'query': 'abc'}),  # duplicate
                {'id': 'c3', 'type': 'function', 'function': {'name': 'web_search', 'arguments': '{broken'}},
                tc('c4', 'shell_exec', {'cmd': 'rm -rf /'}),  # unknown tool
            ]},
            {'role': 'assistant', 'content': None, 'tool_calls': [
                tc('c5', 'web_search', {'query': 'def'}),
                tc('c6', 'web_search', {'query': 'ghi'}),  # exceeds max_tool_calls=2
            ]},
            {'role': 'assistant', 'content': 'Всё проверил в пределах лимита.'},
        ]
        self.b.chat_step = Mock(side_effect=steps)
        with patch('retrieval.search', return_value={'results': []}) as search:
            answer, trace = self.b.agent_reply(msg, 1, live=False)
        self.assertEqual(answer, 'Всё проверил в пределах лимита.')
        self.assertEqual(search.call_count, 2)
        self.assertEqual([t['status'] for t in trace['tools']],
                         ['ok', 'duplicate', 'bad_args', 'unknown', 'ok', 'limit'])
        # Final call after max_tool_rounds must have tools=None
        self.assertIsNone(self.b.chat_step.call_args_list[-1].kwargs['tools'])

    def test_followup_image_is_attached_to_prompt(self):
        m1 = {'chat': {'id': -100, 'type': 'supergroup'}, 'message_id': 1, 'date': int(time.time()) - 999,
              'from': {'id': 7, 'first_name': 'Вася'}, 'text': 'рофл, глянь'}
        m2 = {'chat': {'id': -100, 'type': 'supergroup'}, 'message_id': 2, 'date': int(time.time()) - 998,
              'from': {'id': 7, 'first_name': 'Вася'}, 'photo': [{'file_id': 'f1', 'width': 100, 'height': 100}]}
        self.b.history.add_message((-100, 0), m1)
        self.b.history.add_message((-100, 0), m2)
        self.b.image = Mock(side_effect=lambda m: 'data:image/jpeg;base64,AA==' if m.get('photo') else None)
        self.b.chat_step = Mock(return_value={'role': 'assistant', 'content': 'Вижу картинку'})
        answer, _ = self.b.agent_reply(m1, 2, live=False, extra=(m2,))
        self.assertEqual(answer, 'Вижу картинку')
        last_content = self.b.chat_step.call_args.args[0][-1]['content']
        self.assertIsInstance(last_content, list)
        self.assertEqual(last_content[1]['image_url']['url'], 'data:image/jpeg;base64,AA==')


if __name__ == '__main__':
    unittest.main()
