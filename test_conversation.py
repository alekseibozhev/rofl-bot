import copy
import datetime
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import bot
import conversation


class ConversationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'history.sqlite3'
        self.b = bot.Bot.__new__(bot.Bot)
        self.b.history = bot.History(self.path)
        self.addCleanup(lambda: self.b.history.db.close())
        self.b.allowed_groups = {-1, -2}
        self.b.me = {'id': 42, 'username': 'test_bot'}
        self.b.openrouter = True
        self.b.openrouter_key = 'test-key'
        self.b.model = 'test-model'
        self.b.api = Mock(return_value={'message_id': 1000, 'date': time.time()})
        self.b.generate = Mock(return_value='Глад Валакас')
        self.b.writer_step = Mock(return_value={'role': 'assistant', 'content': 'Шо?'})
        self.b.read = Mock()
        self.b.direct = Mock(side_effect=self.decision)
        self.message_id = 0

    def decision(self, snapshot, previous):
        return {'action': 'reply', 'target': snapshot['target'], 'attitude': 7, 'reconsider': False, 'addressed': True,
                'jev': {'cost': .001, 'elapsed_s': .1}}

    def message(self, text='бот, привет', chat=-1, topic=0, date=None):
        self.message_id += 1
        message = {'chat': {'id': chat, 'type': 'supergroup'}, 'message_id': self.message_id,
                   'date': date or time.time(), 'text': text, 'from': {'id': 7, 'first_name': 'Алексей'}}
        if topic:
            message.update(message_thread_id=topic, is_topic_message=True)
        return message

    def turn(self, message=None, **kwargs):
        message = message or self.message()
        self.b.history.add_message((message['chat']['id'], bot.topic_of(message)), message)
        return self.b.joke(message, message['message_id'], **kwargs)

    def test_jev_has_no_role_metadata_but_has_shared_conversation(self):
        self.turn()
        snapshot, previous = self.b.direct.call_args.args
        body = conversation.payload(snapshot, {**previous, 'role': 'SECRET_ROLE'})
        self.assertNotIn('SECRET_ROLE', json.dumps(body))
        self.assertEqual(body['state']['context'], snapshot['context'])
        writer = self.b.writer_step.call_args.args[0]
        self.assertIn(snapshot['context'], writer[1]['content'])
        self.assertIn('Глад Валакас', writer[0]['content'])
        self.assertEqual(set(body['questions']['attitude']['criteria']), {str(n) for n in range(11)})

    def test_role_is_open_and_stays_until_director_reconsiders(self):
        self.b.generate.return_value = 'типичная анимешница 200кг'
        self.turn()
        self.turn()
        self.b.generate.assert_called_once()
        self.assertEqual(conversation.Store(self.b.history.db).state((-1, 0))['role'], 'типичная анимешница 200кг')
        self.b.direct.side_effect = lambda s, p: {**self.decision(s, p), 'reconsider': True}
        self.b.generate.return_value = 'Ким Кицураги'
        self.turn()
        self.assertEqual(self.b.generate.call_count, 2)
        self.assertEqual(conversation.Store(self.b.history.db).state((-1, 0))['role'], 'Ким Кицураги')

    def test_reconsider_may_keep_role_and_changed_timestamp(self):
        self.turn()
        old = conversation.Store(self.b.history.db).state((-1, 0))
        self.b.direct.side_effect = lambda s, p: {**self.decision(s, p), 'reconsider': True}
        self.turn()
        self.assertEqual(conversation.Store(self.b.history.db).state((-1, 0))['changed_at'], old['changed_at'])

    def test_state_and_role_are_isolated_by_chat_and_topic(self):
        for message, role in [(self.message(), 'Глад'), (self.message(chat=-2), 'Ким'), (self.message(topic=5), 'Гарри')]:
            self.b.generate.return_value = role
            self.turn(message)
        store = conversation.Store(self.b.history.db)
        self.assertEqual([store.state(k)['role'] for k in [(-1, 0), (-2, 0), (-1, 5)]], ['Глад', 'Ким', 'Гарри'])

    def test_first_ignored_message_does_not_consume_role_selection(self):
        self.b.direct.side_effect = lambda s, p: {**self.decision(s, p), 'action': 'ignore'}
        self.turn()
        self.b.generate.assert_not_called()
        self.b.direct.side_effect = self.decision
        self.turn()
        self.b.generate.assert_called_once()

    def test_persisted_decision_survives_restart_and_reuses_snapshot(self):
        m = self.message()
        self.turn(m)
        self.b.history.db.close()
        self.b.history = bot.History(self.path)
        self.b.history.add_message((-1, 0), self.message('БУДУЩИЙ РАЗГОВОР'))
        self.b.direct.reset_mock()
        self.b.generate.reset_mock()
        self.b.joke(m, m['message_id'])
        self.b.direct.assert_not_called()
        self.b.generate.assert_not_called()
        self.assertNotIn('БУДУЩИЙ РАЗГОВОР', self.b.writer_step.call_args.args[0][1]['content'])

    def test_duplicate_event_sends_once(self):
        m = self.message()
        self.b.handle(m)
        self.b.handle(m)
        self.b.api.assert_called_once()
        self.b.direct.assert_called_once()

    def test_ignore_is_saved_without_writing_or_sending(self):
        self.b.direct.side_effect = lambda s, p: {**self.decision(s, p), 'action': 'ignore'}
        self.b.handle(self.message())
        self.b.writer_step.assert_not_called()
        self.b.api.assert_not_called()
        self.assertEqual(self.b.history.db.execute('SELECT count(*) FROM conversation_decisions').fetchone()[0], 1)

    def test_reaction_is_chosen_by_writer_and_targets_directors_message(self):
        first = self.message('привет')
        self.b.history.add_message((-1, 0), first)
        self.b.direct.side_effect = lambda s, p: {**self.decision(s, p), 'action': 'react', 'target': first['message_id']}
        self.b.writer_step.return_value = {'role': 'assistant', 'content': '😂'}
        self.b.handle(self.message('бот, ор'))
        method, = self.b.api.call_args.args
        self.assertEqual(method, 'setMessageReaction')
        self.assertEqual(self.b.api.call_args.kwargs['message_id'], first['message_id'])
        self.assertEqual(self.b.api.call_args.kwargs['reaction'][0]['emoji'], '🤣')

    def test_jev_outage_answers_explicit_call_and_keeps_state(self):
        self.b.direct.side_effect = RuntimeError('offline')
        self.b.handle(self.message())
        self.b.api.assert_called_once()
        self.assertEqual(conversation.Store(self.b.history.db).state((-1, 0))['attitude'], 5)
        self.b.api.reset_mock()
        self.b.handle(self.message('обычный разговор'))
        self.b.api.assert_not_called()

    def test_role_picker_failure_uses_neutral_and_retries_next_turn(self):
        self.b.generate.side_effect = RuntimeError('offline')
        answer, trace = self.turn()
        self.assertEqual(trace['role'], conversation.NEUTRAL_ROLE)
        self.assertEqual(answer, 'Шо?')
        self.b.generate.side_effect = None
        self.b.generate.return_value = 'Папич'
        _, trace = self.turn()
        self.assertEqual(trace['role'], 'Папич')

    def test_implicit_address_bypasses_interjection_cooldown(self):
        self.b.history.add((-1, 0), 0, 'assistant', 'моя шутка', time.time())
        self.b.handle(self.message('ну ты выдал'))
        self.b.api.assert_called_once()

    def test_background_cannot_bypass_cooldown(self):
        self.b.history.add((-1, 0), 0, 'assistant', 'моя шутка', time.time())
        self.b.direct.side_effect = lambda s, p: {**self.decision(s, p), 'addressed': False}
        self.b.handle(self.message('разговор между людьми'))
        self.b.api.assert_not_called()

    def test_replay_sends_nothing_and_does_not_change_database(self):
        m = self.message()
        self.turn(m)
        before = '\n'.join(self.b.history.db.iterdump())
        self.turn(m, live=False)
        self.assertEqual('\n'.join(self.b.history.db.iterdump()), before)
        self.b.api.assert_not_called()

    def test_uncached_replay_uses_state_before_event(self):
        now = time.time()
        old = self.message(date=now - 100)
        self.b.history.add_message((-1, 0), old)
        self.turn(self.message(date=now))
        self.b.generate.reset_mock()
        self.b.joke(old, old['message_id'], live=False)
        previous = self.b.direct.call_args.args[1]
        self.assertEqual(previous, conversation.Store.initial())

    def test_followups_are_one_turn_with_same_snapshot(self):
        first, second = self.message('@test_bot'), self.message('вот сообщение')
        self.b.handle(first, second)
        self.b.direct.assert_called_once()
        self.b.api.assert_called_once()
        self.assertIn('вот сообщение', self.b.direct.call_args.args[0]['context'])

    def test_reset_only_clears_its_topic_state(self):
        self.turn()
        self.turn(self.message(topic=5))
        self.b.handle(self.message('/reset'))
        store = conversation.Store(self.b.history.db)
        self.assertEqual(store.state((-1, 0)), store.initial())
        self.assertEqual(store.state((-1, 5))['role'], 'Глад Валакас')

    def test_writer_tools_return_data_then_model_composes_answer(self):
        self.b.writer_step.side_effect = [
            {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 't1', 'type': 'function', 'function': {'name': 'news', 'arguments': '{"query":"лампочки"}'}}]},
            {'role': 'assistant', 'content': 'Лампочка требует подписку'}]
        with patch('bot.headlines', return_value=[('Новость', 'https://example.com')]):
            answer, trace = self.turn()
        self.assertEqual(answer, 'Лампочка требует подписку')
        self.assertEqual(trace['tools'][0]['tool'], 'news')
        messages = self.b.writer_step.call_args.args[0]
        self.assertEqual(messages[-1]['role'], 'tool')
        self.assertIn('https://example.com', messages[-1]['content'])

    def test_writer_gets_message_ids_without_repeated_target_excerpts(self):
        for text in ('ПЕРВАЯ УНИКАЛЬНАЯ РЕПЛИКА', 'ВТОРАЯ УНИКАЛЬНАЯ РЕПЛИКА'):
            self.b.history.add_message((-1, 0), self.message(text))
        self.turn(self.message('бот, ответь на последнее'))
        snapshot = self.b.direct.call_args.args[0]
        prompt = self.b.writer_step.call_args.args[0][1]['content']
        self.assertIn('[1] Алексей: ПЕРВАЯ УНИКАЛЬНАЯ РЕПЛИКА', snapshot['context'])
        self.assertEqual(prompt.count('ПЕРВАЯ УНИКАЛЬНАЯ РЕПЛИКА'), 1)
        self.assertEqual(prompt.count('ВТОРАЯ УНИКАЛЬНАЯ РЕПЛИКА'), 1)

    def test_old_snapshot_still_identifies_selected_target(self):
        snapshot = {'context': 'старый контекст', 'event': 'событие', 'date': time.time(),
                    'targets': [{'id': 1, 'label': 'выбранный текст'}, {'id': 2, 'label': 'чужая реплика'}]}
        decision = {'state': conversation.Store.initial(), 'action': 'reply', 'target': 1}
        self.b.write_turn(self.message(), snapshot, decision, live=False)
        prompt = self.b.writer_step.call_args.args[0][1]['content']
        self.assertIn('выбранный текст', prompt)
        self.assertNotIn('чужая реплика', prompt)

    def test_news_markup_is_sent_as_html_but_history_keeps_original(self):
        answer = '**Новости.** [Источник](https://example.com/?a=1&b=2) <текст>'
        self.b.deliver(self.message(), answer, {'target': 1}, request=1)
        self.assertEqual(self.b.api.call_args.kwargs['parse_mode'], 'HTML')
        self.assertEqual(self.b.api.call_args.kwargs['text'],
                         '<b>Новости.</b> <a href="https://example.com/?a=1&amp;b=2">Источник</a> &lt;текст&gt;')
        self.assertEqual(self.b.history.answer(-1, 1000)[1], answer)

    def test_balance_uses_lower_ceiling_and_caches_only_filtered_data(self):
        responses = [{'data': {'limit_remaining': .75, 'hash': 'SECRET'}},
                     {'data': {'total_credits': 173, 'total_usage': 171.57, 'secret': 'SECRET'}}]
        with patch('bot.fetch', side_effect=[json.dumps(r).encode() for r in responses]) as fetch:
            result = self.b.balance()
            self.assertEqual(self.b.balance(), result)
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(fetch.call_args_list[0].args[0].get_method(), 'GET')
        self.assertEqual(result['available_usd'], .75)
        self.assertEqual(result['account_balance_usd'], 1.43)
        self.assertNotIn('SECRET', json.dumps(result))
        self.assertNotIn('test-key', json.dumps(result))
        self.assertEqual(json.loads(self.b.writer_tool('balance', {}, self.message(), {}, True)), result)

    def test_balance_account_can_be_lower_than_key_and_negative_is_unavailable(self):
        for usage, expected in ((9.8, .2), (10.1, 0)):
            with self.subTest(usage=usage):
                self.b._balance_cache = None
                responses = [{'data': {'limit_remaining': 2}}, {'data': {'total_credits': 10, 'total_usage': usage}}]
                with patch('bot.fetch', side_effect=[json.dumps(r).encode() for r in responses]):
                    self.assertEqual(self.b.balance()['available_usd'], expected)

    def test_balance_does_not_invent_account_funds_when_credits_are_forbidden(self):
        with patch('bot.fetch', side_effect=[b'{"data":{"limit_remaining":0.5}}', RuntimeError('HTTP 403')]):
            result = self.b.balance()
        self.assertEqual(result['available_usd'], .5)
        self.assertFalse(result['account_balance_known'])
        self.assertNotIn('account_balance_usd', result)

    def test_unlimited_key_and_malformed_or_missing_balance(self):
        with patch('bot.fetch', side_effect=[b'{"data":{"limit_remaining":null}}',
                                           b'{"data":{"total_credits":10,"total_usage":8}}']):
            self.assertEqual(self.b.balance()['available_usd'], 2)
        for body in (b'not json', b'{"data":[]}', b'{"data":{"limit_remaining":true}}',
                     b'{"data":{"limit_remaining":NaN}}', b'{"data":{}}'):
            with self.subTest(body=body):
                self.b._balance_cache = None
                with patch('bot.fetch', return_value=body), self.assertRaises(RuntimeError):
                    self.b.balance()

    def test_balance_refreshes_expired_cache_and_replay_never_fetches(self):
        self.b._balance_cache = {'available_usd': 99, 'checked_at': time.time() - 61}
        with patch('bot.fetch', side_effect=[b'{"data":{"limit_remaining":0.1}}', RuntimeError('offline')]) as fetch:
            self.assertEqual(self.b.balance()['available_usd'], .1)
            self.assertEqual(fetch.call_count, 2)
            fetch.reset_mock()
            self.assertIn('error', self.b.balance(live=False))
            fetch.assert_not_called()

    def test_invalid_tool_arguments_reach_writer_as_error(self):
        self.b.writer_step.side_effect = [
            {'role': 'assistant', 'tool_calls': [{'id': 't', 'function': {'name': 'news', 'arguments': 'invalid'}}]},
            {'role': 'assistant', 'content': 'ладно'}]
        self.turn()
        self.assertIn('недоступен', self.b.writer_step.call_args.args[0][-1]['content'])

    def test_participant_tool_in_replay_never_calls_telegram(self):
        m = self.message()
        snapshot = {'context': ''}
        self.b.writer_tool('participants', {'all': True}, m, snapshot, live=False)
        self.b.api.assert_not_called()

    def test_image_reaches_writer_with_short_exposition(self):
        m = self.message()
        m['photo'] = [{'file_id': 'x'}]
        self.b.image = Mock(return_value='data:image/jpeg;base64,AA==')
        self.turn(m)
        messages = self.b.writer_step.call_args.args[0]
        self.assertEqual(messages[1]['content'][1]['type'], 'image_url')
        self.assertNotIn('Тон:', messages[0]['content'])
        self.assertNotIn('панч', messages[0]['content'])

    def test_reaction_event_uses_same_director_without_canned_comeback(self):
        self.b.history.record((-1, 0), 100, 1, time.time(), 'мой ответ', {}, {})
        self.b.direct.side_effect = lambda s, p: {**self.decision(s, p), 'action': 'ignore', 'addressed': False}
        self.b.react({'chat': {'id': -1}, 'message_id': 100, 'date': time.time(), 'user': {'id': 7},
                      'new_reaction': [{'type': 'emoji', 'emoji': '🤡'}]})
        self.b.direct.assert_called_once()
        self.b.writer_step.assert_not_called()
        self.b.api.assert_not_called()

    def test_director_schema_validates_result_and_does_not_treat_probability_as_scale(self):
        snapshot = {'context': 'чат', 'event': 'сообщение', 'target': 1, 'targets': [{'id': 1, 'label': 'текст'}],
                    'explicit': True, 'can_interject': True, 'date': 100}
        response = {'answers': {k: {'choice': v} for k, v in {'addressed': 'yes', 'action': 'reply', 'target': '1', 'attitude': '8', 'role': 'keep'}.items()}}
        response['answers']['attitude']['probabilities'] = {'8': .3}
        result = conversation.decide(Mock(return_value=response), 'key', snapshot, conversation.Store.initial())
        self.assertEqual(result['attitude'], 8)
        for field, invalid in [('target', '999'), ('attitude', '11'), ('action', 'scream')]:
            broken = copy.deepcopy(response)
            broken['answers'][field]['choice'] = invalid
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                conversation.decide(Mock(return_value=broken), 'key', snapshot, conversation.Store.initial())

    def test_idle_uses_director_once_and_never_uses_a_canned_line(self):
        at = datetime.datetime.combine(datetime.date.today(), datetime.time(19)).timestamp()
        self.b.history.add_message((-1, 0), self.message(date=at - 4 * 3600))
        self.b.allowed_groups = {-1}
        self.b.direct.side_effect = lambda snapshot, previous: {**self.decision(snapshot, previous), 'addressed': False}
        with patch('bot.plan', return_value=6):
            self.b.idle(at)
            self.b.idle(at + 10)
        self.b.direct.assert_called_once()
        self.b.writer_step.assert_called_once()
        self.b.api.assert_called_once()
        self.assertFalse(self.b.direct.call_args.args[0]['explicit'])

    def test_night_blocks_initiatives_but_direct_requests_are_answered(self):
        at = datetime.datetime.combine(datetime.date.today(), datetime.time(2)).timestamp()
        self.b.history.add_message((-1, 0), self.message(date=at - 4 * 3600))
        self.b.idle(at)
        self.b.direct.assert_not_called()
        answer, trace = self.turn(self.message(date=at))
        self.assertEqual(trace['action'], 'reply')

    def test_explicit_reply_target_cannot_be_an_old_quoted_message(self):
        snapshot = {'context': 'chat', 'event': 'new request', 'target': 2,
                    'targets': [{'id': 1, 'label': 'old'}, {'id': 2, 'label': 'new'}],
                    'explicit': True, 'can_interject': True, 'date': 100}
        response = {'answers': {k: {'choice': v} for k, v in
                    {'addressed': 'yes', 'action': 'reply', 'target': '1', 'attitude': '5', 'role': 'keep'}.items()}}
        decision = conversation.decide(Mock(return_value=response), 'key', snapshot, conversation.Store.initial())
        self.assertEqual(decision['target'], 2)

    def test_repeated_reaction_event_sends_only_once(self):
        self.b.history.record((-1, 0), 100, 1, time.time(), 'мой ответ', {}, {})
        event = {'chat': {'id': -1}, 'message_id': 100, 'date': time.time(), 'user': {'id': 7},
                 'new_reaction': [{'type': 'emoji', 'emoji': '👍'}]}
        self.b.react(event)
        self.b.react(event)
        self.b.api.assert_called_once()
        self.b.direct.assert_called_once()

    def test_writer_requests_functions_with_medium_reasoning_and_records_usage(self):
        response = {'choices': [{'message': {'role': 'assistant', 'content': 'ok'}}],
                    'usage': {'prompt_tokens': 12, 'completion_tokens': 4, 'cost': .02}}
        with self.b.counting() as usage, patch('bot.post', return_value=response) as post:
            answer = bot.Bot.writer_step(self.b, [{'role': 'user', 'content': 'hello'}])
        self.assertEqual(answer['content'], 'ok')
        payload = post.call_args.args[1]
        self.assertEqual(payload['reasoning'], {'effort': 'medium'})
        self.assertEqual(payload['tools'], conversation.TOOLS)
        self.assertEqual(usage[0]['output_tokens'], 4)
        self.assertEqual(usage[0]['cost'], .02)

    def test_active_global_role_override_blocks_picker_and_preserves_director_actions(self):
        store = conversation.Store(self.b.history.db)
        store.set_role_override('Глад Валакас', 1000, 2000)
        self.b.direct.side_effect = lambda snapshot, previous: {**self.decision(snapshot, previous), 'reconsider': True}
        for message in [self.message(date=1000), self.message(chat=-2, topic=5, date=1999)]:
            _, trace = self.turn(message)
            self.assertEqual(trace['role'], 'Глад Валакас')
            self.assertEqual(trace['attitude'], 7)
            self.assertEqual(trace['action'], 'reply')
            self.assertEqual(trace['role_override_until'], 2000)
        self.b.generate.assert_not_called()

    def test_override_expiry_boundary_restores_selection_even_when_jev_says_keep(self):
        store = conversation.Store(self.b.history.db)
        store.set_role_override('Глад Валакас', 1000, 2000)
        self.turn(self.message(date=1999))
        self.b.generate.assert_not_called()
        self.assertIsNone(store.role_override(999))
        self.assertIsNone(store.role_override(2000))
        # The picker may independently choose Glad again; that must not force repeated reselection.
        _, trace = self.turn(self.message(date=2000))
        self.assertEqual(trace['role'], 'Глад Валакас')
        self.assertNotIn('role_override_until', trace)
        self.b.generate.assert_called_once()
        self.turn(self.message(date=2001))
        self.b.generate.assert_called_once()

    def test_setting_override_updates_all_current_states_without_changing_attitudes(self):
        self.turn(self.message(date=900))
        self.b.history.add_message((-2, 5), self.message(chat=-2, topic=5))
        store = conversation.Store(self.b.history.db)
        store.set_role_override('Глад Валакас', 1000, 2000)
        self.assertEqual(store.state((-1, 0))['role'], 'Глад Валакас')
        self.assertEqual(store.state((-1, 0))['attitude'], 7)
        self.assertEqual(store.state((-2, 5))['role'], 'Глад Валакас')
        self.assertEqual(store.state((-2, 5))['attitude'], 5)

    def test_role_override_does_not_affect_historical_replay(self):
        store = conversation.Store(self.b.history.db)
        store.set_role_override('Глад Валакас', 1000, 2000)
        self.b.generate.return_value = 'Ким Кицураги'
        _, trace = self.turn(self.message(date=999), live=False)
        self.assertEqual(trace['role'], 'Ким Кицураги')
        self.assertNotIn('role_override_until', trace)

    def test_jev_and_writer_costs_are_counted_once(self):
        _, first = self.turn()
        self.assertEqual(first['cost'], .001)
        self.assertIsNone(self.b.usage)


if __name__ == '__main__':
    unittest.main()
