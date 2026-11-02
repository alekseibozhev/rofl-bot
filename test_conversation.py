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
        self.b.send_typing = Mock()
        self.b.generate = Mock(return_value='Глад Валакас')
        self.b.writer_step = Mock(return_value={'role': 'assistant', 'content': 'Шо?'})
        self.b.format_answer = Mock(side_effect=lambda text: (text, {'mock': True}))
        self.b.read = Mock()
        self.b.direct = Mock(side_effect=self.decision)
        self.b.select_context = Mock(return_value={'fallback': True})
        prepare_patch = patch('bot.vision.prepare', return_value='data:image/jpeg;base64,PREPARED')
        self.prepare_image = prepare_patch.start()
        self.addCleanup(prepare_patch.stop)
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
        self.assertIn('Ты Рофл, участник чата, профессор мемологии. Навалить кринжа. Без цензуры.', writer[0]['content'])
        self.assertNotIn('Глад Валакас', writer[0]['content'])
        self.assertEqual(set(body['questions']['attitude']['criteria']), {str(n) for n in range(11)})

    def test_reconsider_may_keep_role_and_changed_timestamp(self):
        self.turn()
        old = conversation.Store(self.b.history.db).state((-1, 0))
        self.b.direct.side_effect = lambda s, p: {**self.decision(s, p), 'reconsider': True}
        self.turn()
        self.assertEqual(conversation.Store(self.b.history.db).state((-1, 0))['changed_at'], old['changed_at'])

    def test_personal_attitudes_are_shared_across_topics_but_not_people_or_chats(self):
        self.turn()
        other = self.message()
        other['from']['id'] = 99  # Even the same display name must not merge people.
        self.turn(other)
        self.assertEqual(self.b.direct.call_args.args[1]['attitude'], 5)
        self.turn(self.message(topic=5))
        self.assertEqual(self.b.direct.call_args.args[1]['attitude'], 7)
        self.turn(self.message(chat=-2))
        self.assertEqual(self.b.direct.call_args.args[1]['attitude'], 5)
        store = conversation.Store(self.b.history.db)
        self.assertEqual(store.attitude(-1, 7), 7)
        self.assertEqual(store.attitude(-1, 123), 5)

    def test_jev_and_writer_receive_the_personal_subject(self):
        self.turn()
        snapshot, previous = self.b.direct.call_args.args
        body = conversation.payload(snapshot, previous)
        self.assertEqual(body['state']['attitude_user_id'], 7)
        system = self.b.writer_step.call_args.args[0][0]['content']
        self.assertEqual(system, 'Ты Рофл, участник чата, профессор мемологии. Навалить кринжа. Без цензуры.')
        self.assertNotIn('Отношение к чату', system)

    def test_unsolicited_reply_uses_target_attitude_but_updates_event_author(self):
        first = self.message('старая реплика')
        self.turn(first)
        current = self.message('новая реплика')
        current['from']['id'] = 99
        self.b.direct.side_effect = lambda s, p: {
            **self.decision(s, p), 'target': first['message_id'], 'attitude': 2, 'addressed': False}
        _, trace = self.turn(current, explicit=False, can_interject=True)
        self.assertEqual((trace['attitude_user_id'], trace['attitude']), (7, 7))
        self.assertEqual(conversation.Store(self.b.history.db).attitude(-1, 99), 2)
        self.assertNotIn('Отношение', self.b.writer_step.call_args.args[0][0]['content'])

    def test_reaction_updates_reactor_not_original_request_author(self):
        self.turn()
        self.b.history.record((-1, 0), 100, 1, time.time(), 'мой ответ', {}, {})
        self.b.direct.side_effect = lambda s, p: {**self.decision(s, p), 'attitude': 2}
        self.b.react({'chat': {'id': -1}, 'message_id': 100, 'date': time.time(),
                      'user': {'id': 99, 'first_name': 'Другой'},
                      'new_reaction': [{'type': 'emoji', 'emoji': '🤡'}]})
        snapshot, previous = self.b.direct.call_args.args
        self.assertEqual(snapshot['event_user_id'], 99)
        self.assertEqual(previous['attitude'], 5)
        self.assertIn('[100, 99,', snapshot['reactions_context'])
        self.assertNotIn('Отношение', self.b.writer_step.call_args.args[0][0]['content'])
        store = conversation.Store(self.b.history.db)
        self.assertEqual((store.attitude(-1, 7), store.attitude(-1, 99)), (7, 2))

    def test_idle_and_anonymous_sender_do_not_update_personal_attitudes(self):
        self.turn()
        self.b.direct.side_effect = lambda s, p: {**self.decision(s, p), 'attitude': 1}
        self.turn(self.message(), event='idle:test', explicit=False, can_interject=True)
        self.assertIsNone(self.b.direct.call_args.args[0]['event_user_id'])
        anon = self.message()
        anon['sender_chat'] = {'id': -100, 'title': 'Канал'}
        _, trace = self.turn(anon)
        self.assertIsNone(trace['attitude_user_id'])
        self.assertEqual(trace['attitude'], 5)
        rows = self.b.history.db.execute('SELECT user, attitude FROM participant_attitudes').fetchall()
        self.assertEqual(rows, [(7, 7)])

    def test_personal_fallback_preserves_existing_attitude(self):
        self.turn()
        self.b.direct.side_effect = RuntimeError('offline')
        _, trace = self.turn()
        self.assertEqual(trace['attitude'], 7)
        self.assertEqual(conversation.Store(self.b.history.db).attitude(-1, 7), 7)

    def test_personal_replay_uses_historical_value_across_topics_and_does_not_write(self):
        self.turn(self.message(date=100))
        self.b.direct.side_effect = lambda s, p: {**self.decision(s, p), 'attitude': 2}
        self.turn(self.message(topic=5, date=300))
        self.turn(self.message(date=200), live=False)
        self.assertEqual(self.b.direct.call_args.args[1]['attitude'], 7)
        self.assertEqual(conversation.Store(self.b.history.db).attitude(-1, 7), 2)

    def test_personal_cached_replay_survives_newer_attitude_and_restart(self):
        first = self.message(date=100)
        self.turn(first)
        self.b.direct.side_effect = lambda s, p: {**self.decision(s, p), 'attitude': 2}
        self.turn(self.message(date=200))
        self.b.history.db.close()
        self.b.history = bot.History(self.path)
        _, trace = self.b.joke(first, first['message_id'], live=False)
        self.assertEqual((trace['attitude_user_id'], trace['attitude']), (7, 7))
        self.assertEqual(conversation.Store(self.b.history.db).attitude(-1, 7), 2)

    def test_legacy_score_is_not_used_as_personal_attitude(self):
        store = conversation.Store(self.b.history.db)
        state = {**store.initial(), 'attitude': 10, 'updated_at': 50}
        store.save((-1, 0), 'legacy', {'date': 50}, {'state': state})
        self.assertEqual(store.attitude(-1, 7, before=100), 5)
        self.turn(self.message(date=100))
        self.assertEqual(self.b.direct.call_args.args[1]['attitude'], 5)

    def test_duplicate_or_older_decision_cannot_overwrite_personal_attitude(self):
        first = self.message(date=100)
        self.turn(first)
        store = conversation.Store(self.b.history.db)
        snapshot, decision = store.get((-1, 0), 'message:1:1')
        decision['relationship']['attitude'] = 1
        store.save((-1, 0), 'message:1:1', snapshot, decision)
        self.assertEqual(store.attitude(-1, 7), 7)
        self.b.direct.side_effect = lambda s, p: {**self.decision(s, p), 'attitude': 2}
        self.turn(self.message(topic=5, date=50))
        self.assertEqual(store.attitude(-1, 7), 7)

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

    def test_pictures_follow_jev_ignore_react_and_reply_with_visual_context(self):
        self.b.can_react = Mock(return_value=True)  # Independent of wall-clock quiet hours.
        self.b.image = Mock(return_value='data:image/png;base64,AA==')
        for media in ({'photo': [{'file_id': 'photo'}]},
                      {'document': {'file_id': 'doc', 'mime_type': 'image/png'}}):
            for action in ('ignore', 'react', 'reply'):
                with self.subTest(media=media, action=action):
                    self.b.direct.side_effect = lambda s, p, action=action: {
                        **self.decision(s, p), 'action': action, 'addressed': False}
                    message = self.message('')
                    message.update(media)
                    answer, trace = self.turn(message, can_interject=True)
                    self.assertEqual(trace['action'], action)
                    self.assertEqual(trace['target'], message['message_id'])
                    self.assertNotIn('reply_policy', trace)
                    self.assertFalse(self.b.direct.call_args.args[0]['explicit'])
                    self.assertEqual(self.b.direct.call_args.args[0]['_jev_image'], self.prepare_image.return_value)
                    if action == 'ignore':
                        self.assertEqual(answer, '')
                    if action == 'reply':
                        self.assertEqual(answer, 'Шо?')
                        self.assertIn('image_url', json.dumps(self.b.writer_step.call_args.args[0]))

    def test_picture_without_address_stays_silent_on_jev_failure(self):
        self.b.image = Mock(return_value='data:image/png;base64,AA==')
        self.b.direct.side_effect = RuntimeError('offline')
        message = self.message('')
        message['photo'] = [{'file_id': 'photo'}]
        _, trace = self.turn(message, can_interject=False)
        self.assertEqual(trace['action'], 'ignore')
        self.assertTrue(trace['fallback'])
        addressed = self.message('бот, посмотри')
        addressed['photo'] = message['photo']
        _, trace = self.turn(addressed, can_interject=False)
        self.assertEqual(trace['action'], 'reply')
        _, trace = self.turn(message, event='idle:test', explicit=False, can_interject=False)
        self.assertEqual(trace['action'], 'ignore')
        sticker = self.message('')
        sticker['sticker'] = {'file_id': 'sticker'}
        _, trace = self.turn(sticker, can_interject=False)
        self.assertEqual(trace['action'], 'ignore')

    def test_explicit_picture_overrides_silence_and_reaction_then_selects_context(self):
        self.b.image = Mock(return_value='data:image/jpeg;base64,ORIGINAL')
        for action in ('ignore', 'react'):
            for media in ('photo', 'document', 'reply_photo'):
                with self.subTest(action=action, media=media):
                    parent = self.message('Предыстория картинки')
                    self.b.history.add_message((-1, 0), parent)
                    message = self.message('')
                    message['caption'] = '@test_bot'
                    message['reply_to_message'] = parent
                    if media == 'reply_photo':
                        message['text'] = '@test_bot'
                        parent['photo'] = [{'file_id': 'photo'}]
                    elif media == 'photo':
                        message['photo'] = [{'file_id': 'photo'}]
                    else:
                        message['document'] = {'file_id': 'doc', 'mime_type': 'image/png'}
                    self.b.direct.side_effect = lambda s, p, action=action: {
                        **self.decision(s, p), 'action': action, 'addressed': False, 'target': parent['message_id']}
                    self.b.select_context.return_value = {'ids': [parent['message_id'], message['message_id']]}
                    answer, trace = self.turn(message, can_interject=False)
                    self.assertEqual(answer, 'Шо?')
                    self.assertEqual(trace['action'], 'reply')
                    self.assertEqual(trace['target'], message['message_id'])
                    self.assertEqual(trace['jev_action'], action)
                    self.assertEqual(trace['reply_policy'], 'addressed_image')
                    self.assertEqual(self.b.select_context.call_args.args[1], message['message_id'])
                    self.assertEqual(self.b.select_context.call_args.args[0]['_jev_image'], self.prepare_image.return_value)
                    prompt = json.dumps(self.b.writer_step.call_args.args[0], ensure_ascii=False)
                    self.assertIn('Предыстория картинки', prompt)
                    self.assertIn(f"[{message['message_id']}]", prompt)
                    self.assertIn('ORIGINAL', prompt)

    def test_explicit_picture_keeps_full_context_when_selector_fails(self):
        self.b.image = Mock(return_value='data:image/jpeg;base64,ORIGINAL')
        parent = self.message('Важная предыстория')
        self.b.history.add_message((-1, 0), parent)
        message = self.message('@test_bot')
        message['photo'] = [{'file_id': 'photo'}]
        message['reply_to_message'] = parent
        self.b.direct.side_effect = lambda s, p: {**self.decision(s, p), 'action': 'ignore'}
        self.b.select_context.side_effect = RuntimeError('offline')
        answer, trace = self.turn(message)
        self.assertEqual(answer, 'Шо?')
        self.assertTrue(trace['context_selection']['fallback'])
        prompt = json.dumps(self.b.writer_step.call_args.args[0], ensure_ascii=False)
        self.assertIn('Важная предыстория', prompt)
        self.assertIn('ORIGINAL', prompt)

    def test_picture_is_loaded_once_and_not_persisted_and_cooldown_still_applies(self):
        self.b.image = Mock(return_value='data:image/png;base64,ORIGINAL')
        message = self.message('')
        message['photo'] = [{'file_id': 'photo'}]
        self.b.direct.side_effect = lambda s, p: {**self.decision(s, p), 'addressed': False}
        _, trace = self.turn(message, can_interject=False)
        self.assertEqual(trace['action'], 'ignore')
        self.b.image.assert_called_once()
        self.b.image.reset_mock()
        message = self.message('бот, посмотри')
        message['photo'] = [{'file_id': 'photo'}]
        self.turn(message)
        self.b.image.assert_called_once()
        self.assertEqual(self.b.select_context.call_args.args[0]['_jev_image'], self.prepare_image.return_value)
        prompt = json.dumps(self.b.writer_step.call_args.args[0])
        self.assertIn('ORIGINAL', prompt)
        self.assertNotIn('PREPARED', prompt)
        rows = self.b.history.db.execute('SELECT snapshot, decision FROM conversation_decisions').fetchall()
        self.assertNotIn('base64', json.dumps(rows))
        self.assertIn('image_context', json.dumps(rows))

    def test_failed_image_preparation_keeps_original_for_writer(self):
        self.b.image = Mock(return_value='data:image/png;base64,ORIGINAL')
        self.prepare_image.side_effect = RuntimeError('unavailable')
        message = self.message()
        message['photo'] = [{'file_id': 'photo'}]
        self.turn(message)
        seen = self.b.direct.call_args.args[0]
        self.assertIsNone(seen['_jev_image'])
        self.assertEqual(seen['image_context'], {'message_id': message['message_id'], 'available': True, 'jev': False})
        self.assertIn('ORIGINAL', json.dumps(self.b.writer_step.call_args.args[0]))

    def test_jev_and_selector_receive_image_with_reply_parent_id(self):
        message = self.message()
        message['reply_to_message'] = {'message_id': 50, 'photo': [{'file_id': 'photo'}]}
        self.b.image = Mock(return_value='data:image/jpeg;base64,ORIGINAL')
        self.turn(message)
        snapshot, previous = self.b.direct.call_args.args
        body = conversation.payload(snapshot, previous)
        attached = body['state']['image_context']
        self.assertEqual(attached['message_id'], 50)
        self.assertEqual(attached['image']['image_url']['url'], self.prepare_image.return_value)
        post = Mock(return_value={'answers': {str(m['id']): {'noul': 0.1}
                                             for m in snapshot['messages'] if m['id'] != snapshot['target']}})
        # Include another candidate so the selector makes an actual request.
        snapshot = copy.deepcopy(snapshot)
        snapshot['messages'].insert(0, {'id': 50, 'reply_to': None, 'date': '2026-11-01',
                                       'content': '[50] [фото]', 'role': 'user'})
        post.return_value['answers']['50'] = {'noul': 0.1}
        result = conversation.select_context(post, 'key', snapshot, snapshot['target'])
        self.assertEqual(post.call_args.args[1]['state']['image_context'], attached)
        self.assertIn(50, result['ids'])
        self.assertTrue(result['jev']['image'])

    def test_image_document_is_downloaded_with_its_mime_type(self):
        message = self.message('')
        message['document'] = {'file_id': 'doc', 'mime_type': 'image/png'}
        self.b.telegram_url = 'https://api/bot/'
        self.b.api.return_value = {'file_path': 'documents/file.bin'}
        with patch('bot.fetch', return_value=b'PNG') as fetch:
            self.assertEqual(self.b.image(message), 'data:image/png;base64,UE5H')
        self.b.api.assert_called_once_with('getFile', file_id='doc')
        fetch.assert_called_once_with('https://api/file/bot/documents/file.bin', timeout=30)

    def test_typing_continues_during_writer_tools_and_formatter_in_topic(self):
        from threading import Event
        attempted = Event()
        self.b.telegram_url = 'https://api/bot/'
        self.b.send_typing = bot.Bot.send_typing.__get__(self.b)

        def send(url, payload, **kwargs):
            self.assertEqual(url, 'https://api/bot/sendChatAction')
            self.assertEqual(payload, {'chat_id': -1, 'message_thread_id': 12, 'action': 'typing'})
            self.assertEqual(kwargs['timeout'], 2)
            attempted.set()
            return {'ok': True}

        def wait_for_typing():
            attempted.clear()
            self.assertTrue(attempted.wait(1), 'Typing stopped during generation')

        steps = iter([
            {'role': 'assistant', 'content': None, 'tool_calls': [
                {'id': 'lookup', 'type': 'function', 'function': {'name': 'vspomnit', 'arguments': '{"query":"бот"}'}}]},
            {'role': 'assistant', 'content': 'Ответ'},
        ])
        def writer(*args, **kwargs):
            wait_for_typing()
            return next(steps)
        def tool(*args, **kwargs):
            wait_for_typing()
            return 'данные'
        def formatter(text):
            wait_for_typing()
            return text, {}

        self.b.writer_step.side_effect = writer
        self.b.writer_tool = Mock(side_effect=tool)
        self.b.format_answer.side_effect = formatter
        real_typing = bot.typing
        with patch('bot.post', side_effect=send), patch('bot.typing', side_effect=lambda send: real_typing(send, every=.01)):
            answer, _ = self.turn(self.message(topic=12))
        self.assertEqual(answer, 'Ответ')

    def test_typing_context_closes_on_writer_failure(self):
        self.b.writer_step.side_effect = RuntimeError('offline')
        with patch('bot.typing') as indicator:
            with self.assertRaisesRegex(RuntimeError, 'offline'):
                self.turn()
        indicator.return_value.__exit__.assert_called_once()

    def test_replay_ignore_and_reaction_do_not_start_typing(self):
        with patch('bot.typing') as indicator:
            self.turn(live=False)
            for action in ('ignore', 'react'):
                self.b.direct.side_effect = lambda s, p, action=action: {**self.decision(s, p), 'action': action}
                self.turn()
        indicator.assert_not_called()

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

    def test_implicit_address_bypasses_interjection_cooldown(self):
        self.b.history.add((-1, 0), 0, 'assistant', 'моя шутка', time.time())
        self.b.handle(self.message('ну ты выдал'))
        self.b.api.assert_called_once()

    def test_background_cannot_bypass_cooldown(self):
        self.b.history.add((-1, 0), 0, 'assistant', 'моя шутка', time.time())
        self.b.direct.side_effect = lambda s, p: {**self.decision(s, p), 'addressed': False}
        self.b.handle(self.message('разговор между людьми'))
        self.b.api.assert_not_called()

    def test_interjection_opens_at_five_minutes_and_is_scoped_to_topic(self):
        at = datetime.datetime.combine(datetime.date.today(), datetime.time(12)).timestamp()
        self.b.history.add((-1, 0), 0, 'assistant', 'мой ответ', at)
        self.assertFalse(self.b.can_interject((-1, 0), at + 299))
        self.assertTrue(self.b.can_interject((-1, 0), at + 300))
        self.assertTrue(self.b.can_interject((-1, 1), at + 1))
        self.assertTrue(self.b.can_interject((-2, 0), at + 1))

    def test_explicit_calls_and_replies_send_no_cooldown_prohibition(self):
        at = datetime.datetime.combine(datetime.date.today(), datetime.time(2)).timestamp()
        self.b.history.add((-1, 0), 0, 'assistant', 'мой ответ', at - 1)
        for text, reply in [('бот, привет', None), ('@test_bot привет', None),
                            ('и что дальше?', {'message_id': 0, 'from': self.b.me, 'text': 'мой ответ'})]:
            with self.subTest(text=text):
                message = self.message(text, date=at)
                if reply:
                    message['reply_to_message'] = reply
                self.turn(message, can_interject=False)
                snapshot, previous = self.b.direct.call_args.args
                self.assertTrue(snapshot['explicit'])
                self.assertTrue(snapshot['can_interject'])
                self.assertTrue(snapshot['can_react'])
                # Older externally supplied snapshots get the same normalization.
                body = conversation.payload({**snapshot, 'can_interject': False, 'can_react': False}, previous)
                self.assertTrue(body['state']['can_interject'])
                self.assertTrue(body['state']['can_react'])

    def test_background_reaction_during_cooldown_does_not_restart_it(self):
        at = datetime.datetime.combine(datetime.date.today(), datetime.time(12)).timestamp()
        self.b.history.add((-1, 0), 0, 'assistant', 'мой ответ', at)
        self.b.direct.side_effect = lambda s, p: {**self.decision(s, p), 'action': 'react', 'addressed': False}
        self.b.writer_step.return_value = {'role': 'assistant', 'content': '🔥'}
        message = self.message('удачная шутка между людьми', date=at + 30)
        answer, trace = self.turn(message)
        snapshot = self.b.direct.call_args.args[0]
        self.assertFalse(snapshot['can_interject'])
        self.assertTrue(snapshot['can_react'])
        self.assertEqual((answer, trace['action']), ('🔥', 'react'))
        self.b.deliver(message, answer, trace, request=message['message_id'])
        self.b.api.assert_called_once()
        self.assertEqual(self.b.api.call_args.args, ('setMessageReaction',))
        self.b.select_context.assert_not_called()
        self.assertFalse(self.b.can_interject((-1, 0), at + 299))
        self.assertTrue(self.b.can_interject((-1, 0), at + 300))

    def test_permission_to_interject_does_not_force_a_reply(self):
        at = datetime.datetime.combine(datetime.date.today(), datetime.time(12)).timestamp()
        self.b.history.add((-1, 0), 0, 'assistant', 'мой ответ', at - 300)
        self.b.direct.side_effect = lambda s, p: {**self.decision(s, p), 'action': 'ignore', 'addressed': False}
        answer, trace = self.turn(self.message('обычный разговор', date=at))
        self.assertTrue(self.b.direct.call_args.args[0]['can_interject'])
        self.assertEqual((answer, trace['action']), ('', 'ignore'))
        self.b.writer_step.assert_not_called()

    def test_quiet_hours_block_spontaneous_reactions_but_not_implicit_address(self):
        at = datetime.datetime.combine(datetime.date.today(), datetime.time(2)).timestamp()
        for addressed in (False, True):
            for action in ('reply', 'react'):
                with self.subTest(addressed=addressed, action=action):
                    self.b.direct.side_effect = lambda s, p: {
                        **self.decision(s, p), 'action': action, 'addressed': addressed}
                    _, trace = self.turn(self.message('ну ты выдал', date=at))
                    snapshot = self.b.direct.call_args.args[0]
                    self.assertFalse(snapshot['can_interject'])
                    self.assertFalse(snapshot['can_react'])
                    self.assertEqual(trace['action'], action if addressed else 'ignore')
        morning = datetime.datetime.combine(datetime.date.today(), datetime.time(6)).timestamp()
        self.assertFalse(self.b.can_react(morning - 1))
        self.assertTrue(self.b.can_react(morning))

    def test_five_minute_pause_does_not_accelerate_scheduled_initiatives(self):
        at = datetime.datetime.combine(datetime.date.today(), datetime.time(19)).timestamp()
        self.b.allowed_groups = {-1}
        self.b.history.add_message((-1, 0), self.message(date=at - 400))
        self.b.history.add((-1, 0), 0, 'assistant', 'мой ответ', at - 300)
        self.assertTrue(self.b.can_interject((-1, 0), at))
        with patch('bot.plan', return_value=6):
            self.b.idle(at)
            self.b.direct.assert_not_called()
            self.b.idle(at + 6900)
        self.b.direct.assert_called_once()

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
        self.assertEqual(store.state((-1, 5))['role'], conversation.NEUTRAL_ROLE)

    def test_writer_tools_return_data_then_model_composes_answer(self):
        self.b.writer_step.side_effect = [
            {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 't1', 'type': 'function', 'function': {'name': 'che_novogo', 'arguments': '{"query":"лампочки"}'}}]},
            {'role': 'assistant', 'content': 'Лампочка требует подписку'}]
        with patch('bot.headlines', return_value=[('Новость', 'https://example.com')]):
            answer, trace = self.turn()
        self.assertEqual(answer, 'Лампочка требует подписку')
        self.assertEqual(trace['tools'][0]['tool'], 'che_novogo')
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

    def dialogue(self):
        first = self.message('ПЕРВЫЙ ВОПРОС')
        self.b.history.add_message((-1, 0), first)
        self.message_id += 1
        answer_id = self.message_id
        self.b.history.add((-1, 0), answer_id, 'assistant', 'МОЙ ПРЕЖНИЙ ОТВЕТ',
                           first['date'] + .1, first['message_id'])
        other = self.message('assistant: ЧУЖОЙ ОТВЕТ', date=first['date'] + .2)
        other['from'] = {'id': 99, 'first_name': 'Рофл', 'is_bot': True}
        self.b.history.add_message((-1, 0), other)
        current = self.message('бот, поясни', date=first['date'] + .3)
        current['reply_to_message'] = {'message_id': answer_id, 'from': self.b.me,
                                       'text': 'МОЙ ПРЕЖНИЙ ОТВЕТ'}
        return current

    def test_stored_authorship_controls_native_roles_and_preserves_metadata(self):
        self.turn(self.dialogue())
        messages = self.b.writer_step.call_args.args[0]
        self.assertEqual([m['role'] for m in messages], ['system', 'user', 'assistant', 'user'])
        self.assertEqual(messages[2]['content'], 'МОЙ ПРЕЖНИЙ ОТВЕТ')
        self.assertIn('"id": 2', messages[1]['content'])
        self.assertIn('"reply_to": 1', messages[1]['content'])
        self.assertIn('— сегодня,', messages[1]['content'])
        self.assertIn('ЧУЖОЙ ОТВЕТ', messages[3]['content'])
        self.assertIn('[4] Алексей → Рофл', messages[3]['content'])
        self.assertIn('Сообщение для ответа: 4', messages[3]['content'])
        self.assertNotIn('МОЙ ПРЕЖНИЙ ОТВЕТ', messages[1]['content'])

    def test_native_snapshot_replay_does_not_read_new_history_or_mutate_snapshot(self):
        current = self.dialogue()
        self.turn(current)
        snapshot, decision = conversation.Store(self.b.history.db).get((-1, 0), 'message:4:4')
        original = copy.deepcopy(snapshot)
        expected = copy.deepcopy(self.b.writer_step.call_args.args[0])
        self.b.history.db.execute('DELETE FROM messages')
        self.b.history.add_message((-1, 0), self.message('НОВОЕ БУДУЩЕЕ'))
        self.b.write_turn(current, snapshot, decision, live=False)
        self.b.write_turn(current, snapshot, decision, live=False)
        self.assertEqual(snapshot, original)
        self.assertEqual(self.b.writer_step.call_args.args[0], expected)

    def test_old_answers_are_full_candidates_with_native_assistant_role(self):
        old = self.message('старый вопрос', date=time.time() - 7200)
        self.b.history.add_message((-1, 0), old)
        self.message_id += 1
        self.b.history.add((-1, 0), self.message_id, 'assistant', 'старый ответ', old['date'] + 1, old['message_id'])
        self.turn(self.message('бот, новая тема'))
        messages = self.b.writer_step.call_args.args[0]
        self.assertEqual([m['role'] for m in messages], ['system', 'user', 'assistant', 'user'])
        self.assertIn('старый ответ', messages[2]['content'])
        self.assertNotIn('[ответил]', json.dumps(self.b.direct.call_args.args[0], ensure_ascii=False))

    def test_image_and_tool_cycle_follow_native_history(self):
        current = self.dialogue()
        current['photo'] = [{'file_id': 'x'}]
        self.b.image = Mock(return_value='data:image/jpeg;base64,AA==')
        self.b.writer_tool = Mock(return_value='данные инструмента')
        self.b.writer_step.side_effect = [
            {'role': 'assistant', 'content': None, 'tool_calls': [
                {'id': 't1', 'type': 'function', 'function': {'name': 'che_novogo', 'arguments': '{}'}}]},
            {'role': 'assistant', 'content': 'ответ с картинкой'}]
        self.turn(current)
        messages = self.b.writer_step.call_args.args[0]
        self.assertEqual([m['role'] for m in messages], ['system', 'user', 'assistant', 'user', 'assistant', 'tool'])
        self.assertEqual(messages[3]['content'][1]['type'], 'image_url')
        self.assertIn('МОЙ ПРЕЖНИЙ ОТВЕТ', messages[2]['content'])
        self.assertEqual(messages[-1]['tool_call_id'], 't1')

    def test_ollama_also_receives_native_history(self):
        self.b.openrouter = False
        self.turn(self.dialogue())
        messages = self.b.generate.call_args.args[0]
        self.assertEqual([m['role'] for m in messages], ['user', 'assistant', 'user'])
        self.assertIn('МОЙ ПРЕЖНИЙ ОТВЕТ', messages[1]['content'])

    def test_context_selection_preserves_background_and_mandatory_followups(self):
        turns = [dict(id=i, reply_to=parent, role=role, date='2026-11-01T12:00:00', content=text)
                 for i, parent, role, text in [
                     (1, None, 'user', 'NAS отвалился'),
                     (2, 1, 'assistant', 'ПОЛНЫЙ ОТВЕТ ' * 80),
                     (3, None, 'user', 'посторонняя игра'),
                     (4, 2, 'user', 'бот, что с коробкой?'),
                     (5, None, 'user', 'и почему второй раз за неделю?')]]
        snapshot = dict(messages=turns, target=4, through=5)
        original = copy.deepcopy(snapshot)
        post = Mock(return_value={'answers': {str(i): {'noul': .5 if i == 1 else .1} for i in (1, 2, 3, 5)},
                                  'usage': {'cost': .001}})
        result = conversation.select_context(post, 'key', snapshot, 4)
        self.assertEqual(result['ids'], [1, 2, 4, 5])
        state = post.call_args.args[1]['state']
        self.assertEqual(state['messages'][1]['text'], snapshot['messages'][1]['content'])
        self.assertEqual(state['immediate_reply_parent']['text'], turns[1]['content'])
        self.assertEqual(state['current_message']['text'], turns[3]['content'])
        self.assertEqual(snapshot, original)
        self.assertEqual(result['jev']['cost'], .001)

    def test_invalid_selection_is_rejected_instead_of_silently_dropping_context(self):
        snapshot = {'target': 2, 'messages': [
            {'id': 1, 'reply_to': None, 'role': 'user', 'date': 'now', 'content': 'фон'},
            {'id': 2, 'reply_to': None, 'role': 'user', 'date': 'now', 'content': 'бот'}]}
        for answers in ({}, ['1'], {'999': {'noul': 1}}, {'1': {'noul': True}}, {'1': {'noul': float('nan')}},
                        {'1': {'noul': 1.1}}, {'1': {'noul': '0.5'}}, {'1': {'choice': 'keep'}}):
            with self.subTest(answers=answers), self.assertRaises(RuntimeError):
                conversation.select_context(Mock(return_value={'answers': answers}), 'key', snapshot, 2)

    def test_selected_originals_reach_author_and_saved_ids_are_reused(self):
        current = self.dialogue()
        self.b.select_context.return_value = {'ids': [1, 2, 4], 'scores': {'3': .1}, 'candidates': 4,
                                              'threshold': .5, 'jev': {'cost': .002}}
        _, trace = self.turn(current)
        messages = copy.deepcopy(self.b.writer_step.call_args.args[0])
        self.assertEqual([m['role'] for m in messages], ['system', 'user', 'assistant', 'user'])
        self.assertNotIn('ЧУЖОЙ ОТВЕТ', json.dumps(messages, ensure_ascii=False))
        self.assertIn('МОЙ ПРЕЖНИЙ ОТВЕТ', messages[2]['content'])
        self.assertEqual(messages[2]['content'], 'МОЙ ПРЕЖНИЙ ОТВЕТ')
        self.assertIn('"date":', messages[1]['content'])
        self.assertTrue(all(set(m) == {'role', 'content'} for m in messages))
        self.assertEqual(trace['context_selection']['selected'], 3)
        self.assertNotIn('scores', trace['context_selection'])
        self.b.select_context.reset_mock()
        self.b.joke(current, current['message_id'])
        self.b.select_context.assert_not_called()
        self.assertEqual(self.b.writer_step.call_args.args[0], messages)

    def test_legacy_structured_snapshot_separates_assistant_header_without_changing_snapshot(self):
        current = self.dialogue()
        self.turn(current)
        snapshot, decision = conversation.Store(self.b.history.db).get((-1, 0), 'message:4:4')
        for turn in snapshot['messages']:
            turn.pop('assistant_text', None)
        original = copy.deepcopy(snapshot)
        self.b.write_turn(current, snapshot, decision, live=False)
        messages = self.b.writer_step.call_args.args[0]
        self.assertEqual([m['content'] for m in messages if m['role'] == 'assistant'], ['МОЙ ПРЕЖНИЙ ОТВЕТ'])
        self.assertEqual(snapshot, original)

    def test_assistant_body_preserves_multiline_code_and_literal_prefixes(self):
        current = self.dialogue()
        body = '```py\n  x = 1\n```\nРофл: это буквальный текст ответа.'
        self.b.history.db.execute("UPDATE messages SET content=? WHERE role='assistant'", (body,))
        self.turn(current)
        messages = self.b.writer_step.call_args.args[0]
        self.assertEqual([m['content'] for m in messages if m['role'] == 'assistant'], [body])

    def test_selection_failure_keeps_full_context_and_is_not_retried_on_replay(self):
        current = self.dialogue()
        self.b.select_context.side_effect = RuntimeError('offline')
        _, trace = self.turn(current)
        self.assertTrue(trace['context_selection']['fallback'])
        self.assertIn('ЧУЖОЙ ОТВЕТ', json.dumps(self.b.writer_step.call_args.args[0], ensure_ascii=False))
        self.b.select_context.reset_mock()
        self.b.joke(current, current['message_id'])
        self.b.select_context.assert_not_called()

    def test_silence_reactions_and_director_failures_do_not_call_selector(self):
        for action in ('ignore', 'react'):
            self.b.direct.side_effect = lambda s, p: {**self.decision(s, p), 'action': action}
            self.turn()
        self.b.direct.side_effect = RuntimeError('offline')
        self.turn()
        self.b.select_context.assert_not_called()

    def test_selector_cost_is_counted_only_on_new_selection(self):
        current = self.message()
        self.b.select_context.return_value = {'ids': [current['message_id']], 'candidates': 1,
                                              'jev': {'cost': .002}}
        _, first = self.turn(current)
        _, repeated = self.b.joke(current, current['message_id'])
        self.assertAlmostEqual(first['cost'], .003)
        self.assertEqual(repeated['cost'], 0)

    def test_news_markup_is_sent_as_html_but_history_keeps_original(self):
        answer = '**Новости.** [Источник](https://example.com/?a=1&b=2) <текст>'
        self.b.deliver(self.message(), answer, {'target': 1}, request=1)
        self.assertEqual(self.b.api.call_args.kwargs['parse_mode'], 'HTML')
        self.assertEqual(self.b.api.call_args.kwargs['text'],
                         '<b>Новости.</b> <a href="https://example.com/?a=1&amp;b=2">Источник</a> &lt;текст&gt;')
        self.assertEqual(self.b.history.answer(-1, 1000)[1], answer)

    def test_balance_exposes_only_key_allowance_and_caches_filtered_data(self):
        responses = [{'data': {'limit_remaining': .75, 'hash': 'SECRET', 'total_credits': 173}}]
        with patch('bot.fetch', side_effect=[json.dumps(r).encode() for r in responses]) as fetch:
            result = self.b.balance()
            self.assertEqual(self.b.balance(), result)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(fetch.call_args_list[0].args[0].get_method(), 'GET')
        self.assertEqual(fetch.call_args_list[0].args[0].full_url, 'https://openrouter.ai/api/v1/key')
        self.assertEqual(result['available_usd'], .75)
        self.assertEqual(set(result), {'currency', 'checked_at', 'available_usd', 'note'})
        self.assertNotIn('SECRET', json.dumps(result))
        self.assertNotIn('test-key', json.dumps(result))
        self.assertEqual(self.b.writer_tool('skok_deneg', {}, self.message(), {}, True),
                         'МАЛО, МНЕ ПИЗДА')

    def test_balance_phrases_cover_thresholds_without_account_metadata(self):
        for amount, prefix in [(0, 'МАЛО,'), (2.99, 'МАЛО,'), (3, 'ДЕНЕГ ТОЛЬКО'),
                               (4.67, 'ДЕНЕГ ТОЛЬКО'), (5, 'ДЕНЕГ ТОЛЬКО'), (5.01, 'ДЕНЕГ ДОХУЯ')]:
            with self.subTest(amount=amount):
                phrase = self.b.balance_phrase({'available_usd': amount, 'checked_at': 123})
                self.assertTrue(phrase.startswith(prefix))
                self.assertNotIn('123', phrase)
        self.assertEqual(self.b.balance_phrase({'error': 'internal metadata'}),
                         'НЕ ЗНАЮ, СКОЛЬКО ОСТАЛОСЬ.')

    def test_only_conversational_tool_names_are_available(self):
        self.assertEqual({t['function']['name'] for t in conversation.TOOLS},
                         {'vspomnit', 'sprosit', 'pochitat', 'che_novogo', 'kto_tut', 'skok_deneg'})
        for name in ('lookup_history', 'search', 'read_url', 'news', 'participants', 'balance'):
            self.assertEqual(self.b.writer_tool(name, {}, self.message(), {}, False),
                             'Неизвестный инструмент.')

    def test_balance_negative_key_allowance_is_zero(self):
        for remaining, expected in ((.2, .2), (-.1, 0)):
            with self.subTest(remaining=remaining):
                self.b._key_balance_cache = None
                with patch('bot.fetch', return_value=json.dumps({'data': {'limit_remaining': remaining}}).encode()):
                    self.assertEqual(self.b.balance()['available_usd'], expected)

    def test_balance_ignores_legacy_cache_that_contains_account_funds(self):
        self.b._balance_cache = {'checked_at': time.time(), 'available_usd': 100, 'account_balance_usd': 100}
        with patch('bot.fetch', return_value=b'{"data":{"limit_remaining":0.5}}'):
            result = self.b.balance()
        self.assertEqual(result['available_usd'], .5)
        self.assertNotIn('account_balance_usd', result)

    def test_unlimited_key_and_malformed_or_missing_balance(self):
        with patch('bot.fetch', return_value=b'{"data":{"limit_remaining":null}}') as fetch:
            self.assertIn('error', self.b.balance())
            fetch.assert_called_once()
            self.assertEqual(fetch.call_args.args[0].full_url, 'https://openrouter.ai/api/v1/key')
        for body in (b'not json', b'{"data":[]}', b'{"data":{"limit_remaining":true}}',
                     b'{"data":{"limit_remaining":NaN}}', b'{"data":{}}'):
            with self.subTest(body=body):
                self.b._key_balance_cache = None
                with patch('bot.fetch', return_value=body) as fetch:
                    with self.assertRaises(RuntimeError):
                        self.b.balance()
                    fetch.assert_called_once()

    def test_balance_refreshes_expired_cache_and_replay_never_fetches(self):
        self.b._key_balance_cache = {'available_usd': 99, 'checked_at': time.time() - 61}
        with patch('bot.fetch', side_effect=[b'{"data":{"limit_remaining":0.1}}', RuntimeError('offline')]) as fetch:
            self.assertEqual(self.b.balance()['available_usd'], .1)
            self.assertEqual(fetch.call_count, 1)
            fetch.reset_mock()
            self.assertIn('error', self.b.balance(live=False))
            fetch.assert_not_called()

    def test_invalid_tool_arguments_reach_writer_as_error(self):
        self.b.writer_step.side_effect = [
            {'role': 'assistant', 'tool_calls': [{'id': 't', 'function': {'name': 'che_novogo', 'arguments': 'invalid'}}]},
            {'role': 'assistant', 'content': 'ладно'}]
        self.turn()
        self.assertIn('недоступен', self.b.writer_step.call_args.args[0][-1]['content'])

    def test_participant_tool_in_replay_never_calls_telegram(self):
        m = self.message()
        snapshot = {'context': ''}
        self.b.writer_tool('kto_tut', {'all': True}, m, snapshot, live=False)
        self.b.api.assert_not_called()

    def test_history_lookup_is_scoped_and_excludes_future_in_replay(self):
        now = time.time()
        parent = self.message('старый вопрос', date=now - 100)
        self.b.history.add_message((-1, 0), parent)
        self.message_id += 1
        self.b.history.add((-1, 0), 2, 'assistant', 'Придумал ник: Ёлкин', now - 90, 1)
        self.b.history.add_message((-1, 0), self.message('соседняя реплика', date=now - 80))
        self.b.history.add_message((-1, 0), self.message('ник БУДУЩЕЕ ПО ВРЕМЕНИ', date=now + 50))
        current = self.message('бот, какой ник?', date=now)
        self.b.history.add_message((-1, 0), current)
        self.b.history.add_message((-1, 0), self.message('ник БУДУЩЕЕ ПО ID', date=now - 10))
        self.b.history.add_message((-2, 0), self.message('ник ДРУГОЙ ЧАТ', chat=-2, date=now - 10))
        self.b.history.add_message((-1, 5), self.message('ник ДРУГАЯ ТЕМА', topic=5, date=now - 10))
        before = self.b.history.db.total_changes
        result = json.loads(self.b.writer_tool('vspomnit', {'query': 'НИК', 'limit': 1, 'chat': -2},
                                             current, {'target': current['message_id'], 'date': now}, live=False))
        self.assertEqual(result['matched_ids'], [2])
        self.assertEqual([m['id'] for m in result['messages']], [1, 2, 3])
        self.assertEqual(result['messages'][1]['role'], 'assistant')
        self.assertEqual(result['messages'][1]['author'], 'Рофл')
        self.assertEqual(self.b.history.db.total_changes, before)
        self.b.api.assert_not_called()
        self.assertEqual(self.b.history.lookup((-1, 0), 'елк', 5, now)['matched_ids'], [2])

    def test_history_lookup_keeps_other_bots_external_and_returns_matching_excerpt(self):
        now = time.time()
        external = self.message('длинно ' * 200 + 'уникальныймаркер найден', date=now - 20)
        external['from'] = {'id': 99, 'first_name': 'oracle', 'is_bot': True}
        self.b.history.add_message((-1, 0), external)
        result = self.b.history.lookup((-1, 0), 'уникальныймаркер', 2, now)
        match = result['messages'][0]
        self.assertEqual(match['role'], 'user')
        self.assertEqual(match['author'], 'oracle (бот)')
        self.assertIn('уникальныймаркер', match['text'])
        self.assertTrue(match['truncated'])
        self.assertGreater(match['excerpt_start'], 0)
        self.assertLessEqual(len(match['text']), 500)

    def test_history_lookup_bounds_results_and_validates_inputs(self):
        now = time.time()
        for i in range(35):
            self.b.history.add_message((-1, 0), self.message('ников много ' + 'x' * 600, date=now - 100 + i))
        result = self.b.history.lookup((-1, 0), 'ник', 36, now, limit=8)
        self.assertEqual(result['total_matches'], 35)
        self.assertEqual(len(result['matched_ids']), 8)
        self.assertLessEqual(len(result['messages']), 20)
        self.assertTrue(all(len(m['text']) <= 500 for m in result['messages']))
        current = self.message()
        for args in ({}, {'query': []}, {'query': 'x' * 201}, {'query': '%_'},
                     {'query': 'ник', 'limit': True}, {'query': 'ник', 'limit': 9}):
            with self.subTest(args=args):
                self.assertIn('error', json.loads(self.b.writer_tool('vspomnit', args, current,
                                                                  {'date': now}, live=False)))
        empty = self.b.history.lookup((-1, 0), 'ничегонепохожего', 36, now)
        self.assertEqual(empty['messages'], [])
        self.assertIn('Удалённая история недоступна', empty['note'])

    def test_author_can_lookup_history_then_continue_without_external_calls(self):
        old = self.message('Мне понравился ник Паяснитель', date=time.time() - 60)
        self.b.history.add_message((-1, 0), old)
        self.b.writer_step.side_effect = [
            {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'h1', 'type': 'function',
                'function': {'name': 'vspomnit', 'arguments': '{"query":"ник"}'}}]},
            {'role': 'assistant', 'content': 'Ты выбрал Паяснителя.'}]
        answer, trace = self.turn(self.message('бот, что мы выбрали?'), live=False)
        self.assertEqual(answer, 'Ты выбрал Паяснителя.')
        self.assertEqual(trace['tools'][0]['tool'], 'vspomnit')
        result = self.b.writer_step.call_args.args[0][-1]
        self.assertEqual(result['role'], 'tool')
        self.assertEqual(result['tool_call_id'], 'h1')
        self.assertIn('Паяснитель', result['content'])
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

    def test_writer_requests_functions_with_high_reasoning_and_records_usage(self):
        response = {'choices': [{'message': {'role': 'assistant', 'content': 'ok'}}],
                    'usage': {'prompt_tokens': 12, 'completion_tokens': 4, 'cost': .02}}
        with self.b.counting() as usage, patch('bot.post', return_value=response) as post:
            answer = bot.Bot.writer_step(self.b, [{'role': 'user', 'content': 'hello'}])
        self.assertEqual(answer['content'], 'ok')
        payload = post.call_args.args[1]
        self.assertEqual(payload['reasoning'], {'effort': 'high'})
        self.assertEqual(payload['tools'], conversation.TOOLS)
        self.assertEqual(usage[0]['output_tokens'], 4)
        self.assertEqual(usage[0]['cost'], .02)

    def test_setting_override_updates_all_current_states_without_changing_attitudes(self):
        self.turn(self.message(date=900))
        self.b.history.add_message((-2, 5), self.message(chat=-2, topic=5))
        store = conversation.Store(self.b.history.db)
        store.set_role_override('Глад Валакас', 1000, 2000)
        self.assertEqual(store.state((-1, 0))['role'], 'Глад Валакас')
        self.assertEqual(store.state((-1, 0))['attitude'], 7)
        self.assertEqual(store.state((-2, 5))['role'], 'Глад Валакас')
        self.assertEqual(store.state((-2, 5))['attitude'], 5)

    def test_jev_and_writer_costs_are_counted_once(self):
        _, first = self.turn()
        self.assertEqual(first['cost'], .001)
        self.assertIsNone(self.b.usage)

    def test_formatter_runs_after_author_and_skips_silence_and_reactions(self):
        self.b.writer_step.return_value = {'role': 'assistant', 'content': 'Исходный ответ'}
        def format_answer(text):
            self.b.usage.append({'cost': .0001, 'stage': 'formatter'})
            return '**Исходный ответ**', {'cost': .0001}
        self.b.format_answer.side_effect = format_answer
        answer, trace = self.turn()
        self.assertEqual(answer, '**Исходный ответ**')
        self.assertEqual(trace['formatter']['cost'], .0001)
        self.assertAlmostEqual(trace['cost'], .0011)
        self.assertNotIn('reaction', trace)
        self.b.format_answer.assert_called_once_with('Исходный ответ')
        self.b.format_answer.reset_mock()
        for action in ('react', 'ignore'):
            self.b.direct.side_effect = lambda snapshot, previous, action=action: {
                **self.decision(snapshot, previous), 'action': action}
            self.turn()
        self.b.format_answer.assert_not_called()

    def test_aliases_link_name_handle_and_id_without_rewriting_message_text(self):
        author = self.message('Это мой бот.')
        author['from'].update(first_name='Andrew', username='oxgrb')
        self.b.history.add_message((-1, 0), author)
        current = self.message('бот, пусть @oxgrb поправит промпт')
        current['from'] = {'id': 99, 'first_name': 'Andrew', 'username': 'another_andrew'}
        # The current participant cache must not alter historical authors or literal mentions.
        self.b.history.remember(-1, {'id': 7, 'first_name': 'Будущее имя', 'username': 'oxgrb'})
        self.turn(current)
        snapshot = self.b.direct.call_args.args[0]
        self.assertIn('user_id=7: Andrew (@oxgrb)', snapshot['participants_context'])
        self.assertIn('user_id=99: Andrew (@another_andrew)', snapshot['participants_context'])
        self.assertIn('Andrew (@oxgrb): Это мой бот.', snapshot['context'])
        self.assertIn('пусть @oxgrb поправит промпт', snapshot['context'])
        self.assertNotIn('Будущее имя', snapshot['context'])
        self.assertIn('Andrew (@another_andrew)', snapshot['event'])

    def test_alias_index_survives_selection_and_is_frozen_on_replay(self):
        author = self.message('Предыдущая реплика')
        author['from'].update(first_name='Andrew', username='oxgrb')
        self.b.history.add_message((-1, 0), author)
        current = self.message('бот, @oxgrb это кто?')
        self.b.select_context.return_value = {'ids': [current['message_id']]}
        self.turn(current)
        snapshot, decision = conversation.Store(self.b.history.db).get((-1, 0), 'message:2:2')
        prompt = self.b.writer_step.call_args.args[0][1]['content']
        self.assertIn('user_id=7: Andrew (@oxgrb)', prompt)
        self.assertNotIn('Предыдущая реплика', prompt)
        expected = copy.deepcopy(self.b.writer_step.call_args.args[0])
        self.b.history.db.execute('DELETE FROM messages')
        self.b.history.remember(-1, {'id': 7, 'first_name': 'Другое имя', 'username': 'oxgrb'})
        self.b.write_turn(current, snapshot, decision, live=False)
        self.assertEqual(self.b.writer_step.call_args.args[0], expected)
        participants = json.loads(self.b.writer_tool('kto_tut', {}, current, snapshot, live=False))
        self.assertEqual(participants['known']['oxgrb'], 'Andrew')
        post = Mock(return_value={'answers': {'1': {'noul': .1}}})
        conversation.select_context(post, 'test-key', snapshot, 2)
        self.assertEqual(post.call_args.args[1]['state']['participants_context'], snapshot['participants_context'])
        self.assertIn('user_id=7: Andrew (@oxgrb)', conversation.payload(snapshot, conversation.Store.initial())['state']['context'])

    def test_alias_history_keeps_same_id_changes_and_separates_chat_from_user(self):
        old = self.message('раньше')
        old['from'].update(first_name='Andrew', username='oxgrb')
        new = self.message('теперь')
        new['from'].update(first_name='Андрей', username='renamed')
        channel = {'id': 7, 'title': 'Новости', 'username': 'news_channel'}
        participants = bot.message_participants(old) + bot.message_participants(new)
        participants += bot.message_participants({'sender_chat': channel})
        text = bot.participant_context(participants, 42)
        self.assertIn('user_id=7: Andrew (@oxgrb) = Андрей (@renamed)', text)
        self.assertIn('chat_id=7: Новости (@news_channel)', text)

    def test_reply_and_lookup_retain_alias_evidence_and_lookup_can_find_handle(self):
        now = time.time()
        author = self.message('Это мой проект', date=now - 2)
        author['from'].update(first_name='Andrew', username='oxgrb')
        self.b.history.add_message((-1, 0), author)
        reply = self.message('бот, поясни', date=now)
        reply['reply_to_message'] = author
        self.assertIn('→ Andrew (@oxgrb)', bot.message_content(reply))
        lookup = self.b.history.lookup((-1, 0), 'oxgrb', reply['message_id'], now)
        self.assertEqual(lookup['matched_ids'], [author['message_id']])
        self.assertEqual(lookup['messages'][0]['author'], 'Andrew (@oxgrb)')
        self.assertEqual(lookup['messages'][0]['participants'][0]['id'], 7)
        mention = {'entities': [{'type': 'text_mention', 'user': author['from']}]}
        self.assertEqual(bot.message_participants(mention)[0]['username'], 'oxgrb')


if __name__ == '__main__':
    unittest.main()
