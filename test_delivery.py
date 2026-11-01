import json
from contextlib import closing
import signal
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import bot


class DeliveryTests(unittest.TestCase):
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
        self.b.api = Mock(return_value={'message_id': 100, 'date': time.time()})
        self.b.send_typing = Mock()
        self.b.generate = Mock(return_value='Рофл')
        self.b.direct = Mock(side_effect=lambda s, p: {
            'action': 'reply', 'target': s['target'], 'attitude': 5, 'reconsider': False, 'addressed': True})
        self.b.select_context = Mock(return_value={'fallback': True})
        self.b.writer_step = Mock(return_value={'role': 'assistant', 'content': 'Готовый ответ'})
        self.b.format_answer = Mock(side_effect=lambda a: (a, {}))
        self.message = {'chat': {'id': -1, 'type': 'group'}, 'message_id': 1, 'date': time.time(),
                        'from': {'id': 7, 'first_name': 'Автор'}, 'text': 'бот, привет'}
        self.update = {'update_id': 10, 'message': self.message}

    def restart(self):
        self.b.history.db.close()
        self.b.history = bot.History(self.path)

    def stage_input(self):
        self.b.history.delivery.enqueue([self.update])
        return self.b.history.delivery.pending()

    def test_poll_commits_update_before_next_poll_can_acknowledge_it(self):
        def api(method, **kwargs):
            if kwargs['offset'] == 0:
                return [self.update]
            with closing(bot.sqlite3.connect(self.path)) as db:
                self.assertEqual(db.execute('SELECT id FROM telegram_inbox').fetchone()[0], 10)
                self.assertEqual(db.execute("SELECT value FROM state WHERE key='offset'").fetchone()[0], 11)
            raise KeyboardInterrupt
        self.b.api.side_effect = api
        first = self.b.poll(0, 30)
        with self.assertRaises(KeyboardInterrupt):
            self.b.collect(first)
        self.restart()
        self.b.api.side_effect = None
        self.b.process_updates(self.b.history.delivery.pending())
        self.assertEqual(self.b.history.delivery.pending(), [])
        self.assertTrue(self.b.history.replied((-1, 0), 1))

    def test_interrupted_generation_resumes_after_restart(self):
        updates = self.stage_input()
        self.b.writer_step.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.b.process_updates(updates)
        self.assertEqual(len(self.b.history.delivery.pending()), 1)
        self.b.api.assert_not_called()
        self.restart()
        self.b.writer_step.side_effect = None
        self.b.process_updates(self.b.history.delivery.pending())
        self.b.direct.assert_called_once()  # Saved Jev decision is reused.
        self.b.api.assert_called_once()
        self.assertFalse(self.b.history.delivery.pending())

    def test_generation_failure_waits_for_retry_and_does_not_advance_completion(self):
        self.b.writer_step.side_effect = RuntimeError('offline')
        self.b.process_updates(self.stage_input())
        self.assertEqual(self.b.history.db.execute('SELECT done FROM telegram_inbox').fetchone()[0], 0)
        self.assertFalse(self.b.history.delivery.pending())
        self.b.writer_step.side_effect = None
        self.b.history.db.execute('UPDATE telegram_inbox SET retry_at=0')
        self.b.process_updates(self.b.history.delivery.pending())
        self.b.api.assert_called_once()

    def test_ready_answer_survives_restart_without_regeneration(self):
        with patch.object(self.b, 'dispatch', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.b.process_updates(self.stage_input())
        self.assertEqual(self.b.history.delivery.outgoing((-1, 0), 'request:1')['status'], 'pending')
        self.assertFalse(self.b.history.replied((-1, 0), 1))
        self.restart()
        self.b.process_updates(self.b.history.delivery.pending())
        self.b.writer_step.assert_called_once()
        self.b.api.assert_called_once()
        self.assertEqual(self.b.history.answer(-1, 100)[1], 'Готовый ответ')

    def test_receipt_survives_failure_to_record_history_without_resending(self):
        with patch.object(self.b.history, 'add', side_effect=RuntimeError('disk')):
            with self.assertRaises(RuntimeError):
                self.b.deliver(self.message, 'Готовый ответ', {'target': 1}, request=1)
        self.assertEqual(self.b.history.delivery.outgoing((-1, 0), 'request:1')['status'], 'delivered')
        self.restart()
        for item in self.b.history.delivery.ready():
            self.b.dispatch(item)
        self.b.api.assert_called_once()
        self.assertTrue(self.b.history.replied((-1, 0), 1))
        self.assertEqual(self.b.history.delivery.outgoing((-1, 0), 'request:1')['status'], 'done')

    def test_ambiguous_send_is_saved_but_never_automatically_duplicated(self):
        self.b.api.side_effect = RuntimeError('network timeout')
        self.b.process_updates(self.stage_input())
        self.restart()
        self.b.history.delivery.recover()
        self.b.history.db.execute('UPDATE telegram_inbox SET retry_at=0')
        self.b.process_updates(self.b.history.delivery.pending())
        self.b.api.assert_called_once()
        item = self.b.history.delivery.outgoing((-1, 0), 'request:1')
        self.assertEqual(item['status'], 'unknown')
        self.assertEqual(item['payload']['answer'], 'Готовый ответ')
        self.assertEqual(self.b.history.delivery.ready(), [])

    def test_process_killed_during_send_recovers_as_unknown(self):
        item = self.b.history.delivery.stage((-1, 0), 'request:1', {'answer': 'saved'})
        self.b.history.delivery.status(item['key'], item['event'], 'sending')
        self.restart()
        self.assertEqual(self.b.history.delivery.recover(), 1)
        self.assertEqual(self.b.history.delivery.outgoing((-1, 0), 'request:1')['status'], 'unknown')

    def test_shutdown_during_generation_finishes_delivery(self):
        def writer(*args, **kwargs):
            self.b.request_stop(signal.SIGTERM, None)
            return {'role': 'assistant', 'content': 'Законченный ответ'}
        self.b.writer_step.side_effect = writer
        self.b.process_updates(self.stage_input())
        self.assertTrue(self.b.stopping)
        self.b.api.assert_called_once()
        self.assertFalse(self.b.history.delivery.pending())
        with self.assertRaises(KeyboardInterrupt):
            self.b.request_stop(signal.SIGINT, None)

    def test_actual_sigterm_handler_does_not_interrupt_current_answer(self):
        previous = signal.signal(signal.SIGTERM, self.b.request_stop)
        self.addCleanup(signal.signal, signal.SIGTERM, previous)
        def writer(*args, **kwargs):
            os.kill(os.getpid(), signal.SIGTERM)
            return {'role': 'assistant', 'content': 'Ответ после сигнала'}
        self.b.writer_step.side_effect = writer
        self.b.process_updates(self.stage_input())
        self.assertTrue(self.b.stopping)
        self.assertEqual(self.b.history.answer(-1, 100)[1], 'Ответ после сигнала')
        self.assertEqual(self.b.history.delivery.pending(), [])

    def test_reaction_receipt_does_not_duplicate_metrics_after_partial_completion(self):
        trace = {'target': 1, 'reaction': True, 'event': 'reaction:test'}
        with patch.object(self.b.history, 'claim_reply', side_effect=RuntimeError('disk')):
            with self.assertRaises(RuntimeError):
                self.b.deliver(self.message, '👍', trace, request=1)
        for item in self.b.history.delivery.ready():
            self.b.dispatch(item)
        self.assertEqual(self.b.history.db.execute('SELECT count(*) FROM answers').fetchone()[0], 1)
        self.b.api.assert_called_once()

    def test_followups_remain_pending_when_the_primary_generation_fails(self):
        followup = dict(self.message, message_id=2, text='бот, ещё деталь', date=self.message['date'] + 1)
        self.b.history.delivery.enqueue([self.update, {'update_id': 11, 'message': followup}])
        self.b.writer_step.side_effect = RuntimeError('offline')
        self.b.process_updates(self.b.history.delivery.pending())
        self.assertEqual(self.b.history.db.execute('SELECT sum(done) FROM telegram_inbox').fetchone()[0], 0)
        self.assertFalse(self.b.history.replied((-1, 0), 2))
        self.b.writer_step.side_effect = None
        self.b.history.db.execute('UPDATE telegram_inbox SET retry_at=0')
        self.b.process_updates(self.b.history.delivery.pending())
        self.b.api.assert_called_once()
        self.assertTrue(self.b.history.replied((-1, 0), 2))

    def test_old_pending_request_uses_reception_time_and_duplicate_poll_does_not_reopen_it(self):
        self.message['date'] -= 2 * 86400
        self.stage_input()
        self.b.history.db.execute('UPDATE telegram_inbox SET received=?', (self.message['date'] + 1,))
        self.b.process_updates(self.b.history.delivery.pending())
        self.b.api.assert_called_once()
        self.b.history.delivery.enqueue([self.update])
        self.assertEqual(self.b.history.delivery.pending(), [])
        self.assertEqual(self.b.history.stamp('offset'), 11)

    def test_read_only_never_sends_ready_outbox(self):
        with patch.object(self.b, 'dispatch'):
            self.b.deliver(self.message, 'Готовый ответ', {'target': 1}, request=1)
        self.b.read_only = True
        self.b.dispatch(self.b.history.delivery.outgoing((-1, 0), 'request:1'))
        self.b.api.assert_not_called()
        self.assertEqual(self.b.history.delivery.outgoing((-1, 0), 'request:1')['status'], 'pending')


if __name__ == '__main__':
    unittest.main()
