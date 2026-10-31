from email.message import Message
from io import BytesIO
import http.client
import json
import os
import socket
import unittest
from unittest.mock import Mock, patch

import bot
import retrieval


class SocketFixture:
    def __init__(self, data):
        self.data = data

    def makefile(self, *args, **kwargs):
        return BytesIO(self.data)


def http_fixture(body, status=200, content_type='text/html; charset=utf-8', headers=''):
    if isinstance(body, str):
        body = body.encode()
    raw = (f'HTTP/1.1 {status} Test\r\nContent-Type: {content_type}\r\nContent-Length: {len(body)}\r\n'
           + headers + '\r\n').encode() + body
    response = http.client.HTTPResponse(SocketFixture(raw))
    response.begin()
    return response


class RetrievalTests(unittest.TestCase):
    def test_search_uses_fixed_local_searxng_without_environment_proxy(self):
        fixture = {'results': [{'title': 'Глад', 'url': 'https://example.org/glad', 'content': '<b>Пранки</b>'}]}
        opener = Mock()
        opener.open.return_value = http_fixture(json.dumps(fixture), content_type='application/json')
        with patch.dict(os.environ, {'SEARXNG_URL': 'http://127.0.0.1:8888'}), patch('retrieval.build_opener', return_value=opener) as build:
            result = retrieval.search('Глад Валакас')
        request = opener.open.call_args.args[0]
        self.assertIn('127.0.0.1:8888/search?', request.full_url)
        self.assertIn('format=json', request.full_url)
        self.assertEqual(build.call_args.args[0].proxies, {})
        self.assertEqual(result['results'][0]['snippet'], 'Пранки')

    def test_search_filters_duplicates_nonpublic_literals_and_limits_size(self):
        results = [{'title': 'x', 'url': url, 'content': 'a' * 2000} for url in
                   ['http://127.0.0.1/a', 'file:///etc/passwd', 'https://example.org/a', 'https://example.org/a']
                   + [f'https://example.org/{n}' for n in range(10)]]
        opener = Mock()
        opener.open.return_value = http_fixture(json.dumps({'results': results}), content_type='application/json')
        with patch('retrieval.build_opener', return_value=opener):
            result = retrieval.search('test')
        self.assertEqual(len(result['results']), 5)
        self.assertEqual(len(result['results'][0]['snippet']), 700)
        self.assertTrue(result['untrusted_data'])

    def test_search_failure_has_no_paid_fallback(self):
        b = bot.Bot.__new__(bot.Bot)
        b.generate = Mock(side_effect=AssertionError('A search must not call a model'))
        with patch('retrieval.search', side_effect=RuntimeError('offline')) as search:
            with self.assertRaises(RuntimeError):
                b.find('Глад')
        search.assert_called_once_with('Глад')
        b.generate.assert_not_called()

    def test_find_works_without_openrouter_and_returns_raw_results(self):
        b = bot.Bot.__new__(bot.Bot)
        b.openrouter = False
        b.generate = Mock(side_effect=AssertionError('A search must not call a model'))
        result = {'results': [{'title': 'x', 'url': 'https://example.org', 'snippet': 'данные'}]}
        with patch('retrieval.search', return_value=result):
            self.assertEqual(json.loads(b.find('test')), result)
        b.generate.assert_not_called()

    def test_searxng_malformed_and_oversized_responses_fail_explicitly(self):
        for body in ['<html>not JSON</html>', '{"results":{}}', 'x' * (retrieval.MAX_BYTES + 1)]:
            opener = Mock()
            opener.open.return_value = http_fixture(body, content_type='application/json')
            with self.subTest(size=len(body)), patch('retrieval.build_opener', return_value=opener), self.assertRaises(RuntimeError):
                retrieval.search('test')

    def test_searxng_endpoint_cannot_come_from_model_or_redirect(self):
        with patch.dict(os.environ, {'SEARXNG_URL': 'https://external.example'}), self.assertRaises(RuntimeError):
            retrieval.search('test')
        with self.assertRaises(RuntimeError):
            retrieval.NoRedirect().redirect_request(None, None, 302, '', {}, 'http://127.0.0.1/private')

    def test_reader_extracts_main_text_and_drops_navigation_and_scripts(self):
        response = http_fixture('<html><head><title>Статья</title></head><body><nav>меню</nav>'
                                '<main><h1>Заголовок</h1><p>Текст &amp; смысл</p><script>steal()</script></main>'
                                '<footer>подвал</footer></body></html>')
        with patch('retrieval.open_public', return_value=(Mock(), response)):
            result = retrieval.read_url('https://example.org/article')
        self.assertEqual(result['title'], 'Статья')
        self.assertEqual(result['text'], 'Заголовок\nТекст & смысл')
        self.assertNotIn('меню', result['text'])
        self.assertNotIn('steal', result['text'])

    def test_reader_prefers_aria_main_over_page_chrome(self):
        page = '<div>navigation</div><div role="main"><p>Article</p></div><div>other</div>'
        with patch('retrieval.open_public', return_value=(Mock(), http_fixture(page))):
            self.assertEqual(retrieval.read_url('https://example.org')['text'], 'Article')

    def test_reader_extracts_public_telegram_posts_without_sidebar(self):
        response = http_fixture('<html><title>Новости</title><div class="sidebar">Описание канала</div>'
                                '<div class="tgme_widget_message_text js-message_text">Первый<br>пост</div>'
                                '<div class="tgme_widget_message_text">Второй <b>пост</b></div></html>')
        with patch('retrieval.open_public', return_value=(Mock(), response)):
            result = retrieval.read_url('https://t.me/s/example')
        self.assertEqual(result['text'], 'Первый\nпост\nВторой пост')
        self.assertNotIn('Описание канала', result['text'])

    def test_reader_has_independent_path_when_searxng_is_down(self):
        with patch('retrieval.search', side_effect=RuntimeError('offline')) as search, patch(
                'retrieval.open_public', return_value=(Mock(), http_fixture('hello', content_type='text/plain'))):
            self.assertEqual(retrieval.read_url('https://example.org')['text'], 'hello')
        search.assert_not_called()

    def test_reader_rejects_private_urls_and_non_web_protocols_before_connect(self):
        for url in ['file:///etc/passwd', 'http://127.0.0.1', 'http://[::1]/', 'http://192.168.1.1',
                    'http://169.254.169.254', 'http://localhost/', 'http://2130706433/',
                    'https://user:password@example.org', 'https://example.org\nHost: localhost']:
            with self.subTest(url=url), patch('retrieval.public_addresses') as resolve, self.assertRaises(RuntimeError):
                retrieval.open_public(url, 1)
            resolve.assert_not_called()

    def test_reader_rejects_dns_with_any_private_address(self):
        addresses = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443)),
                     (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 443))]
        with patch('retrieval.socket.getaddrinfo', return_value=addresses), self.assertRaises(RuntimeError):
            retrieval.public_addresses('example.org', 443)

    def test_reader_pins_validated_ip_and_keeps_original_host(self):
        connection = Mock()
        with patch('retrieval.public_addresses', return_value=['93.184.216.34']), patch('retrieval.getproxies', return_value={}), patch('retrieval.http.client.HTTPConnection', return_value=connection) as factory:
            retrieval.open_public('http://example.org/page', 2)
        factory.assert_called_once_with('93.184.216.34', 80, timeout=2)
        self.assertEqual(connection.request.call_args.kwargs['headers']['Host'], 'example.org')

    def test_redirect_to_private_address_is_rejected(self):
        connection = Mock()
        redirect = http_fixture('', status=302, headers='Location: http://127.0.0.1/secret\r\n')
        actual = retrieval.open_public
        def open_url(url, timeout):
            return (connection, redirect) if url == 'https://example.org' else actual(url, timeout)
        with patch('retrieval.open_public', side_effect=open_url), self.assertRaises(RuntimeError):
            retrieval.read_url('https://example.org')
        connection.close.assert_called_once()

    def test_reader_bounds_download_text_and_redirects(self):
        for body, content_type in [('a' * (retrieval.MAX_TEXT + 1), 'text/plain'),
                                   ('a' * (retrieval.MAX_BYTES + 20), 'text/plain')]:
            with patch('retrieval.open_public', return_value=(Mock(), http_fixture(body, content_type=content_type))):
                result = retrieval.read_url('https://example.org')
            self.assertTrue(result['truncated'])
            self.assertEqual(len(result['text']), retrieval.MAX_TEXT)
        redirects = [(Mock(), http_fixture('', status=302, headers='Location: /again\r\n')) for _ in range(4)]
        with patch('retrieval.open_public', side_effect=redirects), self.assertRaises(RuntimeError):
            retrieval.read_url('https://example.org')

    def test_reader_rejects_binary_and_compressed_content(self):
        for content_type, headers in [('application/pdf', ''), ('text/html', 'Content-Encoding: gzip\r\n')]:
            with patch('retrieval.open_public', return_value=(Mock(), http_fixture('x', content_type=content_type, headers=headers))), self.assertRaises(RuntimeError):
                retrieval.read_url('https://example.org')

    def test_model_generation_has_no_search_tool_registration(self):
        b = bot.Bot.__new__(bot.Bot)
        b.model, b.openrouter_key = 'test', 'test'
        with patch('bot.post', return_value={'choices': [{'message': {'content': 'ok'}}]}) as post:
            b.ask_openrouter_once([], 1.0, 'medium')
        self.assertNotIn('tools', post.call_args.args[1])


if __name__ == '__main__':
    unittest.main()
