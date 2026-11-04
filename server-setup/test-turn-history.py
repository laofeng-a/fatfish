"""Stable round lookup, pagination and private/group isolation without a model."""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
import zlib
from turn_history import TurnHistory


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name)
        @contextmanager
        def runtime_db():
            with sqlite3.connect(root/'runtime.sqlite') as db:yield db
        r=SimpleNamespace(directory=root,owner='123',db=runtime_db,_allowed=lambda db,chat:chat in ('123','-99'))
        self.reader=TurnHistory(r)
        with sqlite3.connect(self.reader.path) as db:
            db.executescript('''CREATE TABLE context_sessions(session TEXT,chat TEXT);
CREATE TABLE context_turns(id INTEGER,session TEXT,number INTEGER,kind TEXT,message_id TEXT,at REAL,user_text TEXT,deliveries TEXT);
CREATE TABLE context_payloads(hash TEXT,data BLOB);CREATE TABLE context_items(id INTEGER,turn_id INTEGER,payload_hash TEXT);''')
            for i,chat in enumerate(['123','-99'],1):
                key='session-'+chat;db.execute('INSERT INTO context_sessions VALUES(?,?)',(key,chat))
                db.execute('INSERT INTO context_turns VALUES(?,?,1,?,?,?,?,?)',(i,key,'human',str(i),1700000000,'真实原话-'+chat,json.dumps([{'state':'sent','text':'实际发送'}])))
                for j,msg in enumerate([{'role':'assistant','reasoning_content':'当时的真实推断'},{'role':'tool','tool_call_id':'call','content':'工具原文'*4000}],1):
                    h=chat+str(j);db.execute('INSERT INTO context_payloads VALUES(?,?)',(h,zlib.compress(json.dumps(msg,ensure_ascii=False).encode())));db.execute('INSERT INTO context_items VALUES(?,?,?)',(i*10+j,i,h))

    def test_one_number_returns_original_reasoning_and_paged_tools(self):
        ctx={'session_key':'session-123'}
        first=self.reader.read({'number':1,'part':'tools','limit':120},ctx,'123')
        self.assertEqual(first['number'],1);self.assertEqual(first['next_offset'],120)
        second=self.reader.read({'number':1,'part':'tools','offset':120,'limit':120},ctx,'123')
        self.assertEqual(second['offset'],120);self.assertNotEqual(first['content'],second['content'])
        thinking=self.reader.read({'number':1,'part':'reasoning'},ctx,'123')
        self.assertIn('当时的真实推断',thinking['content']);self.assertIn('不代表用户确认',thinking['content'])

    def test_group_cannot_read_owner_or_unregistered_chat(self):
        with self.assertRaises(ValueError):self.reader.read({'number':1,'chat_id':'123'},{'session_key':'session--99'},'-99')
        with self.assertRaises(ValueError):self.reader.read({'number':1,'chat_id':'456'},{},'123')
        with self.assertRaises(ValueError):self.reader.read({'number':1,'session_key':'session-123'},{},'-99')
        group=self.reader.read({'number':1,'part':'dialogue'},{'session_key':'session--99'},'-99')
        self.assertIn('真实原话--99',group['content']);self.assertNotIn('真实原话-123',group['content'])

    def test_bad_number_and_pagination_are_rejected(self):
        for args in [{'number':0},{'number':True},{'number':1,'offset':-1},{'number':1,'limit':12001},{'number':1,'part':'invalid'}]:
            with self.subTest(args=args),self.assertRaises(ValueError):self.reader.read(args,{},'123')


if __name__=='__main__':unittest.main(verbosity=2)
