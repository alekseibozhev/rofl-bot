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

    def test_tool_cache_reuses_search_and_url_skips_errors_and_expires(self):
        msg = {'chat': {'id': -100, 'type': 'supergroup'}, 'message_id': 1, 'date': int(time.time()),
               'from': {'id': 7, 'first_name': 'Вася'}, 'text': 'рофл, проверь'}
        good_search = {'results': [{'title': 'Терентьев', 'url': 'https://example.org/m', 'snippet': 'С какими деньгами?'}]}
        with patch('retrieval.search', return_value=good_search) as search:
            r1 = self.b.execute_tool('web_search', {'query': '«Завяжи лямку!»'}, msg, 1, int(time.time()))
            r2 = self.b.execute_tool('web_search', {'query': '  завяжи   лямку  '}, msg, 1, int(time.time()))
        self.assertEqual(r1, r2)
        self.assertEqual(search.call_count, 1)

        # Errors and empty results are not cached
        with patch('retrieval.search', return_value={'results': [], 'error': 'rate limited'}) as bad:
            self.b.execute_tool('web_search', {'query': 'пусто'}, msg, 1, int(time.time()))
            self.b.execute_tool('web_search', {'query': 'пусто'}, msg, 1, int(time.time()))
        self.assertEqual(bad.call_count, 2)

        # read_url is cached by URL
        with patch('retrieval.read_url', return_value={'title': 'T', 'url': 'https://example.org/p', 'text': 'текст'}) as reader:
            u1 = self.b.execute_tool('read_url', {'url': 'https://example.org/p'}, msg, 1, int(time.time()))
            u2 = self.b.execute_tool('read_url', {'url': 'https://example.org/p'}, msg, 1, int(time.time()))
        self.assertEqual(u1, u2)
        self.assertEqual(reader.call_count, 1)

        # TTL expiry and size cap
        self.b.history.db.execute('DELETE FROM tool_cache')
        self.b.history.cache_put('web_search', 'old', 'v1', ttl=10, now=100, limit=2)
        self.assertEqual(self.b.history.cache_get('web_search', 'old', now=105), 'v1')
        self.assertIsNone(self.b.history.cache_get('web_search', 'old', now=111))
        self.b.history.cache_put('web_search', 'a', 'va', ttl=100, now=200, limit=2)
        self.b.history.cache_put('web_search', 'b', 'vb', ttl=100, now=201, limit=2)
        self.b.history.cache_put('web_search', 'c', 'vc', ttl=100, now=202, limit=2)
        self.assertIsNone(self.b.history.cache_get('web_search', 'a', now=203))
        self.assertEqual(self.b.history.cache_get('web_search', 'c', now=203), 'vc')

    def test_search_chat_matches_russian_inflections_archived_answers_and_keeps_old_messages(self):
        old_ts = int(time.time()) - 60 * 86400  # 60 days old — must NOT be pruned
        self.store(1, 'Старое сообщение два месяца назад про рептилоида', date=old_ts, name='Антон')
        self.store(2, 'Свежее сообщение сегодня', date=int(time.time()) - 10, name='Вася')
        # Record an archived answer not present in messages (e.g. after /reset)
        self.b.history.record((-100, 0), 50, 49, old_ts + 100,
                              'Эндрю - дырявый шершень, Антон - сундучный рептилоид', {})
        msg = {'chat': {'id': -100, 'type': 'supergroup'}, 'message_id': 100, 'date': int(time.time()),
               'from': {'id': 7, 'first_name': 'Вася'}, 'text': 'рофл, поищи'}
        # Inflected query finds archived answer
        out = self.b.execute_tool('search_chat', {'query': 'Шершня Дырявого'}, msg, 100, int(time.time()))
        self.assertIn('дырявый шершень', out)
        # 60-day-old user message is still retained and searchable
        out_old = self.b.execute_tool('search_chat', {'query': 'рептилоид'}, msg, 100, int(time.time()))
        self.assertIn('два месяца назад', out_old)

    def test_final_turn_rejects_stray_tool_calls_instead_of_leaking_preamble(self):
        self.store(1, 'рофл, сравни два топора')
        msg = {'chat': {'id': -100, 'type': 'supergroup'}, 'message_id': 1, 'date': int(time.time()) - 999,
               'from': {'id': 7, 'first_name': 'Вася'}, 'text': 'рофл, сравни два топора'}
        self.b.max_tool_rounds = 2
        self.b.max_tool_calls = 1
        seen_before_final = []

        steps = [
            {'role': 'assistant', 'content': None, 'tool_calls': [tc('c1', 'web_search', {'query': 'топор'})]},
            {'role': 'assistant', 'content': 'Нашёл цифры, сейчас добью статы и выдам вердикт.',
             'tool_calls': [tc('c2', 'web_search', {'query': 'ещё топор'})]},
            {'role': 'assistant', 'content': 'Фиолка с Альтерака лучше в одни ворота.'},
        ]
        idx = [0]
        def fake_step(messages, tools=None):
            if tools is None:
                seen_before_final.append((len(messages), messages[-1]['content']))
            res = steps[idx[0]]
            idx[0] += 1
            return res

        self.b.chat_step = Mock(side_effect=fake_step)
        with patch('retrieval.search', return_value={'results': [{'title': 'Axe', 'url': 'https://ex.org', 'snippet': 'dmg'}]}):
            with self.assertLogs('bot', level='WARNING'):
                answer, trace = self.b.agent_reply(msg, 1, live=False)
        self.assertEqual(answer, 'Фиолка с Альтерака лучше в одни ворота.')
        self.assertEqual(trace['rounds'], 3)
        self.assertEqual([t['status'] for t in trace['tools']], ['ok', 'limit'])
        # Before the first tools=None call, the last tool output must already carry the limit note
        self.assertIn('лимит', seen_before_final[0][1].lower())

    def test_image_found_across_multihop_reply_chain_and_stops_after_two_bot_turns(self):
        now = int(time.time())
        photo_msg = {
            'chat': {'id': -100, 'type': 'supergroup'}, 'message_id': 10, 'date': now - 600,
            'from': {'id': 1, 'first_name': 'Антон'},
            'photo': [{'file_id': 'p10', 'width': 800, 'height': 600}], 'caption': 'решили проблему',
        }
        comment_msg = {
            'chat': {'id': -100, 'type': 'supergroup'}, 'message_id': 11, 'date': now - 300,
            'from': {'id': 2, 'first_name': 'Петя'},
            'reply_to_message': {'message_id': 10, 'from': {'id': 1, 'first_name': 'Антон'}, 'caption': 'решили проблему'},
            'text': 'Мне кажется они на акциях заработали',
        }
        ask_msg = {
            'chat': {'id': -100, 'type': 'supergroup'}, 'message_id': 12, 'date': now - 10,
            'from': {'id': 2, 'first_name': 'Петя'},
            'reply_to_message': {'message_id': 11, 'from': {'id': 2, 'first_name': 'Петя'}, 'text': comment_msg['text']},
            'text': '@rofl_bot, что думаешь?',
        }
        for m in (photo_msg, comment_msg, ask_msg):
            self.b.history.add_message((-100, 0), m)
        self.b.image = Mock(side_effect=lambda m: f"data:image/jpeg;base64,{m['photo'][0]['file_id']}" if m.get('photo') else None)
        self.b.chat_step = Mock(return_value={'role': 'assistant', 'content': 'Вижу фотку Хуанга'})

        answer, trace = self.b.agent_reply(ask_msg, 12, live=False)
        self.assertEqual(answer, 'Вижу фотку Хуанга')
        self.assertEqual(trace['images'], [10])
        last_content = self.b.chat_step.call_args.args[0][-1]['content']
        self.assertIsInstance(last_content, list)
        self.assertIn('#10', last_content[0]['text'])
        self.assertEqual(last_content[1]['image_url']['url'], 'data:image/jpeg;base64,p10')

        # 1 bot reply + user follow-up still sees the photo
        self.b.history.add((-100, 0), 13, 'assistant', 'Вижу фотку Хуанга', now - 5, 12)
        followup1 = {
            'chat': {'id': -100, 'type': 'supergroup'}, 'message_id': 14, 'date': now - 3,
            'from': {'id': 2, 'first_name': 'Петя'},
            'reply_to_message': {'message_id': 13, 'from': {'id': 42, 'first_name': 'Рофл'}, 'text': 'Вижу фотку Хуанга'},
            'text': 'а что в углу написано?',
        }
        self.b.history.add_message((-100, 0), followup1)
        _, trace1 = self.b.agent_reply(followup1, 14, live=False)
        self.assertEqual(trace1['images'], [10])

        # 2 bot replies later -> stops dragging the old photo
        self.b.history.add((-100, 0), 15, 'assistant', 'Там дата', now - 2, 14)
        followup2 = {
            'chat': {'id': -100, 'type': 'supergroup'}, 'message_id': 16, 'date': now - 1,
            'from': {'id': 2, 'first_name': 'Петя'},
            'reply_to_message': {'message_id': 15, 'from': {'id': 42, 'first_name': 'Рофл'}, 'text': 'Там дата'},
            'text': 'ладно, проехали',
        }
        self.b.history.add_message((-100, 0), followup2)
        _, trace2 = self.b.agent_reply(followup2, 16, live=False)
        self.assertEqual(trace2['images'], [])

    def test_recent_unanswered_photo_and_sticker_reply_and_album_images(self):
        now = int(time.time())
        # Recent photo sent right before a non-reply mention is attached
        p20 = {'chat': {'id': -100, 'type': 'supergroup'}, 'message_id': 20, 'date': now - 10,
               'from': {'id': 7, 'first_name': 'Вася'}, 'photo': [{'file_id': 'p20', 'width': 500, 'height': 500}]}
        q21 = {'chat': {'id': -100, 'type': 'supergroup'}, 'message_id': 21, 'date': now - 5,
               'from': {'id': 7, 'first_name': 'Вася'}, 'text': '@rofl_bot кто это?'}
        self.b.history.add_message((-100, 0), p20)
        self.b.history.add_message((-100, 0), q21)
        self.b.image = Mock(side_effect=lambda m: 'data:image/jpeg;base64,' + (
            m['photo'][0]['file_id'] if m.get('photo') else m['sticker']['file_id'])
            if (m.get('photo') or m.get('sticker')) else None)
        self.b.chat_step = Mock(return_value={'role': 'assistant', 'content': 'Это альянсер'})

        _, trace = self.b.agent_reply(q21, 21, live=False)
        self.assertEqual(trace['images'], [20])

        # Replying to a different text-only message must NOT pull in unrelated photo #20
        t22 = {'chat': {'id': -100, 'type': 'supergroup'}, 'message_id': 22, 'date': now - 4,
               'from': {'id': 8, 'first_name': 'Олег'}, 'text': 'когда рейд?'}
        q23 = {'chat': {'id': -100, 'type': 'supergroup'}, 'message_id': 23, 'date': now - 3,
               'from': {'id': 9, 'first_name': 'Иван'},
               'reply_to_message': {'message_id': 22, 'from': {'id': 8, 'first_name': 'Олег'}, 'text': 'когда рейд?'},
               'text': '@rofl_bot ответь ему'}
        self.b.history.add_message((-100, 0), t22)
        self.b.history.add_message((-100, 0), q23)
        _, trace_unrelated = self.b.agent_reply(q23, 23, live=False)
        self.assertEqual(trace_unrelated['images'], [])

        # Reply to a sticker attaches the sticker image
        q24 = {'chat': {'id': -100, 'type': 'supergroup'}, 'message_id': 24, 'date': now - 2,
               'from': {'id': 9, 'first_name': 'Иван'},
               'reply_to_message': {'message_id': 19, 'from': {'id': 8, 'first_name': 'Олег'},
                                    'sticker': {'file_id': 'st19', 'emoji': '😂'}},
               'text': '@rofl_bot что за стикер?'}
        self.b.receive(q24, received_at=now)
        # Captionless sticker reply parent should be stored in history by receive()
        self.assertIsNotNone(self.b.history.db.execute('SELECT 1 FROM messages WHERE id=19').fetchone())
        _, trace_sticker = self.b.agent_reply(q24, 24, live=False)
        self.assertEqual(trace_sticker['images'], [19])

        # Multi-photo album attaches all photos (up to cap)
        a1 = {'chat': {'id': -100, 'type': 'supergroup'}, 'message_id': 30, 'date': now,
              'from': {'id': 7, 'first_name': 'Вася'}, 'caption': '@rofl_bot сравни',
              'photo': [{'file_id': 'a1', 'width': 400, 'height': 400}]}
        a2 = {'chat': {'id': -100, 'type': 'supergroup'}, 'message_id': 31, 'date': now,
              'from': {'id': 7, 'first_name': 'Вася'},
              'photo': [{'file_id': 'a2', 'width': 400, 'height': 400}]}
        self.b.history.add_message((-100, 0), a1)
        self.b.history.add_message((-100, 0), a2)
        _, trace_album = self.b.agent_reply(a1, 31, live=False, extra=(a2,))
        self.assertEqual(trace_album['images'], [30, 31])
        last_content = self.b.chat_step.call_args.args[0][-1]['content']
        self.assertEqual(len(last_content), 3)  # text + 2 images

        # External reply with photo uses local message_id (not foreign chat message_id);
        # text-only external reply does not pull unrelated prior photo from the chat.
        ext_photo = {'chat': {'id': -100, 'type': 'supergroup'}, 'message_id': 40, 'date': now + 1,
                     'from': {'id': 7, 'first_name': 'Вася'}, 'text': '@rofl_bot глянь',
                     'external_reply': {'message_id': 9999,
                                        'photo': [{'file_id': 'ext40', 'width': 400, 'height': 400}]}}
        self.b.history.add_message((-100, 0), ext_photo)
        _, trace_ext = self.b.agent_reply(ext_photo, 40, live=False)
        self.assertEqual(trace_ext['images'], [40])
        self.assertNotIn('#9999', self.b.chat_step.call_args.args[0][-1]['content'][0]['text'])

        ext_text = {'chat': {'id': -100, 'type': 'supergroup'}, 'message_id': 41, 'date': now + 2,
                    'from': {'id': 7, 'first_name': 'Вася'}, 'text': '@rofl_bot что думаешь?',
                    'external_reply': {'message_id': 8888}}
        self.b.history.add_message((-100, 0), ext_text)
        _, trace_ext_text = self.b.agent_reply(ext_text, 41, live=False)
        self.assertEqual(trace_ext_text['images'], [])


if __name__ == '__main__':
    unittest.main()
