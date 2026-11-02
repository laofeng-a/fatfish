"""Shared two-yuan background sub-budget; all amounts are micro-CNY."""
import datetime as dt
import sqlite3
from contextlib import closing
from pathlib import Path
from spending_control import limits_enabled

UTC8 = dt.timezone(dt.timedelta(hours=8))
DEFAULT_PATH = '/var/lib/picoclaw-readers/activity.sqlite'

class ActivityLedger:
    def __init__(self, path=DEFAULT_PATH):
        self.path = str(path)
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.execute('PRAGMA cache_size=-128')
            db.execute('CREATE TABLE IF NOT EXISTS charges(id INTEGER PRIMARY KEY, day TEXT NOT NULL, kind TEXT NOT NULL, amount INTEGER NOT NULL)')

    def reserve(self, amount, kind, limit=2_000_000):
        day = dt.datetime.now(UTC8).date().isoformat()
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            used = db.execute('SELECT COALESCE(SUM(amount),0) FROM charges WHERE day=?', (day,)).fetchone()[0]
            if limits_enabled() and used + amount > limit:
                raise ValueError('今日后台探索子额度已用完；普通聊天仍按总额度执行')
            return db.execute('INSERT INTO charges(day,kind,amount) VALUES(?,?,?)', (day,kind,amount)).lastrowid

    def settle(self, identifier, amount):
        if identifier is None or type(amount) is not int or amount < 0:
            return
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.execute('UPDATE charges SET amount=? WHERE id=?', (amount,identifier))

    def used(self):
        day = dt.datetime.now(UTC8).date().isoformat()
        with closing(sqlite3.connect(self.path, timeout=5)) as db:
            return db.execute('SELECT COALESCE(SUM(amount),0) FROM charges WHERE day=?', (day,)).fetchone()[0]
