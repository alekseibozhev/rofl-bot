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
        self.b.execute_tool = Mock(return_value='{"known":{"andrew":"Andrew"},"identities":[]}')
        self.message = {'chat': {'id': -1}, 'message_id': 1}
        self.snapshot = {'target': 1, 'date': 1000, 'context': 'Кто тут в чате?', 'event': 'message',
                         '_visible_tool_requests': set()}

    def test_repeat_returns_only_rebuke_and_does_not_execute(self):
        self.b.writer_tool('kto_tut', {}, self.message, self.snapshot, True, verdict='execute')
        self.snapshot.update(target=3, date=1001)
        self.assertEqual(self.b.writer_tool('kto_tut', {}, self.message, self.snapshot, True, verdict='repeat'),
                         tooluse.REPEAT)
        self.b.execute_tool.assert_called_once()
        self.assertEqual(self.b.history.db.execute('SELECT count(*) FROM tool_executions').fetchone()[0], 1)

    def test_replay_and_failed_results_do_not_write(self):
        self.b.writer_tool('kto_tut', {}, self.message, self.snapshot, False)
        self.b.execute_tool.return_value = '{"error":"offline"}'
        self.b.writer_tool('kto_tut', {}, self.message, self.snapshot, True)
        self.assertEqual(self.b.history.db.execute('SELECT count(*) FROM tool_executions').fetchone()[0], 0)

    def test_writer_sends_penalty(self):
        self.b.model = 'test'
        with patch('bot.post', return_value={'choices': [{'message': {'content': 'ok'}}]}) as post:
            self.b.writer_step([])
        self.assertEqual(post.call_args.args[1]['repetition_penalty'], 1.1)

    def test_successful_and_clean_query_helpers(self):
        self.assertFalse(tooluse.successful('[]'))
        self.assertFalse(tooluse.successful(json.dumps({'results': []})))
        self.assertFalse(tooluse.successful(json.dumps({'messages': []})))
        self.assertFalse(tooluse.successful('Ничего не найдено.'))
        self.assertTrue(tooluse.successful(json.dumps({'results': [{'url': 'https://example.org'}]})))
        self.assertTrue(tooluse.successful('OK'))
        self.assertEqual(tooluse.clean_query('@bot РОФЛ, ЧЕ ТАМ С ВЫБОРАМИ?! https://x.co/1'), 'че там с выборами')


if __name__ == '__main__':
    unittest.main()
