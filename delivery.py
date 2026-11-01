"""Durable Telegram inbox and outbox. Network delivery and local completion are separate."""
import json
import time


class Store:
    def __init__(self, db):
        self.db = db
        db.execute('''CREATE TABLE IF NOT EXISTS telegram_inbox (
            id INTEGER PRIMARY KEY, payload TEXT NOT NULL, received REAL NOT NULL,
            done INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0)''')
        db.execute('''CREATE TABLE IF NOT EXISTS telegram_outbox (
            chat INTEGER, topic INTEGER, event TEXT, payload TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', receipt TEXT, created REAL NOT NULL,
            PRIMARY KEY(chat, topic, event))''')

    def enqueue(self, updates):
        if not updates:
            return
        now = time.time()
        with self.db:
            self.db.executemany('INSERT OR IGNORE INTO telegram_inbox (id,payload,received) VALUES (?,?,?)',
                                [(u['update_id'], json.dumps(u, ensure_ascii=False), now) for u in updates])
            self.db.execute('''INSERT INTO state VALUES ('offset',?) ON CONFLICT(key)
                DO UPDATE SET value=max(value,excluded.value)''', (max(u['update_id'] for u in updates) + 1,))
            self.db.execute('DELETE FROM telegram_inbox WHERE done=1 AND received<?', (now - 7 * 86400,))

    def pending(self):
        return [dict(json.loads(raw), _received_at=received) for raw, received in self.db.execute(
            'SELECT payload,received FROM telegram_inbox WHERE done=0 AND retry_at<=? ORDER BY id', (time.time(),))]

    def finish(self, updates, failed):
        with self.db:
            for update in updates:
                retry = update['update_id'] in failed
                self.db.execute('UPDATE telegram_inbox SET done=?,retry_at=? WHERE id=?',
                                (not retry, time.time() + 30 if retry else 0, update['update_id']))

    def outgoing(self, key, event):
        row = self.db.execute('SELECT payload,status,receipt FROM telegram_outbox WHERE chat=? AND topic=? AND event=?',
                              (*key, event)).fetchone()
        if row:
            return {'key': key, 'event': event, 'payload': json.loads(row[0]), 'status': row[1],
                    'receipt': json.loads(row[2]) if row[2] else None}

    def stage(self, key, event, payload):
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO telegram_outbox (chat,topic,event,payload,created) VALUES (?,?,?,?,?)',
                            (*key, event, json.dumps(payload, ensure_ascii=False), time.time()))
        return self.outgoing(key, event)

    def status(self, key, event, status, receipt=None):
        with self.db:
            self.db.execute('UPDATE telegram_outbox SET status=?,receipt=? WHERE chat=? AND topic=? AND event=?',
                            (status, json.dumps(receipt) if receipt is not None else None, *key, event))

    def recover(self):
        # sendMessage has no idempotency key: a killed in-flight send may have succeeded.
        with self.db:
            changed = self.db.execute("UPDATE telegram_outbox SET status='unknown' WHERE status='sending'").rowcount
        return changed

    def ready(self):
        return [self.outgoing((chat, topic), event) for chat, topic, event in self.db.execute(
            "SELECT chat,topic,event FROM telegram_outbox WHERE status IN ('pending','delivered') ORDER BY created")]
