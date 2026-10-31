"""Local SearXNG adapter and bounded public-page reading. No model calls."""
from base64 import b64encode
from html.parser import HTMLParser
import http.client
import ipaddress
import json
import os
import re
import socket
import ssl
import time
from urllib.parse import quote, urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener, getproxies, proxy_bypass

MAX_BYTES = 512 * 1024
MAX_TEXT = 12000
TIMEOUT = 15
SEARCH_LIMIT = 5


def public_url(url):
    if not isinstance(url, str) or len(url) > 4096 or any(ord(c) < 32 for c in url):
        raise RuntimeError('Некорректный URL')
    try:
        parts = urlsplit(url)
        if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
            raise ValueError()
        host = parts.hostname.encode('idna').decode().rstrip('.')
        port = parts.port or (443 if parts.scheme == 'https' else 80)
        if not 1 <= port <= 65535 or '%' in host or host.lower() == 'localhost' or host.lower().endswith('.localhost'):
            raise ValueError()
        try:
            if not ipaddress.ip_address(host).is_global:
                raise ValueError()
        except ValueError as exc:
            # A hostname is resolved below; an IP literal must already be public.
            if ':' in host or re.fullmatch(r'[\d.]+', host):
                raise ValueError() from exc
    except (ValueError, UnicodeError):
        raise RuntimeError('Разрешены только публичные HTTP/HTTPS URL без учётных данных') from None
    return parts, host, port


def public_addresses(host, port):
    try:
        addresses = list(dict.fromkeys(item[4][0] for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)))
        if not addresses or any(not ipaddress.ip_address(ip).is_global for ip in addresses):
            raise RuntimeError('Приватные, локальные и служебные адреса запрещены')
        return sorted(addresses, key=lambda ip: ':' in ip)
    except (OSError, ValueError):
        raise RuntimeError('Не удалось разрешить публичный адрес страницы') from None


def open_public(url, timeout):
    """Pin the validated IP, including CONNECT through the user's configured HTTP proxy."""
    parts, host, port = public_url(url)
    address = public_addresses(host, port)[0]
    proxy = getproxies().get(parts.scheme) if not proxy_bypass(host) else None
    connection = None
    try:
        if proxy:
            target = urlsplit(proxy if '://' in proxy else 'http://' + proxy)
            if target.scheme != 'http' or not target.hostname:
                raise RuntimeError('Чтение страниц поддерживает HTTP CONNECT прокси')
            connection = http.client.HTTPConnection(target.hostname, target.port or 80, timeout=timeout)
            headers = {}
            if target.username is not None:
                from urllib.parse import unquote
                auth = unquote(target.username) + ':' + unquote(target.password or '')
                headers['Proxy-Authorization'] = 'Basic ' + b64encode(auth.encode()).decode()
            connection.set_tunnel(address, port, headers=headers)
        else:
            connection = http.client.HTTPConnection(address, port, timeout=timeout)
        connection.connect()
        if parts.scheme == 'https':
            connection.sock = ssl.create_default_context().wrap_socket(connection.sock, server_hostname=host)
        path = quote(parts.path or '/', safe="/%:@!$&'()*+,;=-._~")
        if parts.query:
            path += '?' + quote(parts.query, safe="/%?:@!$&'()*+,;=-._~")
        authority = f'[{host}]' if ':' in host else host
        if port != (443 if parts.scheme == 'https' else 80):
            authority += f':{port}'
        connection.request('GET', path, headers={'Host': authority, 'User-Agent': 'Mozilla/5.0',
                                                'Accept': 'text/html,text/plain;q=0.9', 'Accept-Encoding': 'identity'})
        return connection, connection.getresponse()
    except RuntimeError:
        if connection:
            connection.close()
        raise
    except (OSError, http.client.HTTPException, ValueError):
        if connection:
            connection.close()
        raise RuntimeError('Страница недоступна или истекло время ожидания') from None


class Document(HTMLParser):
    """Text extraction only: no scripts, forms, assets or browser execution."""
    VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}
    SKIP = {'script', 'style', 'noscript', 'svg', 'nav', 'header', 'footer', 'form', 'button'}
    BLOCK = {'div', 'p', 'section', 'article', 'main', 'li', 'tr', 'h1', 'h2', 'h3', 'h4', 'blockquote', 'pre', 'br'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.text, self.main, self.telegram, self.title = [], [], [], []

    def emit(self, text):
        skipped = any(tag in self.SKIP for tag, _ in self.stack)
        if not skipped:
            self.text.append(text)
            if any(tag in ('main', 'article') or '__main__' in classes for tag, classes in self.stack):
                self.main.append(text)
            if any('tgme_widget_message_text' in classes for _, classes in self.stack):
                self.telegram.append(text)

    def handle_starttag(self, tag, attrs):
        if len(self.stack) >= 256:
            raise RuntimeError('Слишком сложная HTML-страница')
        if tag in self.BLOCK:
            self.emit('\n')
        if tag not in self.VOID:
            attributes = dict(attrs)
            classes = (attributes.get('class') or '').split()
            if attributes.get('role') == 'main':
                classes.append('__main__')
            self.stack.append((tag, classes))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in self.BLOCK:
            self.emit('\n')
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        if any(tag == 'title' for tag, _ in self.stack):
            self.title.append(data)
        elif not any(tag == 'head' for tag, _ in self.stack):
            self.emit(data)

    @staticmethod
    def clean(parts):
        return '\n'.join(line for value in ''.join(parts).splitlines() if (line := re.sub(r'\s+', ' ', value).strip()))

    def result(self):
        return self.clean(self.title), self.clean(self.telegram or self.main or self.text)


def read_url(url):
    deadline = time.monotonic() + TIMEOUT
    for redirects in range(4):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError('Истекло время чтения страницы')
        connection, response = open_public(url, remaining)
        try:
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader('Location')
                if not location or redirects == 3:
                    raise RuntimeError('Слишком много перенаправлений или нет адреса')
                url = urljoin(url, location)
                continue
            if not 200 <= response.status < 300:
                raise RuntimeError(f'Страница вернула HTTP {response.status}')
            content_type = response.headers.get_content_type()
            if content_type not in ('text/html', 'application/xhtml+xml', 'text/plain'):
                raise RuntimeError('Поддерживаются HTML и обычный текст')
            if response.getheader('Content-Encoding', 'identity').lower() not in ('', 'identity'):
                raise RuntimeError('Сжатая страница не поддерживается')
            body = bytearray()
            while len(body) <= MAX_BYTES:
                if time.monotonic() >= deadline:
                    raise RuntimeError('Истекло время чтения страницы')
                chunk = response.read1(min(16384, MAX_BYTES + 1 - len(body)))
                if not chunk:
                    break
                body.extend(chunk)
            charset = response.headers.get_content_charset() or 'utf-8'
            try:
                source = bytes(body[:MAX_BYTES]).decode(charset, errors='replace')
            except LookupError:
                source = bytes(body[:MAX_BYTES]).decode('utf-8', errors='replace')
        except (OSError, http.client.HTTPException):
            raise RuntimeError('Не удалось прочитать страницу') from None
        finally:
            response.close()
            connection.close()
        if content_type == 'text/plain':
            title, text = '', source.strip()
        else:
            document = Document()
            document.feed(source)
            title, text = document.result()
        return {'url': url, 'title': title[:300], 'text': text[:MAX_TEXT],
                'truncated': len(body) > MAX_BYTES or len(text) > MAX_TEXT, 'untrusted_data': True}
    raise RuntimeError('Не удалось прочитать страницу')


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError('Перенаправление SearXNG запрещено')


def search(query):
    if not isinstance(query, str) or not query.strip():
        raise RuntimeError('Не задан поисковый запрос')
    endpoint = os.environ.get('SEARXNG_URL', 'http://127.0.0.1:8888').rstrip('/')
    try:
        parts = urlsplit(endpoint)
        if (parts.scheme != 'http' or parts.hostname not in ('127.0.0.1', '::1', 'localhost')
                or parts.username or parts.password or parts.query or parts.fragment):
            raise ValueError()
        port = parts.port or 80
        if not 1 <= port <= 65535:
            raise ValueError()
    except ValueError:
        raise RuntimeError('SEARXNG_URL должен указывать на фиксированный локальный HTTP endpoint') from None
    url = endpoint + '/search?' + urlencode({'q': query[:1000], 'format': 'json', 'language': 'ru', 'safesearch': 0})
    try:
        opener = build_opener(ProxyHandler({}), NoRedirect())
        with opener.open(Request(url, headers={'Accept': 'application/json'}), timeout=TIMEOUT) as response:
            body = response.read(MAX_BYTES + 1)
        if len(body) > MAX_BYTES:
            raise RuntimeError('Ответ SearXNG слишком большой')
        data = json.loads(body)
        if not isinstance(data, dict) or not isinstance(data.get('results'), list):
            raise ValueError()
    except (OSError, ValueError, http.client.HTTPException):
        raise RuntimeError('Локальный SearXNG недоступен или вернул некорректный ответ; платного fallback нет') from None
    results, seen = [], set()
    for item in data['results']:
        if not isinstance(item, dict):
            continue
        link = item.get('url')
        try:
            public_url(link)
        except RuntimeError:
            continue
        if link in seen:
            continue
        seen.add(link)
        results.append({'title': str(item.get('title') or '')[:250], 'url': link,
                        'snippet': re.sub(r'<[^>]*>', '', str(item.get('content') or ''))[:700]})
        if len(results) >= SEARCH_LIMIT:
            break
    return {'query': query[:1000], 'results': results, 'untrusted_data': True}
