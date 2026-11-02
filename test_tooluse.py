import json
import sqlite3
import unittest
from unittest.mock import Mock, patch

import bot
import tooluse


class ToolUseTests(unittest.TestCase):
    def setUp(self):
        self.b = bot.Bot.__new__(bot.Bot)
        self.b.tool_judge_enabled = True
        self.b.openrouter_key = 'test'
        self.b.usage = []
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.execute('CREATE TABLE tool_executions (chat,topic,request,date,name,arguments,result)')
        self.b.history = Mock(db=db)
        self.b.execute_tool = Mock(return_value='ДЕНЕГ ТОЛЬКО НА ЕДУ И КОММУНАЛКУ')
        self.message = {'chat': {'id': -1}, 'message_id': 1}
        self.snapshot = {'target': 1, 'date': 1000, 'context': 'Что с деньгами?', 'event': 'message',
                         '_visible_tool_requests': set()}

    def call(self, live=True):
        return self.b.writer_tool('skok_deneg', {}, self.message, self.snapshot, live)

    def test_repeat_returns_only_rebuke_and_does_not_execute(self):
        with patch('bot.tooluse.judge', return_value=('execute', {})):
            self.call()
        self.snapshot.update(target=3, date=1001)
        with patch('bot.tooluse.judge', return_value=('repeat', {})) as judge:
            self.assertEqual(self.call(), tooluse.REPEAT)
            self.assertEqual(len(judge.call_args.args[-1]), 1)
        self.b.execute_tool.assert_called_once()
        self.assertEqual(self.b.history.db.execute('SELECT count(*) FROM tool_executions').fetchone()[0], 1)

    def test_refresh_executes_and_unseen_or_other_chat_evidence_is_excluded(self):
        with patch('bot.tooluse.judge', return_value=('execute', {})) as judge:
            self.call()
            self.snapshot.update(target=3, date=1001, context='Я пополнил, проверь')
            self.call()
            self.assertEqual(self.b.execute_tool.call_count, 2)
            self.snapshot['_visible_tool_requests'] = set()
            self.call()
            self.assertEqual(judge.call_args.args[-1], [])
            self.message['chat']['id'] = -2
            self.snapshot['_visible_tool_requests'] = {1, 3}
            self.call()
            self.assertEqual(judge.call_args.args[-1], [])

    def test_replay_and_failed_results_do_not_write(self):
        with patch('bot.tooluse.judge', return_value=('execute', {})):
            self.call(live=False)
            self.b.execute_tool.return_value = '{"error":"offline"}'
            self.call()
        self.assertEqual(self.b.history.db.execute('SELECT count(*) FROM tool_executions').fetchone()[0], 0)

    def test_judge_failure_executes_and_skip_does_not(self):
        with patch('bot.tooluse.judge', side_effect=RuntimeError('offline')):
            self.call()
        with patch('bot.tooluse.judge', return_value=('skip', {})):
            self.assertEqual(self.call(), tooluse.SKIP)
        self.b.execute_tool.assert_called_once()

    def test_judge_cannot_claim_repeat_without_evidence(self):
        post = Mock(return_value={'answers': {'usefulness': {'choice': 'repeat'}}})
        with self.assertRaises(RuntimeError):
            tooluse.judge(post, 'key', 'skok_deneg', {}, self.snapshot, [])
        self.assertNotIn('repeat', post.call_args.args[1]['questions']['usefulness']['criteria'])

    def test_same_turn_retry_cannot_accuse_user_of_repeating(self):
        post = Mock(return_value={'answers': {'usefulness': {'choice': 'skip'}}})
        recent = [{'request': self.snapshot['target'], 'result': 'already known'}]
        choice, _ = tooluse.judge(post, 'key', 'skok_deneg', {}, self.snapshot, recent)
        self.assertEqual(choice, 'skip')
        self.assertNotIn('repeat', post.call_args.args[1]['questions']['usefulness']['criteria'])

    def test_writer_sends_penalty(self):
        self.b.model = 'test'
        with patch('bot.post', return_value={'choices': [{'message': {'content': 'ok'}}]}) as post:
            self.b.writer_step([])
        self.assertEqual(post.call_args.args[1]['repetition_penalty'], 1.1)
