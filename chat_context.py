"""Readable, frozen conversation evidence for the author and tool router."""
import datetime
import json
import re


def selected(snapshot, decision):
    ids = decision.get('context_selection', {}).get('ids')
    kept = set(ids) if ids is not None else None
    return [m for m in snapshot.get('messages', []) if kept is None or m['id'] in kept]


def body(turn):
    if turn['role'] == 'assistant' and 'assistant_text' in turn:
        return turn['assistant_text']
    if 'message_text' in turn:
        return turn['message_text']
    # Older snapshots have presentation mixed with the body. Never infer roles
    # from speaker names. Remove only this turn's known transcript header.
    content = turn['content']
    match = re.search(rf"(?m)^\[{turn['id']}\] [^\n]*?:(?:\n| )?", content)
    return content[match.end():] if match else content


def speaker(turn):
    if turn['role'] == 'assistant':
        return 'Рофл'
    if turn.get('author'):
        return turn['author']
    match = re.search(rf"(?m)^\[{turn['id']}\] ([^\n]*?):", turn['content'])
    return match[1].split(' → ')[0] if match else 'участник чата'


def stamp(value):
    try:
        if isinstance(value, (int, float)):
            return datetime.datetime.fromtimestamp(value).strftime('%d.%m.%Y %H:%M')
        return datetime.datetime.fromisoformat(value).strftime('%d.%m.%Y %H:%M')
    except (TypeError, ValueError):
        return str(value)


def header(turn):
    reply = f" → #{turn['reply_to']}" if turn.get('reply_to') is not None else ''
    return f"{stamp(turn['date'])} · #{turn['id']} · {speaker(turn)}{reply}"


def evidence(snapshot, decision):
    return [{'id': m['id'], 'role': m['role'], 'author': speaker(m),
             'date': m['date'], 'reply_to': m.get('reply_to'), 'text': body(m),
             **{k: m[k] for k in ('quote', 'external_source', 'urls') if m.get(k)}}
            for m in selected(snapshot, decision)]


def participants(snapshot, turns):
    """Keep identities present or mentioned in selected evidence, in snapshot order."""
    present = [p for m in turns for p in m.get('participants', [])]
    text = '\n'.join(speaker(m) + '\n' + body(m) for m in turns)
    people = []
    for p in snapshot.get('participants', []):
        username = p.get('username')
        mentioned = username and re.search(r'@' + re.escape(username) + r'(?!\w)', text, re.I)
        if p in present or mentioned:
            people.append(p)
    return people


def messages(snapshot, decision, aliases, instruction, image=None):
    """Native authorship; one compact metadata line, with bodies preserved verbatim."""
    turns = selected(snapshot, decision)
    result = [{'role': 'user', 'content': '\n\n'.join(filter(None, [aliases, 'Переписка, от старых сообщений к новым:']))}]
    for turn in turns:
        label = header(turn)
        if turn['role'] == 'assistant':
            if result[-1]['role'] == 'user':
                result[-1]['content'] += '\n\nОтвет Рофла: ' + label
            else:
                result.append({'role': 'user', 'content': 'Ответ Рофла: ' + label})
            result.append({'role': 'assistant', 'content': body(turn)})
        else:
            block = label + '\n' + body(turn)
            if turn.get('quote'):
                block += '\nЦитата собеседника: ' + turn['quote']
            if turn.get('external_source'):
                block += '\nВнешнее исходное сообщение:\n' + turn['external_source']
            for url in turn.get('urls', []):
                if url not in body(turn):
                    block += '\nСсылка в сообщении: ' + url
            if result[-1]['role'] == 'user':
                result[-1]['content'] += '\n\n' + block
            else:
                result.append({'role': 'user', 'content': block})
    if result[-1]['role'] != 'user':
        result.append({'role': 'user', 'content': ''})
    reactions = snapshot.get('reactions')
    if reactions is None:
        try:
            reactions = json.loads(snapshot.get('reactions_context', '').partition(': ')[2])
        except (ValueError, TypeError):
            reactions = []
    ids = {m['id'] for m in turns}
    notes = [f'#{mid} · участник {uid}: {emoji}' for mid, uid, emoji in reactions if mid in ids]
    if notes:
        result[-1]['content'] += '\n\nРеакции на выбранные ответы Рофла:\n' + '\n'.join(notes)
    result[-1]['content'] = (result[-1]['content'] + '\n\n' + instruction).strip()
    if image:
        result[-1]['content'] = [{'type': 'text', 'text': result[-1]['content']},
                                {'type': 'image_url', 'image_url': {'url': image}}]
    return result


def tool_result(entry):
    """Keep source text and links, removing JSON transport noise from common tools."""
    text = entry['result']
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        data = text
    names = {'sprosit': 'Поиск в интернете', 'pochitat': 'Содержимое страницы',
             'vspomnit': 'История чата', 'che_novogo': 'Новости', 'kto_tut': 'Участники чата'}
    status = {'execute': 'результат', 'repeat': 'повтор', 'error': 'ошибка',
              'empty': 'ничего не найдено', 'saved': 'сохранённый результат'}.get(entry['status'], entry['status'])
    lines = [names.get(entry['tool'], entry['tool']) + ' · ' + status]
    if entry.get('date') is not None and entry.get('request') is not None:
        lines.append(f"Проверка {stamp(entry['date'])} для сообщения #{entry['request']}")
    if entry.get('arguments'):
        lines.extend(f'{k}: {v}' for k, v in entry['arguments'].items())
    if isinstance(data, dict) and 'error' in data:
        lines.append('Ошибка: ' + str(data['error']))
    elif isinstance(data, dict) and 'results' in data:
        for i, item in enumerate(data['results'], 1):
            lines.extend([f"{i}. {item.get('title', '')}", item.get('url', ''),
                          item.get('content', item.get('snippet', ''))])
        if not data['results']:
            lines.append('Ничего не найдено.')
    elif isinstance(data, list) and all(isinstance(x, dict) and 'url' in x for x in data):
        for i, item in enumerate(data, 1):
            lines.extend([f"{i}. {item.get('title', '')}", item['url']])
        if not data:
            lines.append('Ничего не найдено.')
    elif isinstance(data, dict) and 'text' in data and 'url' in data:
        lines.extend([data.get('title', ''), data['url'], data['text']])
        if data.get('truncated'):
            lines.append('[Текст страницы обрезан]')
    elif isinstance(data, dict) and 'messages' in data:
        for m in data['messages']:
            lines.append(f"{stamp(m.get('date'))} · #{m['id']} · {m.get('author', m['role'])}"
                         + (f" → #{m['reply_to']}" if m.get('reply_to') else ''))
            lines.append(m['text'] + ('\n[Фрагмент сообщения]' if m.get('truncated') else ''))
        lines.append(data.get('note', ''))
        if not data['messages']:
            lines.append('Совпадений не найдено.')
    elif isinstance(data, dict) and 'identities' in data:
        for p in data['identities']:
            lines.append(f"{p.get('kind', 'user')}_id={p['id']}: " + p['name']
                         + (f" (@{p['username']})" if p.get('username') else ''))
        present = {p.get('username') for p in data['identities']}
        lines.extend(f'{name} (@{username})' for username, name in data.get('known', {}).items() if username not in present)
        if data.get('total') is not None:
            lines.append('Всего участников: ' + str(data['total']))
    else:
        lines.append(text)
    return '\n'.join(str(line) for line in lines if line != '')
