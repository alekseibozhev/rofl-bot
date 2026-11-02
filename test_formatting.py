import os
import unittest
from unittest.mock import patch

import bot
import formatting


class FormattingTests(unittest.TestCase):
    def setUp(self):
        self.b = bot.Bot.__new__(bot.Bot)
        self.b.openrouter, self.b.openrouter_key, self.b.usage = True, 'test-key', []
        self.env = patch.dict(os.environ, {'FORMATTER_ENABLED': '1', 'FORMATTER_MODEL': formatting.MODEL,
                                          'FORMATTER_STYLE': ''})
        self.env.start()
        self.addCleanup(self.env.stop)

    def response(self, text, finish='stop'):
        return {'choices': [{'message': {'content': text}, 'finish_reason': finish}],
                'usage': {'cost': .0001, 'prompt_tokens': 30, 'completion_tokens': 10}}

    def test_prefix_cleanup_keeps_legitimate_dates_and_mentions(self):
        for prefix in ['Время сообщения: 2026-10-31T20:57:41\n[367] Рофл: ',
                       '[31.10.2026 20:57] [367] Рофл: ', '[367] Рофл: ', 'Рофл → Алексей: ']:
            self.assertEqual(formatting.clean_prefix(prefix + 'Два бакса.'), 'Два бакса.')
        for text in ['31.10.2026 встречаемся.', '[367] — номер заказа.', 'Время сообщения: 2026-10-31T20:57:41']:
            self.assertEqual(formatting.clean_prefix(text), text)

    def test_formatter_gets_only_clean_answer_and_counts_cost(self):
        with patch('bot.post', return_value=self.response('**Два бакса.**\n\n@alex, держись!')) as post:
            answer, info = self.b.format_answer('[123] Рофл: Два бакса. @alex, держись!')
        self.assertEqual(answer, '**Два бакса.**\n\n@alex, держись!')
        payload = post.call_args.args[1]
        self.assertEqual(payload['messages'][1], {'role': 'user', 'content': 'Два бакса. @alex, держись!'})
        self.assertEqual(len(payload['messages']), 2)
        self.assertNotIn('tools', payload)
        self.assertEqual(payload['reasoning'], {'effort': 'low'})
        self.assertEqual(self.b.usage[0]['stage'], 'formatter')
        self.assertEqual(info['cost'], .0001)
        self.assertNotIn('fallback', info)

    def test_semantic_link_code_and_truncation_changes_fall_back(self):
        cases = [('2 бакса', '4 бакса', 'stop'),
                 ('@alex привет', '@bob привет', 'stop'),
                 ('[Источник](https://example.com)', '[Источник](https://evil.com)', 'stop'),
                 ('```py\n  x = 1\n```', '```py\nx = 1\n```', 'stop'),
                 ('Всё готово', 'Всё готово', 'length'),
                 ('Всё готово', '', 'stop'),
                 ('Всё готово', 'Вот ответ: Всё готово', 'stop')]
        for source, candidate, finish in cases:
            with self.subTest(source=source), patch('bot.post', return_value=self.response(candidate, finish)):
                answer, info = self.b.format_answer(source)
                self.assertEqual(answer, source)
                self.assertTrue(info['fallback'])

    def test_paragraphs_bold_and_existing_links_are_accepted(self):
        source = 'Новости. [Источник](https://example.com/?a=1&b=2) <текст>'
        candidate = '**Новости.**\n\n[Источник](https://example.com/?a=1&b=2) <текст>'
        self.assertTrue(formatting.validate(source, candidate, bot.telegram_html))

    def test_failure_is_one_call_and_disabled_formatter_is_free(self):
        with patch('bot.post', side_effect=RuntimeError('HTTP 403')) as post:
            answer, info = self.b.format_answer('[123] Рофл: Готово')
        self.assertEqual(answer, 'Готово')
        self.assertTrue(info['fallback'])
        post.assert_called_once()
        with patch.dict(os.environ, {'FORMATTER_ENABLED': '0'}), patch('bot.post') as post:
            self.assertEqual(self.b.format_answer('Рофл: Готово')[0], 'Готово')
        post.assert_not_called()

    def test_style_changes_only_formatter_system_prompt(self):
        with patch.dict(os.environ, {'FORMATTER_STYLE': 'Без жирного.'}), patch('bot.post', return_value=self.response('Готово')) as post:
            self.b.format_answer('Готово')
        self.assertIn('Без жирного.', post.call_args.args[1]['messages'][0]['content'])

    def test_default_formatting_is_local_and_preserves_markdown_links_and_code(self):
        with patch.dict(os.environ):
            os.environ.pop('FORMATTER_ENABLED', None)
            text = '**Итог**\n\n[Источник](https://example.com)\n```py\n  x = 1\n```'
            with patch('bot.post') as post:
                answer, info = self.b.format_answer('Рофл: ' + text)
            self.assertEqual(answer, text)
            self.assertTrue(info['skipped'])
            self.assertIn('<b>Итог</b>', bot.telegram_html(answer))
            self.assertIn('https://example.com', bot.telegram_html(answer))
            post.assert_not_called()

    def test_invalid_api_shape_does_not_lose_the_authors_answer(self):
        for response in ([], {}, {'usage': [1]}, {'choices': [None]}, {'choices': [{'message': 'bad'}]}):
            with self.subTest(response=response), patch('bot.post', return_value=response):
                answer, info = self.b.format_answer('Рофл: Готово')
                self.assertEqual(answer, 'Готово')
                self.assertTrue(info['fallback'])


if __name__ == '__main__':
    unittest.main()
