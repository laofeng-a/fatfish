"""Small executor files, stored apart from model context, with delivery receipts."""
import base64
import hashlib
import json
from pathlib import PurePosixPath
import time

MAX_ARTIFACT_BYTES=128*1024
WORKSPACE='/var/lib/picoclaw/.picoclaw/workspace'


class TaskArtifacts:
    def __init__(self,runtime):
        self.r=runtime
        with runtime.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS task_artifacts(job_id TEXT PRIMARY KEY,task_id TEXT,source_path TEXT,content BLOB,sha256 TEXT,bytes INTEGER,receipt TEXT,created REAL)')

    def ingest(self,db,job,payload):
        value=dict(payload);raw=base64.b64decode(value.pop('content_base64',''),validate=True)
        digest=hashlib.sha256(raw).hexdigest()
        if len(raw)>MAX_ARTIFACT_BYTES or value.get('bytes')!=len(raw) or value.get('sha256')!=digest:
            raise ValueError('执行端文件大小或哈希不匹配')
        task=db.execute("SELECT task_id FROM loop_bindings WHERE kind='codex' AND key=?",(job['id'],)).fetchone()
        task=task['task_id'] if task else None
        db.execute('INSERT OR REPLACE INTO task_artifacts VALUES(?,?,?,?,?,?,NULL,?)',
            (job['id'],task,str(value.get('path','')),raw,digest,len(raw),time.time()))
        return {'thread_id':value.get('thread_id'),'artifact':{'job_id':job['id'],'source_path':value.get('path'),
            'bytes':len(raw),'sha256':digest,'state':'received_on_bridge'},
            'note':'执行端文件已收取；用 exec 运行 python3 tools/task_artifact.py receive --job '+job['id']+' --to state/文件名，原样写入工作区并生成交付回执。'}

    def action(self,name,data):
        with self.r.db() as db:
            row=db.execute('SELECT * FROM task_artifacts WHERE job_id=?',(data.get('job_id'),)).fetchone()
            if not row:raise ValueError('文件尚未收取；查看原artifact请求状态，无需让Codex重做')
            if name=='task_artifact_get':
                return {'allowed':True,'job_id':row['job_id'],'bytes':row['bytes'],'sha256':row['sha256'],
                    'source_path':row['source_path'],'content_base64':base64.b64encode(row['content']).decode()}
            target=PurePosixPath(str(data.get('path','')))
            if '..' in target.parts or not target.is_relative_to(PurePosixPath(WORKSPACE)):
                raise ValueError('交付路径必须位于当前工作区')
            if data.get('bytes')!=row['bytes'] or data.get('sha256')!=row['sha256'] or data.get('read_back') is not True:
                raise ValueError('交付回执与执行端原件不匹配')
            receipt={'job_id':row['job_id'],'source_host':'desktop','source_path':row['source_path'],
                'target_host':'fatfish','path':str(target),'bytes':row['bytes'],'sha256':row['sha256'],
                'read_back':True,'state':'verified','verified_at':time.time()}
            db.execute('UPDATE task_artifacts SET receipt=? WHERE job_id=?',(json.dumps(receipt,ensure_ascii=False),row['job_id']))
            if row['task_id']:
                task=db.execute('SELECT * FROM loop_tasks WHERE id=?',(row['task_id'],)).fetchone()
                if task:
                    record=json.loads(task['data']);deliveries=record.setdefault('deliveries',{})
                    deliveries[str(target)]=receipt
                    self.r.loops.save(db,task,record,{'kind':'verified_artifact_delivery','job_id':row['job_id']})
                    db.execute("UPDATE codex_chains SET delivery_state='verified' WHERE id IN (SELECT j.chain_id FROM codex_jobs j JOIN loop_bindings b ON b.kind='codex' AND b.key=j.id WHERE b.task_id=?)",(row['task_id'],))
            return {'allowed':True,'accepted':True,'delivery':receipt}
