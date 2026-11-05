"""Rollback code and versioned prompts; retain all post-deployment records."""
import argparse
import hashlib
import json
import lzma
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile


def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def atomic(path,raw,mode=0o644):
    fd,tmp=tempfile.mkstemp(dir=path.parent,prefix='.loop-rollback-')
    with os.fdopen(fd,'wb') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
    os.chmod(tmp,mode);os.replace(tmp,path)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--backup',required=True);args=parser.parse_args()
    backup=Path(args.backup);manifest=json.loads((backup/'deployment.json').read_text());migration=json.loads((backup/'migration.json').read_text())
    root=Path('/opt/picoclaw/fatfish-v1');sys.path.insert(0,str(root))
    binary=Path(manifest['binary']['path'])
    from runtime_policy import Runtime
    r=Runtime();context={'channel':'telegram','chat_id':r.owner,'message_id':'codex-rollback:agent-loops-20261105','session_key':'deployment'}
    for name,info in manifest['modules'].items():
        if digest(root/name)!=info['after_sha256']:raise RuntimeError(name+'已被后续修改，请合并后再回滚')
    if digest(binary)!=manifest['binary']['after_sha256']:raise RuntimeError('主程序已更新，请核对版本后回滚')
    for name,info in migration['profiles'].items():
        if r.notes.read(name)['revision']!=info['after_revision']:raise RuntimeError(name+'已有后续编辑，请保留编辑并合并回滚')
    guide=migration.get('guide')
    if guide and digest(Path(guide['path']))!=guide['after_sha']:raise RuntimeError('操作指南已有后续编辑，请先合并')
    old_binary=lzma.decompress((backup/'picoclaw-before.xz').read_bytes())
    if hashlib.sha256(old_binary).hexdigest()!=manifest['binary']['before_sha256']:raise RuntimeError('回滚二进制校验失败')
    subprocess.run(['systemctl','stop','picoclaw.service'],check=True)
    subprocess.run(['systemctl','stop','picoclaw-memory.service'],check=True)
    try:
        for name,info in migration['profiles'].items():r.notes.write({'path':name,'content':info['before']['content'],'expected_revision':info['after_revision'],'source_ids':[]},context,r.owner)
        if guide:
            p=Path(guide['path']);atomic(p,guide['before'].encode(),guide['mode']);os.chown(p,guide['uid'],guide['gid'])
        with r.db() as db:
            db.execute('UPDATE loop_autonomy SET enabled=0,epoch=epoch+1');db.execute('UPDATE loop_tasks SET monitor=0')
            for alarm in migration.get('migrated_alarms',[]):
                db.execute("UPDATE silent_alarms SET status=?,due=?,interval_seconds=?,purpose=?,last_result=? WHERE id=? AND last_result='已迁入持久任务／自主活动／独立额度守候'",(alarm['status'],alarm['due'],alarm['interval_seconds'],alarm['purpose'],alarm['last_result'],alarm['id']))
            r._set(db,'agent_loops_migration_v1','rolled_back')
        cron=Path('/var/lib/picoclaw/.picoclaw/workspace/cron/jobs.json')
        if cron.exists():
            cron_stat=cron.stat()
            value=json.loads(cron.read_text());removed=[j for j in value.get('jobs',[]) if j.get('payload',{}).get('message','').startswith('[fatfish-session=')]
            value['jobs']=[j for j in value.get('jobs',[]) if j not in removed];(backup/'rollback-cron-preserved.json').write_text(json.dumps(removed,ensure_ascii=False));atomic(cron,json.dumps(value,ensure_ascii=False,indent=2).encode(),cron_stat.st_mode&0o777);os.chown(cron,cron_stat.st_uid,cron_stat.st_gid)
        with tarfile.open(backup/'files-before.tar.gz') as archive:
            for name,info in manifest['modules'].items():
                if info['before_sha256'] is None:(root/name).unlink(missing_ok=True)
                else:
                    raw=archive.extractfile('opt/picoclaw/fatfish-v1/'+name).read()
                    if hashlib.sha256(raw).hexdigest()!=info['before_sha256']:raise RuntimeError('模块备份校验失败：'+name)
                    atomic(root/name,raw)
        atomic(binary,old_binary,0o755)
    finally:
        subprocess.run(['systemctl','start','picoclaw-memory.service'],check=True)
        subprocess.run(['systemctl','start','picoclaw.service'],check=True)
    print(json.dumps({'status':'rolled_back','binary_sha256':digest(binary),'new_history_retained':True,'diaries_retained':True}))


if __name__=='__main__':main()
