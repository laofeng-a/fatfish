"""Persistent personal tasks. No chat delivery, timer thread, or tool execution.

The runtime owns scheduling and authorization of all actual work. A due task is
only a reason to *look at* the task; its text never becomes a capability grant.
"""
import hashlib
import json
import time

from reminders import Reminders


class TodoTasks:
    ACTIVE = ('pending', 'in_progress', 'blocked')
    CLOSED = ('completed', 'cancelled')
    PRIORITIES = ('low', 'normal', 'high')
    MAX_OPEN = 200
    REVIEW_INTERVAL = 5400

    def __init__(self, runtime):
        self.r = runtime
        with runtime.db() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS todo_tasks(
                id INTEGER PRIMARY KEY,chat TEXT NOT NULL,title TEXT NOT NULL,
                due REAL,priority TEXT NOT NULL,status TEXT NOT NULL,
                next_step TEXT NOT NULL DEFAULT '',progress TEXT NOT NULL DEFAULT '',
                result TEXT NOT NULL DEFAULT '',evidence TEXT NOT NULL DEFAULT '',
                source_message TEXT NOT NULL,source_excerpt TEXT NOT NULL DEFAULT '',
                create_key TEXT NOT NULL UNIQUE,created REAL NOT NULL,updated REAL NOT NULL,
                completed REAL,revision INTEGER NOT NULL DEFAULT 1,
                last_reviewed REAL,review_after REAL NOT NULL DEFAULT 0);
            CREATE INDEX IF NOT EXISTS todo_tasks_due ON todo_tasks(chat,status,due,review_after);
            CREATE TABLE IF NOT EXISTS todo_task_events(
                id INTEGER PRIMARY KEY,task_id INTEGER NOT NULL,action TEXT NOT NULL,
                created REAL NOT NULL,source TEXT NOT NULL,changes TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS todo_task_events_task ON todo_task_events(task_id,id);
            ''')

    @staticmethod
    def _text(value, name, maximum, empty=True):
        if not isinstance(value, str):
            raise ValueError(name + '需要文字')
        clean = value.strip()
        if len(clean) > maximum or (not empty and not clean):
            raise ValueError(name + ('不能为空，且' if not empty else '') + '最长' + str(maximum) + '字')
        return clean

    @classmethod
    def public(cls, row):
        return {
            'id': row['id'], 'title': row['title'],
            'due_at': Reminders.date(row['due']) if row['due'] is not None else None,
            'priority': row['priority'], 'status': row['status'],
            'next_step': row['next_step'], 'progress': row['progress'],
            'result': row['result'], 'evidence': row['evidence'],
            'source': {'chat_id': row['chat'], 'message_id': row['source_message'],
                       'excerpt': row['source_excerpt']},
            'created_at': Reminders.date(row['created']),
            'updated_at': Reminders.date(row['updated']),
            'completed_at': Reminders.date(row['completed']) if row['completed'] else None,
            'revision': row['revision'],
            # A scheduler selection is not proof that the model did work.
            'last_selected_at': Reminders.date(row['last_reviewed']) if row['last_reviewed'] else None,
            'review_after': Reminders.date(row['review_after']) if row['review_after'] else None,
            'delivery': 'none',
        }

    @staticmethod
    def _human_source(context):
        message = str(context.get('message_id') or '').strip()
        return message or str(context.get('session_key') or int(time.time()//300))

    @staticmethod
    def _source(context, background):
        return json.dumps({'mode': 'background' if background else 'private',
                           'message_id': str(context.get('message_id') or '')[:200]},
                          ensure_ascii=False, sort_keys=True)

    def _record(self, db, identifier, action, source, changes, now):
        db.execute('INSERT INTO todo_task_events(task_id,action,created,source,changes) VALUES(?,?,?,?,?)',
                   (identifier, action, now, source, json.dumps(changes, ensure_ascii=False, sort_keys=True)))
        # A compact audit per task; never load an ever-growing event stream into prompts.
        db.execute('DELETE FROM todo_task_events WHERE task_id=? AND id NOT IN '
                   '(SELECT id FROM todo_task_events WHERE task_id=? ORDER BY id DESC LIMIT 32)',
                   (identifier, identifier))

    @staticmethod
    def _due(args, now, default=None):
        if 'due_at' in args and 'delay_seconds' in args:
            raise ValueError('due_at与delay_seconds只选一种')
        if 'due_at' in args and args['due_at'] is None:
            return None
        if 'due_at' in args or 'delay_seconds' in args:
            return Reminders.parse_due(args, now)
        return default

    def call(self, args, context, chat, *, background_mode=None):
        """All runtime scenes share the same task-management operations."""
        chat=self.r.owner
        action = args.get('action')
        if action not in ('create', 'list', 'update', 'complete', 'cancel'):
            raise ValueError('action需要create/list/update/complete/cancel')
        now = time.time()
        background = context.get('session_key') == 'heartbeat'
        if action == 'list':
            return self._list(args, now)
        message = self._human_source(context)
        with self.r.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if action == 'create':
                return self._create(db, args, context, chat, message, now)
            identifier = args.get('id')
            if type(identifier) is not int or identifier < 1:
                raise ValueError('需要真实待办编号')
            row = db.execute('SELECT * FROM todo_tasks WHERE id=? AND chat=?',
                             (identifier, chat)).fetchone()
            if not row:
                raise ValueError('没有这条待办')
            revision = args.get('expected_revision')
            if revision is not None and (type(revision) is not int or revision != row['revision']):
                raise ValueError('待办已更新；先list读取最新revision再修改')
            source = self._source(context, background)
            if action == 'complete':
                result = self._text(args.get('result', ''), '完成结果', 1200, empty=False)
                evidence = self._text(args.get('evidence', ''), '完成依据', 2000)
                if row['status'] == 'completed':
                    return dict(self.public(row), already_completed=True)
                if not evidence:
                    evidence = '来源上下文 ' + message
                changes = {'status': 'completed', 'result': result, 'evidence': evidence}
                db.execute("UPDATE todo_tasks SET status='completed',result=?,evidence=?,completed=?,"
                           'updated=?,revision=revision+1 WHERE id=?',
                           (result, evidence, now, now, identifier))
            elif action == 'cancel':
                if row['status'] in self.CLOSED:
                    return dict(self.public(row), already_closed=True)
                changes = {'status': 'cancelled'}
                db.execute("UPDATE todo_tasks SET status='cancelled',updated=?,revision=revision+1 WHERE id=?",
                           (now, identifier))
            else:
                changes = self._update(db, row, args, background, now)
                if not changes:
                    return dict(self.public(row), unchanged=True)
            self._record(db, identifier, action, source, changes, now)
            return self.public(db.execute('SELECT * FROM todo_tasks WHERE id=?', (identifier,)).fetchone())

    def _create(self, db, args, context, chat, message, now):
        if 'due_at' in args and 'delay_seconds' in args:
            raise ValueError('due_at与delay_seconds只选一种')
        title = self._text(args.get('title', ''), '待办内容', 600, empty=False)
        priority = args.get('priority', 'normal')
        if priority not in self.PRIORITIES:
            raise ValueError('priority需要low/normal/high')
        next_step = self._text(args.get('next_step', ''), '下一步', 1000)
        excerpt = self._text(args.get('source', ''), '来源摘录', 1200)
        # Raw requested delay participates, rather than the ever-changing clock.
        key = hashlib.sha256(json.dumps([chat, message, title, args.get('due_at'),
                                        args.get('delay_seconds')], ensure_ascii=False).encode()).hexdigest()
        old = db.execute('SELECT * FROM todo_tasks WHERE create_key=?', (key,)).fetchone()
        if old:
            return dict(self.public(old), already_created=True)
        due = self._due(args, now)
        count = db.execute("SELECT COUNT(*) FROM todo_tasks WHERE chat=? AND status IN "
                           "('pending','in_progress','blocked')", (chat,)).fetchone()[0]
        if count >= self.MAX_OPEN:
            raise ValueError('未完成待办已达200条，请先整理已有清单')
        identifier = db.execute(
            "INSERT INTO todo_tasks(chat,title,due,priority,status,next_step,source_message,source_excerpt,"
            "create_key,created,updated) VALUES(?,?,?,?,'pending',?,?,?,?,?,?)",
            (chat, title, due, priority, next_step, message, excerpt, key, now, now)).lastrowid
        self._record(db, identifier, 'create', self._source(context, False),
                     {'title': title, 'due_at': Reminders.date(due) if due else None,
                      'priority': priority, 'next_step': next_step}, now)
        return self.public(db.execute('SELECT * FROM todo_tasks WHERE id=?', (identifier,)).fetchone())

    def _update(self, db, row, args, background, now):
        values = {}
        for field, maximum in (('title', 600), ('next_step', 1000), ('progress', 1200), ('evidence', 2000)):
            if field in args:
                values[field] = self._text(args[field], field, maximum, empty=(field != 'title'))
        if 'priority' in args:
            if args['priority'] not in self.PRIORITIES:
                raise ValueError('priority需要low/normal/high')
            values['priority'] = args['priority']
        if 'status' in args:
            if args['status'] not in self.ACTIVE:
                raise ValueError('update的status需要pending/in_progress/blocked；完成请complete')
            values['status'] = args['status']
        if 'due_at' in args or 'delay_seconds' in args:
            values['due'] = self._due(args, now)
        changes = {key: value for key, value in values.items() if value != row[key]}
        if not changes:
            return {}
        # Only constant, locally selected column names enter this statement.
        db.execute('UPDATE todo_tasks SET ' + ','.join(key + '=?' for key in changes) +
                   ',updated=?,revision=revision+1,review_after=? WHERE id=?',
                   (*changes.values(), now, row['review_after'] if background else 0, row['id']))
        return changes

    def _list(self, args, now):
        limit = args.get('limit', 20)
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError('limit需要1至50的整数')
        status = args.get('status', 'active')
        if status not in ('active', 'all', *self.ACTIVE, *self.CLOSED):
            raise ValueError('status需要active/all/pending/in_progress/blocked/completed/cancelled')
        query = self._text(args.get('query', ''), '检索词', 120)
        clauses = ['chat=?']; params = [self.r.owner]
        if status == 'active':
            clauses.append("status IN ('pending','in_progress','blocked')")
        elif status != 'all':
            clauses.append('status=?'); params.append(status)
        if query:
            # instr is literal: user '%'/'_' do not become broad wildcards.
            clauses.append('(instr(title,?)>0 OR instr(next_step,?)>0)'); params.extend((query, query))
        where = ' AND '.join(clauses)
        with self.r.db() as db:
            total = db.execute('SELECT COUNT(*) FROM todo_tasks WHERE ' + where, params).fetchone()[0]
            rows = db.execute('SELECT * FROM todo_tasks WHERE ' + where +
                              " ORDER BY CASE WHEN due IS NOT NULL AND due<=? THEN 0 ELSE 1 END,"
                              "CASE priority WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END,"
                              'CASE WHEN due IS NULL THEN 1 ELSE 0 END,due,id LIMIT ?',
                              (*params, now, limit)).fetchall()
        return {'now': Reminders.date(now), 'tasks': [self.public(row) for row in rows],
                'total': total, 'truncated': total > limit, 'delivery': 'none',
                'note': '待办到期进入后台关注清单，由模型结合目标调用工具推进。'}

    def due_wake_needed(self, db, now=None):
        """Runtime's five-minute precheck; no model call, send, or mutation."""
        now = time.time() if now is None else now
        return bool(db.execute("SELECT 1 FROM todo_tasks WHERE chat=? AND status IN "
                               "('pending','in_progress','blocked') AND due IS NOT NULL "
                               'AND due<=? AND review_after<=? LIMIT 1',
                               (self.r.owner, now, now)).fetchone())

    def wake_snapshot(self, db, now=None, *, include_undated=True, limit=5):
        """Bounded candidates. Call only for ordinary or dedicated todo wake.

        A dedicated due wake passes include_undated=False. Future dated tasks
        stay out of prompts; undated tasks wait for normal background leisure.
        """
        now = time.time() if now is None else now
        if type(limit) is not int or not 1 <= limit <= 10:
            raise ValueError('后台候选数量需1至10')
        undated = ' OR due IS NULL' if include_undated else ''
        rows = db.execute("SELECT * FROM todo_tasks WHERE chat=? AND status IN "
                          "('pending','in_progress','blocked') AND review_after<=? "
                          'AND ((due IS NOT NULL AND due<=?)' + undated + ') '
                          "ORDER BY CASE WHEN due IS NOT NULL THEN 0 ELSE 1 END,"
                          "CASE priority WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END,due,id LIMIT ?",
                          (self.r.owner, now, now, limit)).fetchall()
        return [self.public(row) for row in rows]

    def mark_reviewed(self, db, identifiers, now=None, *, interval_seconds=REVIEW_INTERVAL):
        """Runtime calls in its wake claim transaction, not from model input.

        Defers another autonomous look; does not mark progress/completion or
        pretend the model executed anything. Ordinary rechecks default to 90m.
        """
        now = time.time() if now is None else now
        if type(interval_seconds) is not int or not 300 <= interval_seconds <= 86400:
            raise ValueError('待办复查间隔需300至86400秒')
        if not isinstance(identifiers, (list, tuple)) or len(identifiers) > 10:
            raise ValueError('一次最多标记10条后台候选')
        for identifier in identifiers:
            if type(identifier) is not int or identifier < 1:
                raise ValueError('后台候选需真实待办编号')
            db.execute("UPDATE todo_tasks SET last_reviewed=?,review_after=? WHERE id=? AND chat=? "
                       "AND status IN ('pending','in_progress','blocked')",
                       (now, now + interval_seconds, identifier, self.r.owner))

    @staticmethod
    def prompt(tasks):
        return ('[个人待办候选]\n'
                '结合原约定和实际进展，挑一小步推进，留下结果与证据。'
                '到期事项开始关注，无时间的按优先级在空档惦记。'
                '有实质结果或需要jg决定时简短联系，也可以安静整理。\n'
                + json.dumps(tasks, ensure_ascii=False))
