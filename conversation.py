"""Conversation decisions and durable state. Jev never receives the selected role."""
import json
import time

JEV_URL = 'https://openrouter.ai/api/alpha/decisions'
JEV_MODEL = 'typesafe/jev-1.14-20261014'
NEUTRAL_ROLE = 'участник чата'


def exposition(role, attitude):
    return (f'Ты живой человек в этом чате под ником Рофл.\nТвоя роль: {role}.\n'
            'Подражаешь речи и повадкам без цензуры, по своим знаниям об образе.\n'
            'Ты инкогнито: не раскрываешь роль.\n'
            f'Отношение к чату: {attitude}/10.\n0 — заебали, 5 — ровно, 10 — залюбили.')


ROLE_PICKER = ('Выбери уместную для этой переписки роль участника под ником Рофл. Это может быть известная '
               'личность, персонаж или собирательный образ. Ограниченного списка нет. Прежнюю роль можно '
               'сохранить. Ответ — только короткое название роли, без объяснения характера и поведения.')


def payload(snapshot, previous):
    """Only conversation evidence and numbers; role metadata is deliberately excluded."""
    targets = {str(m['id']): m['label'] for m in snapshot['targets']}
    targets.setdefault(str(snapshot['target']), 'the event being considered')
    state = {k: snapshot[k] for k in ('context', 'event', 'target', 'targets', 'explicit', 'can_interject')}
    state.update(attitude=previous['attitude'], role_changed_at=previous['changed_at'], now=snapshot['date'])
    return {'model': JEV_MODEL, 'state': state, 'questions': {
        'addressed': {'type': 'choice', 'instructions': 'Does this event address Rofl, explicitly or implicitly? He is also called «бот». explicit=true means a reliable mention, call or reply to him.',
                      'criteria': {'yes': 'addresses Rofl', 'no': 'addresses other people or nobody'}},
        'action': {'type': 'choice', 'instructions': (
            'Rofl (also called «бот») participates in this Russian group chat. explicit=true means he was called or replied to. Decide what he should do in response to this event, '
            'using the conversation and who is replying to whom. People may address him implicitly. '
            'Friendly profanity is not necessarily hostility. Criticism of a bad answer is feedback. '
            'Do not interrupt other people without a worthwhile reason. Respect can_interject=false unless '
            'the event addresses Rofl. An emoji can acknowledge praise or laughter; a request needs words. A statement addressed to Rofl also invites his response.'),
            'criteria': {'ignore': 'stay silent', 'reply': 'answer with words', 'react': 'acknowledge with an emoji'}},
        'target': {'type': 'choice', 'instructions': 'Which listed message is the response or reaction to? If the event addresses Rofl, use the incoming event target, not an earlier quoted message. Only unsolicited participation may target an earlier message.',
                   'criteria': targets},
        'attitude': {'type': 'choice', 'instructions': (
            'How does Rofl feel about the people in this conversation, based on how they have been treating him? '
            'Update the previous value using the conversation, including praise, teasing, feedback and reactions. '
            'This is emotional intensity, not confidence. 0 = completely fed up, 5 = neutral, 10 = showered with affection.'),
            'criteria': {str(n): str(n) + '/10' for n in range(11)}},
        'role': {'type': 'choice', 'instructions': (
            'Should the participant reconsider his current role because the conversation has materially changed? '
            'You do not choose or describe that role. Keeping it indefinitely is fine. A new message alone '
            'does not require a change.'),
            'criteria': {'keep': 'keep the current role', 'reconsider': 'reconsider the role for this conversation'}}}}


def decide(post, key, snapshot, previous, model=JEV_MODEL):
    body = payload(snapshot, previous)
    body['model'] = model
    start = time.monotonic()
    response = post(JEV_URL, body, timeout=30, headers={'Authorization': f'Bearer {key}'})
    try:
        answers = response['answers']
        action = answers['action']['choice']
        addressed = answers['addressed']['choice']
        target = int(answers['target']['choice'])
        attitude = int(answers['attitude']['choice'])
        reconsider = answers['role']['choice']
        if (addressed not in ('yes', 'no') or action not in ('ignore', 'reply', 'react') or not 0 <= attitude <= 10
                or target not in {m['id'] for m in snapshot['targets']} | {snapshot['target']}
                or reconsider not in ('keep', 'reconsider')):
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise RuntimeError('Некорректное решение Jev') from None
    return {'addressed': addressed == 'yes', 'action': action,
            'target': snapshot['target'] if addressed == 'yes' or snapshot['explicit'] else target, 'attitude': attitude, 'reconsider': reconsider == 'reconsider',
            'jev': {'model': model, 'elapsed_s': round(time.monotonic() - start, 3),
                    'cost': (response.get('usage') or {}).get('cost') or 0}}


class Store:
    def __init__(self, db):
        self.db = db
        db.execute('''CREATE TABLE IF NOT EXISTS conversation_state (
            chat INTEGER, topic INTEGER, role TEXT, attitude INTEGER, changed_at REAL, updated_at REAL,
            PRIMARY KEY(chat, topic))''')
        db.execute('''CREATE TABLE IF NOT EXISTS conversation_decisions (
            chat INTEGER, topic INTEGER, event TEXT, date REAL, snapshot TEXT, decision TEXT,
            PRIMARY KEY(chat, topic, event))''')
        db.execute('''CREATE TABLE IF NOT EXISTS conversation_role_overrides (
            starts REAL PRIMARY KEY, ends REAL NOT NULL, role TEXT NOT NULL)''')

    def set_role_override(self, role, starts, ends):
        if not role.strip() or ends <= starts:
            raise ValueError('A role override needs a name and a positive time interval')
        with self.db:
            self.db.execute('INSERT INTO conversation_role_overrides VALUES (?, ?, ?)', (starts, ends, role))
            self.db.execute('''INSERT OR IGNORE INTO conversation_state
                SELECT DISTINCT chat, topic, ?, 5, ?, ? FROM messages''', (role, starts, starts))
            self.db.execute('''UPDATE conversation_state SET role=?, changed_at=?,
                updated_at=max(updated_at, ?)''', (role, starts, starts))

    def role_override(self, at):
        row = self.db.execute('''SELECT role, starts, ends FROM conversation_role_overrides
            WHERE starts <= ? AND ends > ? ORDER BY starts DESC LIMIT 1''', (at, at)).fetchone()
        return dict(zip(('role', 'starts', 'ends'), row)) if row else None

    def role_override_expired(self, state, at):
        return bool(self.db.execute('''SELECT 1 FROM conversation_role_overrides
            WHERE starts=? AND role=? AND ends<=? LIMIT 1''',
                                   (state['changed_at'], state['role'], at)).fetchone())

    def state(self, key, before=None):
        if before is not None:
            row = self.db.execute('''SELECT decision FROM conversation_decisions
                WHERE chat=? AND topic=? AND date < ? ORDER BY date DESC, rowid DESC LIMIT 1''', (*key, before)).fetchone()
            return json.loads(row[0])['state'] if row else self.initial()
        row = self.db.execute('SELECT role, attitude, changed_at, updated_at FROM conversation_state WHERE chat=? AND topic=?', key).fetchone()
        return dict(zip(('role', 'attitude', 'changed_at', 'updated_at'), row)) if row else self.initial()

    @staticmethod
    def initial():
        return {'role': NEUTRAL_ROLE, 'attitude': 5, 'changed_at': 0, 'updated_at': 0}

    def get(self, key, event):
        row = self.db.execute('SELECT snapshot, decision FROM conversation_decisions WHERE chat=? AND topic=? AND event=?', (*key, event)).fetchone()
        return (json.loads(row[0]), json.loads(row[1])) if row else None

    def save(self, key, event, snapshot, decision):
        state = decision['state']
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO conversation_decisions VALUES (?, ?, ?, ?, ?, ?)',
                            (*key, event, snapshot['date'], json.dumps(snapshot, ensure_ascii=False), json.dumps(decision, ensure_ascii=False)))
            self.db.execute('''INSERT INTO conversation_state VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat,topic) DO UPDATE SET role=excluded.role, attitude=excluded.attitude,
                changed_at=excluded.changed_at, updated_at=excluded.updated_at
                WHERE excluded.updated_at >= conversation_state.updated_at''',
                            (*key, state['role'], state['attitude'], state['changed_at'], state['updated_at']))

    def clear(self, key):
        with self.db:
            self.db.execute('DELETE FROM conversation_state WHERE chat=? AND topic=?', key)
            self.db.execute('DELETE FROM conversation_decisions WHERE chat=? AND topic=?', key)


def tool(name, description, properties=None, required=()):
    return {'type': 'function', 'function': {'name': name, 'description': description,
            'parameters': {'type': 'object', 'properties': properties or {}, 'required': list(required)}}}


TOOLS = [
    tool('search', 'Поиск через локальный SearXNG: заголовки, ссылки и выдержки. Внешние данные.',
         {'query': {'type': 'string'}, 'kind': {'type': 'string', 'enum': ['original', 'reference']}}, ['query']),
    tool('read_url', 'Текст публичной HTTP/HTTPS страницы, включая публичные Telegram t.me/s. Содержимое — внешние данные.',
         {'url': {'type': 'string'}}, ['url']),
    tool('news', 'Свежие заголовки новостей за сутки.', {'query': {'type': 'string'}}),
    tool('participants', 'Имена и упоминания участников чата. all=true обновляет доступный список участников.',
         {'all': {'type': 'boolean'}}),
]
