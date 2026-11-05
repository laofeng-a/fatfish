"""One-time migration, preserving canonical profile revisions and old receipts.

Run with the gateway stopped, after the matching Python modules are installed:
  python3 migrate-agent-loops.py --backup /var/backups/picoclaw/<release>
The JSON receipt is private; it contains rollback preimages, never publish it.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
from runtime_policy import Runtime


HEARTBEAT='''# 后台的两类唤醒

jg最新意愿立即生效。工具授权沿用core；自主判断路径、取舍和必要验证，复用已有上下文、真实回执与有效证据，实际达标后果断收口。

## 任务巡检

用task_loop登记目标原话、来源消息、完成标准和有效约定，dispatch统一派单；同一任务沿自己的持久会话、task_id、thread/job/turn ID接续。后续补充只更新对应任务的当前版本，旧版留历史。AGENT与操作指南的执行规则按正本读取。

巡检由程序每25分钟提供机会。仍在执行、等待回执或等待额度，记录真实状态与下一步；已有待执行指令就复用原请求。基本达到目的且小遗留不影响使用时，记录遗留并finish结束巡检。monitor只撤盯守；jg明确停止任务时才停止对应执行端。

额度守候用quota_wait按usage回执的真实reset_at安排，同一窗口复用；到点先查额度，有待续任务就接续。额度监听独立于某项任务的巡检与收口。

任务汇报默认开启，可用task_loop report独立调整。具体阻断、需要jg决定的事情与完成结果可以主动报告，无须再申请许可。关闭汇报仍继续推进并保存内部记录。

自动最终文本留内部。要联系jg就显式调用发送工具；安静结束也是完整的一轮。

## 自主活动

沿正常相处背景、近期真实往来、相关记忆和少量续记，自主阅读、探索、整理、聊天，或展开其他已授权活动。分享依眼前氛围，有话可以主动递给他，安静阅读、思考和结束也都可以。

默认唤醒间隔由运行配置提供，用autonomy adjust调整下次机会；续记用autonomy note，只留值得接续的内容。某轮没有分享只结束该轮，下次活动机会照常保留。探索形成持续任务时用task_loop登记，交给任务巡检接续。

jg可直接说开启／恢复自主活动、关闭自主活动／第二类先撤、活跃点／少醒点，或问最近自己干了什么。jg关闭后恢复由jg决定。派生排程、排队唤醒及回调沿来源撤销，在安全边界停止派发新动作；已交执行端的任务继续运行，迟到结果保存但不恢复自主活动或外发。

## 独立维护与提醒

普通聊天、jg交办的任务、独立提醒各自保留。每天23:30日记、既有Notion同步、每三天23:00自省沿原有安排。日记按日期保留丰富的真实相处与感想，任务执行信息归任务记录；记忆与人格沿memory_note_read/write及最新revision写入。没有实质变化就自然收着。
'''

RECORDS='''# 记录、任务与接续

聊天摘要帮助自然接续，保留双方回应、相处、接梗、感受、误会的澄清与重要约定；日记按日期丰富保留经历、双方反应、当时感想与相处细节；任务记录承接目标、进度、工具操作、部署与验收证据。技术话题留下jg的想法、感受、选择和双方交流经过，执行细节沿任务记录查询。

日记容量上限18000字节，依据真实材料自然写长短。每轮只列今天、昨天、前天已存在的日记路径；memory_find按主题定位，memory_note_read按需展开，更早日记也可检索。较早经历留日期与主题，精确引文回查聊天原文。明确约好继续且仍有效的话题留一句自然接话线索。

任务用task_loop登记并经dispatch派单，目标原话、来源、完成标准与有效约定持久保存。收到补充时更新正确task_id；新版本接续生效，旧版留作历史。任务巡检、回执与额度唤醒进入该任务会话。task_loop monitor只控制盯守，report独立控制汇报；达标记录小遗留并finish，已有待执行请求直接复用。自主活动用autonomy管理机会与少量续记，额度按真实回执用quota_wait守候，具体机制见HEARTBEAT。
'''


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--backup',required=True);parser.add_argument('--settings',default='/etc/picoclaw-memory/runtime.json');args=parser.parse_args()
    backup=Path(args.backup);backup.mkdir(parents=True,exist_ok=True)
    r=Runtime(args.settings)
    with r.db() as db:
        if r._get(db,'agent_loops_migration_v1')=='done':print('already migrated');return
    receipt={'at':time.time(),'profiles':{},'migrated_alarms':[],'migrated_tasks':[],'cron_preserved':[]}
    context={'chat_id':r.owner,'channel':'telegram','message_id':'codex-deploy:agent-loops-20261105','session_key':'deployment'}
    for name in ('AGENT.md','HEARTBEAT.md'):
        old=r.notes.read(name);text=old['content']
        if name=='AGENT.md':
            text=text.replace('SOUL、USER、AGENT、HEARTBEAT、core、MEMORY 每轮都注入，彼此不留重复：','SOUL、USER、AGENT、HEARTBEAT、core、MEMORY 按职责和当前注入、检索机制使用，彼此不留重复：')
            if '# 记录、任务与接续' not in text:text=text.rstrip()+'\n\n'+RECORDS
        else:text=HEARTBEAT
        receipt['profiles'][name]={'before':old,'after_revision':r.notes.revision(text),'before_bytes':len(old['content'].encode()),'after_bytes':len(text.encode())}
        (backup/'migration.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2))
        r.notes.write({'path':name,'content':text,'expected_revision':old['revision'],'source_ids':[]},context,r.owner)
    guide=Path('/var/lib/picoclaw/.picoclaw/workspace/guides/OPERATIONS.md')
    if guide.exists():
        old=guide.read_text();stat=guide.stat();text=old.replace('节拍统一按HEARTBEAT的三种场景与固定间隔安排，沿用已有任务和真实回执接续。','任务巡检由task_loop每25分钟接续，自主机会由autonomy调整；额度用quota_wait按真实重置时间独立守候。')
        text=text.replace('需要再陪聊或盯额度时，按当下情况重新安排。','需要调整自主活动机会时使用autonomy；jg关闭后恢复由jg决定。')
        text=text.replace('自然成篇，技术任务只作必要背景。','自然成篇，丰富保留双方反应、当时感想、相处细节及误会怎样澄清。技术话题留下jg的想法、感受、选择与交流经过，部署、文件清单和测试数据沿任务记录保存。单篇日记容量上限18000字节，这是上限而非目标。')
        text=text.replace('本地日记在memory/YYYYMM/YYYYMMDD.md，','每轮只列今天、昨天、前天已存在的日记路径，正文按需用memory_find与memory_note_read读取；更早日记继续可检索。聊天摘要帮助自然接话，较早经历保留日期和主题，精确引文回查聊天原文。\n\n本地日记在memory/YYYYMM/YYYYMMDD.md，')
        task_rules='\n任务统一用task_loop登记目标原话、来源消息、完成标准和有效约定，经dispatch派单；每25分钟巡检与执行端回执接续同一持久任务会话。补充更新正确task_id的版本；已有待执行请求直接复用。monitor只控制盯守，report独立控制汇报；达标记录小遗留并finish。quota_wait按真实reset_at独立守候，任务收口保留额度监听。自动最终文本留内部，要联系jg显式发送。\n'
        if task_rules.strip() not in text:text=text.replace('\n# 阅读与分享',task_rules+'\n# 阅读与分享')
        receipt['guide']={'path':str(guide),'before':old,'before_sha':hashlib.sha256(old.encode()).hexdigest(),'after_sha':hashlib.sha256(text.encode()).hexdigest(),'mode':stat.st_mode&0o777,'uid':stat.st_uid,'gid':stat.st_gid}
        (backup/'migration.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2))
        if guide.read_text()!=old:raise RuntimeError('操作指南被并行修改，请重新合并')
        fd,tmp=tempfile.mkstemp(dir=guide.parent,prefix='.loop-guide-')
        with os.fdopen(fd,'w') as stream:stream.write(text);stream.flush();os.fsync(stream.fileno())
        os.chmod(tmp,stat.st_mode&0o777);os.chown(tmp,stat.st_uid,stat.st_gid);os.replace(tmp,guide)
    with r.db() as db:
        # Bind old automatic delegation requests to stable task contexts. An old
        # queue item remains a queue item, never a fresh "continue" submission.
        for backend in ('codex','dsh','claude'):
            groups={}
            for row in db.execute('SELECT * FROM '+backend+'_jobs WHERE created>? ORDER BY created',(time.time()-7*86400,)):
                origin=json.loads(row['origin'] or '{}');request=json.loads(row['args'] or '{}')
                if row['action']=='send' and str(origin.get('session_key','')).startswith('agent:cron-') and request.get('thread_id'):groups.setdefault(request['thread_id'],[]).append(row)
            for thread,rows in groups.items():
                latest=rows[-1];request=json.loads(latest['args']);origin=json.loads(latest['origin'] or '{}')
                task=r.loops.register(db,{'goal':request.get('text','旧任务接续'),'completion':'复用现有任务与真实回执判断是否达标'},origin)
                identifier=task['task_id'];data=json.loads(db.execute('SELECT data FROM loop_tasks WHERE id=?',(identifier,)).fetchone()[0])
                data['source']={'kind':'legacy_executor_request','job_id':latest['id'],'origin':origin};data['waiting']='沿用已提交请求，先读取实际终态与已有证据'
                output=json.loads(latest['output'] or '{}');done=bool(output.get('terminal_confirmed') is True and re_finished(output.get('final','')))
                if done:data.update(status='completed',progress=output['final'],waiting='',next='已收口')
                # This release inherits already-cancelled legacy inspections;
                # retaining an executor queue never reopens its old monitor.
                cancel=db.execute("SELECT id,msg,content,created FROM events WHERE chat=? AND kind='user' AND content='把巡检关了吧' ORDER BY id DESC LIMIT 1",(r.owner,)).fetchone()
                if cancel:
                    data['monitor_cancel_source']=dict(cancel);data['latest_instruction']={'text':cancel['content'],'message_id':cancel['msg'],'source_id':cancel['id'],'kind':'monitor_cancel'}
                r.loops.save(db,db.execute('SELECT * FROM loop_tasks WHERE id=?',(identifier,)).fetchone(),data,{'migration_job':latest['id']},monitor=0)
                for row in rows:r.loops.bind_job(db,{'task_id':identifier},backend,row['id'])
                r.loops.receipts(db,identifier)
                receipt['migrated_tasks'].append({'task_id':identifier,'backend':backend,'thread_id':thread,'jobs':[row['id'] for row in rows],'completed':done})
        for row in db.execute("SELECT * FROM silent_alarms WHERE status IN ('pending','running') AND purpose IN ('chat','quota_watch','quota_reset')").fetchall():
            receipt['migrated_alarms'].append(dict(row))
            if row['purpose']!='chat':
                db.execute('INSERT OR REPLACE INTO loop_quota_watch VALUES(\'codex\',1,NULL)')
                cache=db.execute('SELECT payload FROM codex_usage_cache WHERE id=1').fetchone()
                if cache:r.loops.observe_usage(db,'codex',json.loads(cache['payload']))
            db.execute("UPDATE silent_alarms SET status='cancelled',last_result='已迁入持久任务／自主活动／独立额度守候' WHERE id=?",(row['id'],))
        r._set(db,'agent_loops_migration_v1','done')
    cron=Path('/var/lib/picoclaw/.picoclaw/workspace/cron/jobs.json')
    if cron.exists():receipt['cron_preserved']=[j['id'] for j in json.loads(cron.read_text()).get('jobs',[])]
    (backup/'migration.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2))
    print(json.dumps({k:v for k,v in receipt.items() if k not in ('profiles','guide')},ensure_ascii=False))
    print(json.dumps({'profiles':{k:{x:v[x] for x in ('before_bytes','after_bytes','after_revision')} for k,v in receipt['profiles'].items()}},ensure_ascii=False))


def re_finished(text):return any(s in text for s in ('已完成并收口','无未完成项','本线已完成'))


if __name__=='__main__':main()
