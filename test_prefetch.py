import copy
import json
import sqlite3
import unittest
from unittest.mock import Mock, patch

import bot
import chat_context
import conversation
import tooluse


class PrefetchTests(unittest.TestCase):
    def setUp(self):
        self.b = bot.Bot.__new__(bot.Bot)
        self.b.tool_judge_enabled = True
        self.b.openrouter = True
        self.b.openrouter_key = 'test'
        self.b.usage = []
        db = sqlite3.connect(':memory:')
        db.execute('CREATE TABLE tool_executions (chat,topic,request,date,name,arguments,result)')
        self.addCleanup(db.close)
        self.b.history = Mock(db=db)
        self.b.execute_tool = Mock(return_value='УЧАСТНИКИ ЧАТА')
        self.message = {'chat': {'id': -1}, 'message_id': 3}
        self.snapshot = {'target': 3, 'through': 3, 'date': 1000, 'event': 'сообщение 3', 'context': 'old',
                         'targets': [{'id': 3, 'label': 'проверь деньги'}],
                         'messages': [
            {'id': 1, 'role': 'user', 'date': '2026-11-02T09:00:00', 'reply_to': None,
             'content': '[1] Andrew: Что с деньгами?', 'message_text': 'Что с деньгами?', 'author': 'Andrew'},
            {'id': 2, 'role': 'assistant', 'date': '2026-11-02T09:00:01', 'reply_to': 1,
             'content': '[2] Рофл: ответ', 'assistant_text': 'ответ'},
            {'id': 3, 'role': 'user', 'date': '2026-11-02T09:01:00', 'reply_to': 2,
             'content': '[3] Andrew: @test_bot проверь деньги', 'message_text': '@test_bot проверь деньги', 'author': 'Andrew'}]}
        self.decision = {'action': 'reply', 'target': 3, 'context_selection': {'ids': [1, 2, 3]}}

    def responses(self, choose):
        def post(url, payload, **kwargs):
            return {'answers': {name: {'choice': choose(name, q)} for name, q in payload['questions'].items()},
                    'usage': {'cost': .001}}
        return post

    def test_router_chooses_one_batch_of_exact_arguments_and_rejects_invented_choice(self):
        post = Mock(side_effect=self.responses(lambda name, q: 'call_0' if name == 'kto_tut' else 'skip'))
        picks, stats = tooluse.route(post, 'test', self.snapshot, self.decision, [])
        self.assertEqual(picks, [{'tool': 'kto_tut', 'arguments': {}, 'status': 'execute'}])
        post.assert_called_once()
        self.assertEqual(stats['cost'], .001)
        self.assertNotIn('repeat', post.call_args.args[1]['questions']['kto_tut']['criteria'])
        with self.assertRaises(RuntimeError):
            tooluse.route(self.responses(lambda name, q: 'invented'), 'test', self.snapshot, self.decision, [])

    def test_skipped_tools_are_not_executed_and_selection_cost_is_counted(self):
        with patch('bot.post', side_effect=self.responses(lambda name, q: 'skip')):
            entries, meta = self.b.prepare_tools(self.message, self.snapshot, self.decision, True)
        self.assertEqual(entries, [])
        self.b.execute_tool.assert_not_called()
        self.assertEqual(len(meta['stages']), 1)
        self.assertEqual(self.b.usage[0]['cost'], .001)

    def test_prefetch_executes_once_without_second_judge_and_saves_success(self):
        with patch('bot.post', side_effect=self.responses(lambda name, q: 'call_0' if name == 'kto_tut' else 'skip')), \
                patch('bot.tooluse.judge', side_effect=AssertionError('No second judge')):
            entries, _ = self.b.prepare_tools(self.message, self.snapshot, self.decision, True)
        self.b.execute_tool.assert_called_once()
        self.assertEqual(entries[0]['result'], 'УЧАСТНИКИ ЧАТА')
        self.assertEqual(self.b.history.db.execute('SELECT request FROM tool_executions').fetchall(), [(3,)])

    def test_repeat_is_based_on_visible_past_calls_of_same_chat_and_does_not_execute(self):
        rows = [(-1, 0, 1, 900, 'kto_tut', '{}', 'old'),
                (-2, 0, 1, 900, 'kto_tut', '{}', 'OTHER_CHAT'),
                (-1, 4, 1, 900, 'kto_tut', '{}', 'OTHER_TOPIC'),
                (-1, 0, 5, 1100, 'kto_tut', '{}', 'FUTURE'),
                (-1, 0, 0, 900, 'kto_tut', '{}', 'UNSEEN')]
        self.b.history.db.executemany('INSERT INTO tool_executions VALUES (?,?,?,?,?,?,?)', rows)
        post = Mock(side_effect=self.responses(lambda name, q: 'repeat' if name == 'kto_tut' else 'skip'))
        with patch('bot.post', post):
            entries, _ = self.b.prepare_tools(self.message, self.snapshot, self.decision, True)
        self.b.execute_tool.assert_not_called()
        self.assertEqual(entries[0]['result'], tooluse.REPEAT)
        self.assertEqual([r['result'] for r in post.call_args.args[1]['state']['previous_successful_calls']], ['old'])

    def test_past_results_are_scoped_to_selected_sources_and_time(self):
        rows = [(-1, 0, 1, 800, 'kto_tut', '{}', 'OLDER'),
                (-1, 0, 1, 900, 'kto_tut', '{}', 'SOURCE_TRUTH'),
                (-1, 0, 1, 1100, 'kto_tut', '{}', 'FUTURE_SAME_SOURCE'),
                (-1, 0, 1, 950, 'sprosit', '{"query":"q"}', '{"error":"offline"}'),
                (-2, 0, 1, 900, 'kto_tut', '{}', 'OTHER_CHAT'),
                (-1, 8, 1, 900, 'kto_tut', '{}', 'OTHER_TOPIC'),
                (-1, 0, 9, 900, 'kto_tut', '{}', 'UNSELECTED_SOURCE')]
        self.b.history.db.executemany('INSERT INTO tool_executions VALUES (?,?,?,?,?,?,?)', rows)
        result = self.b.past_tool_results(self.message, self.snapshot, self.decision)
        self.assertEqual([e['result'] for e in result], ['SOURCE_TRUTH'])
        self.assertEqual(result[0]['request'], 1)
        self.assertEqual(result[0]['date'], 900)
        self.assertEqual(result[0]['status'], 'saved')
        self.decision['context_selection']['ids'] = [3]
        self.assertEqual(self.b.past_tool_results(self.message, self.snapshot, self.decision), [])
        self.b.execute_tool.assert_not_called()

    def test_saved_result_keeps_its_original_time_and_source_in_author_context(self):
        history = {'tool': 'kto_tut', 'arguments': {}, 'result': 'SOURCE_TRUTH',
                   'status': 'saved', 'request': 1, 'date': 900}
        self.b.writer_step = Mock(return_value={'role': 'assistant', 'content': 'пояснение'})
        decision = {**self.decision, 'tool_results': [], 'tool_history': [history]}
        original = copy.deepcopy(decision)
        self.b.write_turn(self.message, self.snapshot, decision, False)
        self.b.writer_step.assert_called_once()
        prompt = self.b.writer_step.call_args.args[0][-1]['content']
        self.assertIn('SOURCE_TRUTH', prompt)
        self.assertIn('для сообщения #1', prompt)
        self.assertIn('Проверка ' + chat_context.stamp(900), prompt)
        self.assertEqual(decision, original)
        self.b.execute_tool.assert_not_called()
        # The earlier explicit repeat response still suppresses re-sending that result.
        decision['tool_results'] = [{'tool': 'kto_tut', 'status': 'repeat', 'arguments': {}, 'result': tooluse.REPEAT}]
        self.b.write_turn(self.message, self.snapshot, decision, False)
        prompt = self.b.writer_step.call_args.args[0][-1]['content']
        self.assertNotIn('SOURCE_TRUTH', prompt)
        self.assertIn(tooluse.REPEAT, prompt)

    def test_search_can_be_followed_by_one_jev_selected_page_before_author(self):
        self.b.execute_tool.side_effect = [json.dumps({'results': [{'title': 'Source', 'url': 'https://example.org/fact',
                                                                    'snippet': 'incomplete'}]}),
                                          json.dumps({'url': 'https://example.org/fact', 'text': 'Full source'})]
        posts = []
        def route_post(url, payload, **kwargs):
            posts.append(copy.deepcopy(payload))
            tool = 'sprosit' if len(posts) == 1 else 'pochitat'
            return self.responses(lambda name, q: 'call_0' if name == tool else 'skip')(url, payload)
        with patch('bot.post', side_effect=route_post):
            entries, _ = self.b.prepare_tools(self.message, self.snapshot, self.decision, False)
        self.assertEqual([e['tool'] for e in entries], ['sprosit', 'pochitat'])
        self.assertEqual(entries[-1]['arguments'], {'url': 'https://example.org/fact'})
        self.assertEqual(set(posts[-1]['questions']), {'pochitat'})
        self.assertEqual(self.b.history.db.execute('SELECT count(*) FROM tool_executions').fetchone()[0], 0)

    def test_failed_router_and_failed_tool_are_visible_as_errors_without_fake_data(self):
        with patch('bot.post', side_effect=RuntimeError('offline')):
            entries, stages = self.b.prepare_tools(self.message, self.snapshot, self.decision, False)
        self.assertEqual(entries[0]['status'], 'error')
        self.assertTrue(stages['stages'][0]['fallback'])
        self.b.execute_tool.side_effect = RuntimeError('offline')
        with patch('bot.post', side_effect=self.responses(lambda name, q: 'call_0' if name == 'kto_tut' else 'skip')):
            entries, _ = self.b.prepare_tools(self.message, self.snapshot, self.decision, True)
        self.assertEqual(entries[0]['status'], 'error')
        self.assertEqual(self.b.history.db.execute('SELECT count(*) FROM tool_executions').fetchone()[0], 0)

    def test_candidate_arguments_use_selected_text_and_keep_explicit_urls(self):
        self.snapshot['messages'].insert(0, dict(self.snapshot['messages'][0], id=0, message_text='SECRET https://secret.example/'))
        self.snapshot['messages'][-1]['message_text'] = '@test_bot прочитай https://example.org/a?q=1&b=2'
        choices = tooluse.candidates(self.snapshot, self.decision)
        self.assertNotIn('SECRET', json.dumps(choices))
        self.assertEqual(choices['pochitat'], [{'url': 'https://example.org/a?q=1&b=2'}])

    def test_larger_context_preserves_relevant_ancestors_but_drops_unrelated_messages(self):
        turns = copy.deepcopy(self.snapshot['messages'])
        turns[-1]['reply_to'] = None
        turns.append(dict(turns[-1], id=4, message_text='другая тема'))
        snapshot = {**self.snapshot, 'messages': turns}
        response = {'answers': {'1': {'noul': .1}, '2': {'noul': .6}, '4': {'noul': .1}}}
        selected = conversation.select_context(Mock(return_value=response), 'test', snapshot, 3)
        self.assertEqual(selected['ids'], [1, 2, 3])
        self.assertEqual(selected['threshold'], .5)

    def test_context_selection_avoids_topic_switch_ancestors_and_orphaned_bot_replies(self):
        turns = [{'id': i, 'role': ('assistant' if i % 2 == 0 else 'user'), 'date': '2026-11-04T12:00:00',
                  'reply_to': (i - 1 if i > 1 else None),
                  'content': f'[{i}] Msg {i}', 'message_text': f'msg {i}', 'author': 'User'}
                 for i in range(1, 26)]
        snapshot = {**self.snapshot, 'target': 25, 'through': 25, 'messages': turns}
        # Topic switch: low scores keep only mandatory target + immediate reply parent (24, 25).
        low_scores = {'answers': {str(i): {'noul': .1} for i in range(1, 25)}}
        selected = conversation.select_context(Mock(return_value=low_scores), 'test', snapshot, 25)
        self.assertEqual(selected['ids'], [24, 25])
        # High scores stay capped at CONTEXT_MAX_MESSAGES and never add a non-mandatory assistant without its parent.
        high_scores = {'answers': {str(i): {'noul': .9} for i in range(1, 25)}}
        capped = conversation.select_context(Mock(return_value=high_scores), 'test', snapshot, 25)
        self.assertLessEqual(len(capped['ids']), conversation.CONTEXT_MAX_MESSAGES)
        kept = set(capped['ids'])
        for mid in kept - {24, 25}:
            turn = turns[mid - 1]
            if turn['role'] == 'assistant':
                self.assertIn(turn['reply_to'], kept)

    def test_past_tool_results_excludes_low_score_transitive_ancestors(self):
        self.b.history.db.execute(
            'INSERT INTO tool_executions VALUES (?,?,?,?,?,?,?)',
            (-1, 0, 1, 900, 'kto_tut', '{}', 'УЧАСТНИКИ ЧАТА'))
        decision = {'action': 'reply', 'target': 3,
                    'context_selection': {'ids': [1, 2, 3], 'threshold': .5, 'scores': {'1': .1, '2': .2}}}
        # Message 2 is immediate reply parent of 3, so its reply_to=1 is only included if 1 or 2 is relevant.
        # Move target's reply_to to None so neither 1 nor 2 is an immediate parent of 3.
        snap = copy.deepcopy(self.snapshot)
        snap['messages'][-1]['reply_to'] = None
        self.assertEqual(self.b.past_tool_results(self.message, snap, decision), [])
        decision['context_selection']['scores']['1'] = .8
        self.assertEqual(len(self.b.past_tool_results(self.message, snap, decision)), 1)

    def test_empty_search_results_are_not_saved_or_followed_by_pochitat(self):
        self.b.execute_tool.return_value = '[]'
        with patch('bot.post', side_effect=self.responses(lambda name, q: 'call_0' if name == 'che_novogo' else 'skip')) as post:
            entries, _ = self.b.prepare_tools(self.message, self.snapshot, self.decision, True)
        self.assertEqual(post.call_count, 1)  # No second pochitat call on empty results.
        self.assertEqual(entries[0]['status'], 'empty')
        self.assertEqual(self.b.history.db.execute('SELECT count(*) FROM tool_executions').fetchone()[0], 0)
        rendered = chat_context.tool_result(entries[0])
        self.assertIn('ничего не найдено', rendered.lower())
        empty_search = chat_context.tool_result({'tool': 'sprosit', 'arguments': {'query': 'q'},
                                                 'status': 'empty', 'result': json.dumps({'results': []})})
        self.assertIn('Ничего не найдено.', empty_search)

    def test_query_rewrite_normalizes_and_caches_for_web_tools_with_fallback(self):
        self.b.model = 'test-model'
        self.snapshot['messages'][-1]['message_text'] = 'ЧЕ ТАМ С ВЫБОРАМИ ЕБАНЫМИ В АМЕРИКЕ ДОЛОЖИ'
        self.b.execute_tool.side_effect = [
            json.dumps({'results': [{'title': 'News', 'url': 'https://example.org/us', 'snippet': 'ok'}]}),
            json.dumps([{'title': 'News', 'url': 'https://example.org/us'}]),
        ]
        calls = []
        def fake_post(url, payload, **kwargs):
            calls.append((url, payload))
            if 'decisions' in url:
                return self.responses(
                    lambda name, q: 'call_0' if name == 'sprosit' else ('call_1' if name == 'che_novogo' else 'skip'))(url, payload)
            return {'choices': [{'message': {'content': 'выборы США ноябрь 2026'}}],
                    'usage': {'cost': .0002, 'prompt_tokens': 40, 'completion_tokens': 8}}
        with patch('bot.post', side_effect=fake_post):
            entries, meta = self.b.prepare_tools(self.message, self.snapshot, self.decision, False)
        rewrite_calls = [p for u, p in calls if 'chat/completions' in u]
        self.assertEqual(len(rewrite_calls), 1)  # Shared between sprosit and che_novogo.
        self.assertEqual(entries[0]['arguments'], {'query': 'выборы США ноябрь 2026'})
        self.assertEqual(entries[1]['arguments'], {'query': 'выборы США ноябрь 2026'})
        self.assertTrue(any(s.get('stage') == 'query_rewrite' for s in meta['stages']))
        # Fallback to clean_query when rewrite fails.
        self.b.execute_tool.side_effect = None
        self.b.execute_tool.return_value = json.dumps({'results': []})
        def failing_rewrite(url, payload, **kwargs):
            if 'decisions' in url:
                return self.responses(lambda name, q: 'call_0' if name == 'sprosit' else 'skip')(url, payload)
            raise RuntimeError('offline')
        with patch('bot.post', side_effect=failing_rewrite):
            entries, _ = self.b.prepare_tools(self.message, self.snapshot, self.decision, False)
        self.assertEqual(entries[0]['arguments']['query'], 'че там с выборами ебаными в америке доложи')

    def test_presentation_excludes_unselected_names_and_keeps_literal_body(self):
        self.snapshot['participants'] = [
            {'kind': 'user', 'id': 7, 'name': 'Andrew', 'username': 'andrew'},
            {'kind': 'user', 'id': 99, 'name': 'UNRELATED', 'username': 'outsider'}]
        self.snapshot['messages'][-1]['participants'] = [self.snapshot['participants'][0]]
        people = chat_context.participants(self.snapshot, self.snapshot['messages'])
        self.assertEqual([p['id'] for p in people], [7])
        literal = '```py\nx = 1\n```\nРофл: это слова человека.'
        self.snapshot['messages'][-1]['message_text'] = literal
        result = chat_context.messages(self.snapshot, self.decision, '', 'Ответь на #3')
        self.assertIn(literal, result[-1]['content'])
        self.assertNotIn('reply_to', json.dumps(result))
        self.assertIn('02.11.2026 09:01 · #3 · Andrew → #2', result[-1]['content'])

    def test_only_reactions_to_selected_messages_reach_author(self):
        self.snapshot['reactions'] = [[2, 7, '👍'], [999, 88, 'SECRET_EMOJI']]
        messages = chat_context.messages(self.snapshot, self.decision, '', 'Ответь')
        self.assertIn('#2 · участник 7: 👍', messages[-1]['content'])
        self.assertNotIn('SECRET_EMOJI', json.dumps(messages))

    def test_hidden_telegram_links_are_available_without_rewriting(self):
        self.snapshot['messages'][-1]['message_text'] = 'прочитай статью'
        self.snapshot['messages'][-1]['urls'] = ['https://example.org/hidden?a=1&b=2']
        choices = tooluse.candidates(self.snapshot, self.decision)
        self.assertEqual(choices['pochitat'], [{'url': 'https://example.org/hidden?a=1&b=2'}])
        messages = chat_context.messages(self.snapshot, self.decision, '', 'Ответь')
        self.assertIn('https://example.org/hidden?a=1&b=2', messages[-1]['content'])

    def test_tool_renderer_preserves_snippets_links_and_truncation(self):
        entry = {'tool': 'sprosit', 'arguments': {'query': 'q'}, 'status': 'execute',
                 'result': json.dumps({'results': [{'title': 'Title', 'url': 'https://example.org/?a=1&b=2',
                                                    'snippet': 'Source snippet'}]})}
        result = chat_context.tool_result(entry)
        self.assertIn('Source snippet', result)
        self.assertIn('https://example.org/?a=1&b=2', result)
        self.assertNotIn('"results":', result)
        entry.update(tool='pochitat', result=json.dumps({'url': 'https://example.org', 'text': 'text', 'truncated': True}))
        self.assertIn('обрезан', chat_context.tool_result(entry))


if __name__ == '__main__':
    unittest.main()
