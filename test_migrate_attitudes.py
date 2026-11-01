import copy
import json
import sqlite3
import unittest

import bot
import conversation
import migrate_attitudes as migration


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.history = bot.History(':memory:')
        self.db = self.history.db
        self.addCleanup(self.db.close)

    def report(self):
        return {'complete': True, 'events': [
            {'chat': -1, 'user_id': 7, 'event': 'message:1', 'date': 100, 'previous': 5, 'attitude': 7},
            {'chat': -1, 'user_id': 7, 'event': 'message:2', 'date': 200, 'previous': 7, 'attitude': 4}]}

    def test_apply_is_repeatable_preserves_live_state_and_supplies_historical_replay(self):
        store = conversation.Store(self.db)
        migration.apply(self.db, self.report())
        migration.apply(self.db, self.report())
        self.assertEqual(store.attitude(-1, 7), 4)
        self.assertEqual(store.attitude(-1, 7, before=100), 5)
        self.assertEqual(store.attitude(-1, 7, before=150), 7)
        self.assertEqual(store.attitude(-1, 7, before=250), 4)
        self.assertEqual(self.db.execute('SELECT count(*) FROM participant_attitude_backfill').fetchone()[0], 2)
        store.save((-1, 8), 'new', {'date': 300}, {
            'state': {**store.initial(), 'updated_at': 300}, 'relationship': {'user_id': 7, 'attitude': 9}})
        migration.apply(self.db, self.report())
        self.assertEqual(store.attitude(-1, 7), 9)
        self.assertEqual(store.attitude(-1, 7, before=350), 9)
        self.assertEqual(self.db.execute('SELECT count(*) FROM conversation_decisions').fetchone()[0], 1)

    def test_conflicting_or_invalid_report_cannot_partially_apply(self):
        report = self.report()
        migration.apply(self.db, report)
        bad = copy.deepcopy(report)
        bad['events'][0]['event'] = 'unseen'
        bad['events'][1]['attitude'] = 6
        with self.assertRaises(ValueError):
            migration.apply(self.db, bad)
        self.assertEqual(self.db.execute('SELECT count(*) FROM participant_attitude_backfill').fetchone()[0], 2)
        bad = self.report()
        bad['events'][1]['previous'] = 2
        with self.assertRaises(ValueError):
            migration.apply(self.db, bad)

    def test_collect_deduplicates_and_never_attributes_quoted_author_to_sender(self):
        self.db.execute('ALTER TABLE messages ADD COLUMN legacy TEXT')
        message = {'message_id': 1, 'date': 100, 'chat': {'id': -1}, 'text': 'бот, привет',
                   'from': {'id': 7, 'first_name': 'Автор'},
                   'reply_to_message': {'message_id': 0, 'from': {'id': 99, 'first_name': 'Другой'}, 'text': 'цитата'}}
        self.history.add_message((-1, 0), message)
        # History retention discards synthetic ancient messages; insert explicitly for extraction.
        self.db.execute('INSERT OR REPLACE INTO messages (chat,topic,id,role,content,date,reply_to,raw) VALUES (-1,0,1,\'user\',NULL,100,0,?)', (json.dumps(message),))
        snapshot = {'messages': [{'id': 1, 'date': '1970-01-01T03:01:40', 'role': 'user',
                                 'content': 'same message', 'participants': bot.message_participants(message)}]}
        self.db.execute('INSERT INTO conversation_decisions VALUES (-1,0,\'old\',100,?,\'{}\')', (json.dumps(snapshot),))
        _, events = migration.collect(self.db, 42)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['user_id'], 7)
        body = migration.request_body(events[0], events, 5, 'test')
        self.assertEqual(set(body['questions']), {'attitude'})
        self.assertEqual(body['state']['attitude_user_id'], 7)

    def test_same_second_events_keep_last_score(self):
        report = self.report()
        report['events'][1]['date'] = 100
        migration.apply(self.db, report)
        store = conversation.Store(self.db)
        self.assertEqual(store.attitude(-1, 7), 4)
        self.assertEqual(store.attitude(-1, 7, before=101), 4)


if __name__ == '__main__':
    unittest.main()
