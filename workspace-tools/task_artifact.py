"""Receive a queued executor artifact, atomically write it, and confirm readback."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
import urllib.request


def rpc(action,payload):
    token=Path(os.environ.get('FATFISH_RUNTIME_TOKEN_FILE','/var/lib/picoclaw/.picoclaw/runtime.token')).read_text().strip()
    root=os.environ.get('FATFISH_RUNTIME_URL','http://127.0.0.1:18792').rstrip('/')
    request=urllib.request.Request(root+'/runtime/'+action,data=json.dumps(payload).encode(),
        headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
    with urllib.request.urlopen(request,timeout=20) as response:return json.load(response)


def receive(job,destination,workspace=None,call=rpc):
    root=Path(workspace or Path(__file__).resolve().parent.parent).resolve()
    target=Path(destination)
    if not target.is_absolute():target=root/target
    target=target.resolve()
    if not target.is_relative_to(root):raise ValueError('目标路径超出当前工作区')
    artifact=call('task_artifact_get',{'job_id':job})
    raw=base64.b64decode(artifact['content_base64'],validate=True)
    digest=hashlib.sha256(raw).hexdigest()
    if len(raw)!=artifact['bytes'] or digest!=artifact['sha256']:raise ValueError('下载文件校验失败')
    target.parent.mkdir(parents=True,exist_ok=True)
    if target.exists() and target.read_bytes()!=raw:
        shutil.copy2(target,target.with_name(target.name+'.before-'+str(time.time_ns())))
    if not target.exists() or target.read_bytes()!=raw:
        fd,temporary=tempfile.mkstemp(dir=target.parent,prefix='.artifact-')
        try:
            with os.fdopen(fd,'wb') as out:out.write(raw);out.flush();os.fsync(out.fileno())
            os.replace(temporary,target)
        finally:
            if os.path.exists(temporary):os.unlink(temporary)
    readback=target.read_bytes()
    return call('task_artifact_commit',{'job_id':job,'path':str(target),'bytes':len(readback),
        'sha256':hashlib.sha256(readback).hexdigest(),'read_back':True})


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['receive']);parser.add_argument('--job',required=True);parser.add_argument('--to',required=True)
    args=parser.parse_args()
    print(json.dumps(receive(args.job,args.to),ensure_ascii=False))
