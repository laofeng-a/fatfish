"""Small text-only queue for a local Douyin reader. No media reaches this server."""
import hashlib
import json
import re
import secrets
import time
from douyin_reader import request, project_result, VideoReadError

ID = re.compile(r'^[0-9a-f]{24}$')
ERRORS = {'douyin_auth_or_extractor_blocked', 'download_failed', 'transcription_failed',
          'dependencies_missing', 'timeout', 'invalid_request', 'worker_failed', 'result_too_large',
          'media_too_large', 'duration_unknown', 'live_unsupported', 'audio_missing', 'no_speech_detected'}
ERROR_NOTES = {'media_too_large':'本次媒体超过64MiB，需要更小的音轨或视频版本。',
               'duration_unknown':'没有取得视频时长，本次尚未下载音轨。',
               'live_unsupported':'当前工具读取已发布视频，本链接为直播。',
               'audio_missing':'视频信息已取得，但没有下载到可供转写的媒体。',
               'no_speech_detected':'媒体已取得，但音轨没有识别出可用语音。'}


class VideoJobs:
    def __init__(self, runtime):
        self.r = runtime
        with self.r.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS video_jobs(
                    id TEXT PRIMARY KEY, chat TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    request TEXT NOT NULL, state TEXT NOT NULL, claim TEXT, result TEXT,
                    created REAL NOT NULL, updated REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS video_jobs_ready ON video_jobs(state,created);
                CREATE TABLE IF NOT EXISTS video_worker(id INTEGER PRIMARY KEY CHECK(id=1),seen REAL NOT NULL);
            ''')

    @staticmethod
    def view(row):
        result = {'job_id': row['id'], 'state': row['state']}
        if row['result']:
            result['result'] = json.loads(row['result'])
        elif row['state'] in ('queued', 'processing'):
            result['note'] = '本机正在读取视频。可用同一job_id继续查询，不必重复提交链接。'
        return result

    def call(self, args, chat, *, wait_seconds=65):
        if set(args) - {'url', 'mode', 'job_id'}:
            raise VideoReadError('仅接受url、mode或现有job_id')
        now = time.time()
        identifier = args.get('job_id')
        with self.r.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if identifier:
                if not isinstance(identifier, str) or not ID.fullmatch(identifier):
                    raise VideoReadError('无效视频任务编号')
                row = db.execute('SELECT * FROM video_jobs WHERE id=? AND chat=?', (identifier, chat)).fetchone()
                if not row:
                    raise VideoReadError('本会话未找到这项视频读取')
            else:
                clean = request({'url': args.get('url'), 'mode': args.get('mode', 'transcript')})
                fingerprint = hashlib.sha256(json.dumps(clean, sort_keys=True).encode()).hexdigest()
                row = db.execute('SELECT * FROM video_jobs WHERE chat=? AND fingerprint=? AND created>? ORDER BY created DESC LIMIT 1',
                                 (chat, fingerprint, now - 48 * 3600)).fetchone()
                if row and row['state'] == 'failed' and row['updated'] < now - 60:
                    row = None
                if row is None:
                    seen = db.execute('SELECT seen FROM video_worker WHERE id=1').fetchone()
                    if not seen or now - seen['seen'] > 25:
                        return {'state': 'offline', 'note': '电脑视频读取端当前不在线，本次没有读取到视频。'}
                    pending = db.execute("SELECT count(*) FROM video_jobs WHERE state IN ('queued','processing')").fetchone()[0]
                    if pending >= 3:
                        return {'state': 'busy', 'note': '视频读取端正在处理已有请求，请稍后再读。'}
                    identifier = secrets.token_hex(12)
                    db.execute('INSERT INTO video_jobs VALUES(?,?,?,?,?,NULL,NULL,?,?)',
                               (identifier, chat, fingerprint, json.dumps(clean), 'queued', now, now))
                    row = db.execute('SELECT * FROM video_jobs WHERE id=?', (identifier,)).fetchone()
                    db.execute("DELETE FROM video_jobs WHERE state IN ('done','failed') AND created<?", (now - 48 * 3600,))
            identifier = row['id']
        deadline = time.monotonic() + min(65, max(0, wait_seconds))
        while row['state'] in ('queued', 'processing') and time.monotonic() < deadline:
            time.sleep(.5)
            with self.r.db() as db:
                row = db.execute('SELECT * FROM video_jobs WHERE id=? AND chat=?', (identifier, chat)).fetchone()
        return self.view(row)

    def worker(self, data):
        action = data.get('action')
        now = time.time()
        with self.r.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if action == 'video_poll':
                if set(data) - {'action', 'active_id', 'claim'}:
                    raise ValueError('Invalid video poll')
                db.execute('INSERT OR REPLACE INTO video_worker VALUES(1,?)', (now,))
                if data.get('active_id'):
                    active = db.execute('SELECT * FROM video_jobs WHERE id=?', (str(data['active_id']),)).fetchone()
                    if not active or not isinstance(data.get('claim'), str) or not secrets.compare_digest(active['claim'] or '', data['claim']):
                        raise ValueError('Video heartbeat claim mismatch')
                    if active['state'] == 'processing':
                        db.execute('UPDATE video_jobs SET updated=? WHERE id=?', (now, active['id']))
                    return {'job': None}
                # Unknown executions are never requeued automatically.
                db.execute("UPDATE video_jobs SET state='failed',result=?,updated=? WHERE state='processing' AND updated<?",
                           (json.dumps({'state': 'error', 'code': 'timeout', 'note': '本机读取超时，未自动重复处理。'}), now, now - 900))
                row = db.execute("SELECT * FROM video_jobs WHERE state='queued' ORDER BY created LIMIT 1").fetchone()
                if not row:
                    return {'job': None}
                claim = secrets.token_hex(16)
                db.execute("UPDATE video_jobs SET state='processing',claim=?,updated=? WHERE id=?", (claim, now, row['id']))
                return {'job': {'id': row['id'], 'claim': claim, 'request': json.loads(row['request'])}}
            if action != 'video_result' or set(data) - {'action', 'id', 'claim', 'result'}:
                raise ValueError('Invalid video operation')
            row = db.execute('SELECT * FROM video_jobs WHERE id=?', (str(data.get('id', '')),)).fetchone()
            if not row or not isinstance(data.get('claim'), str) or not secrets.compare_digest(row['claim'] or '', data['claim']):
                raise ValueError('Video claim mismatch')
            if row['state'] in ('done', 'failed'):
                return {'accepted': True, 'duplicate': True}
            result = data.get('result')
            if not isinstance(result, dict):
                raise ValueError('Invalid video result')
            if result.get('state') == 'ok':
                result = project_result(result)
                original = json.loads(row['request'])
                if original.get('video_id') and original['video_id'] != result['video_id']:
                    raise ValueError('Video identity mismatch')
                state = 'done'
            else:
                code = result.get('code')
                code = code if code in ERRORS else 'worker_failed'
                result = {'state': 'error', 'code': code, 'note': ERROR_NOTES.get(code,'本次视频读取未成功，没有取得可用视频内容；可说明错误类型，但不要编造内容。')}
                state = 'failed'
            encoded = json.dumps(result, ensure_ascii=False)
            if len(encoded.encode()) > 56000:
                raise ValueError('Video result exceeds transport bound')
            db.execute('UPDATE video_jobs SET state=?,result=?,updated=? WHERE id=?', (state, encoded, now, row['id']))
            return {'accepted': True}
