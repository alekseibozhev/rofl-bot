"""Compact conversation and tool-output formatting for the agentic bot."""
import datetime


def stamp(value):
    try:
        if isinstance(value, (int, float)):
            return datetime.datetime.fromtimestamp(value).strftime('%d.%m.%Y %H:%M')
        return datetime.datetime.fromisoformat(value).strftime('%d.%m.%Y %H:%M')
    except (TypeError, ValueError):
        return str(value)


def header(turn):
    reply = f" → #{turn['reply_to']}" if turn.get('reply_to') is not None else ''
    return f"{stamp(turn['date'])} · #{turn['id']} · {turn['author']}{reply}"


def format_turn(turn, max_chars=None):
    text = turn.get('text', '')
    truncated = False
    if max_chars is not None and len(text) > max_chars:
        text = text[:max_chars].rstrip() + '…'
        truncated = True
    parts = [header(turn), text]
    if turn.get('quote'):
        parts.append('Цитата собеседника: ' + turn['quote'])
    if turn.get('external_source'):
        parts.append('Внешнее исходное сообщение:\n' + turn['external_source'])
    for url in turn.get('urls', []):
        if url not in text:
            parts.append('Ссылка в сообщении: ' + url)
    if truncated:
        parts.append('[Фрагмент сообщения]')
    return '\n'.join(parts)


def messages(turns, aliases, instruction, image=None):
    """Native authorship: human turns in user role, Rofl's past answers in assistant role."""
    intro = '\n\n'.join(filter(None, [aliases, 'Недавняя переписка (от старых к новым):']))
    result = [{'role': 'user', 'content': intro}]
    for turn in turns:
        if turn['role'] == 'assistant':
            label = 'Ответ Рофла: ' + header(turn)
            if result[-1]['role'] == 'user':
                result[-1]['content'] += '\n\n' + label
            else:
                result.append({'role': 'user', 'content': label})
            result.append({'role': 'assistant', 'content': turn.get('text', '')})
        else:
            block = format_turn(turn)
            if result[-1]['role'] == 'user':
                result[-1]['content'] += '\n\n' + block
            else:
                result.append({'role': 'user', 'content': block})
    if result[-1]['role'] != 'user':
        result.append({'role': 'user', 'content': ''})
    result[-1]['content'] = (result[-1]['content'] + '\n\n' + instruction).strip()
    if image:
        result[-1]['content'] = [{'type': 'text', 'text': result[-1]['content']},
                                 {'type': 'image_url', 'image_url': {'url': image}}]
    return result


def format_web_search(data):
    if isinstance(data, dict) and data.get('error'):
        return 'Ошибка поиска: ' + str(data['error'])
    items = (data or {}).get('results', []) if isinstance(data, dict) else []
    if not items:
        return 'Ничего не найдено.'
    lines = []
    for i, item in enumerate(items, 1):
        lines.append(f"{i}. {item.get('title', '')}")
        if item.get('url'):
            lines.append(item['url'])
        snippet = item.get('content') or item.get('snippet') or ''
        if snippet:
            lines.append(snippet)
    return '\n'.join(lines)


def format_read_url(data):
    if isinstance(data, dict) and data.get('error'):
        return 'Ошибка чтения страницы: ' + str(data['error'])
    lines = [data.get('title', ''), data.get('url', ''), data.get('text', '')]
    if data.get('truncated'):
        lines.append('[Текст страницы обрезан]')
    return '\n'.join(line for line in lines if line)


def format_news(items):
    if not items:
        return 'Свежих новостей по этому запросу не найдено.'
    lines = []
    for i, (title, url) in enumerate(items, 1):
        lines.append(f'{i}. {title}')
        if url:
            lines.append(url)
    return '\n'.join(lines)


def format_search_chat(data):
    if isinstance(data, dict) and data.get('error'):
        return 'Ошибка поиска по чату: ' + str(data['error'])
    items = data.get('messages', [])
    if not items:
        return 'Совпадений в сохранённой истории чата не найдено.'
    lines = []
    for m in items:
        reply = f" → #{m['reply_to']}" if m.get('reply_to') else ''
        lines.append(f"{stamp(m.get('date'))} · #{m['id']} · {m.get('author', m['role'])}{reply}")
        lines.append(m['text'] + ('\n[Фрагмент сообщения]' if m.get('truncated') else ''))
    if data.get('note'):
        lines.append(data['note'])
    return '\n'.join(lines)


def format_read_chat(data):
    turns = data.get('messages', [])
    if not turns:
        return 'Более ранних сообщений в сохранённой истории этого чата нет.'
    lines = [format_turn(t, max_chars=600) for t in turns]
    footer = f"Показано {len(turns)} сообщений (самое старое: #{data['oldest_id']})."
    if data.get('has_more'):
        footer += f" Есть ещё более ранние — можно вызвать read_chat(before_id={data['oldest_id']})."
    else:
        footer += ' Это начало доступной истории.'
    lines.append(footer)
    return '\n\n'.join(lines)
