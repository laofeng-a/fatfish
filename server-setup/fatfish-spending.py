#!/usr/bin/python3
"""Root-only spending switch. Never resets the ledgers or changes token limits."""
import json,os,pwd,sys
from pathlib import Path

def main():
    if os.geteuid()!=0:raise SystemExit('Run with sudo.')
    command=sys.argv[1] if len(sys.argv)==2 else 'status'
    path=Path('/etc/picoclaw-spending-control.json')
    saved=path.with_name('picoclaw-spending-control.restore.json')
    def write(target,value,mode):
        pending=target.with_suffix('.pending');pending.write_text(json.dumps(value,indent=2)+'\n')
        os.chown(pending,0,pwd.getpwnam('picobudget').pw_gid);os.chmod(pending,mode);os.replace(pending,target)
    current=json.loads(path.read_text()) if path.exists() else {'mode':'enforced'}
    if command=='off':
        if current.get('mode')!='accounting_only':
            write(saved,{'existed':path.exists(),'original':current},0o600)
        write(path,{'mode':'accounting_only'},0o644)
    elif command=='restore':
        if saved.exists():
            previous=json.loads(saved.read_text())
            if previous['existed']:write(path,previous['original'],0o644)
            else:path.unlink(missing_ok=True)
        elif current.get('mode')=='accounting_only':raise SystemExit('Restore snapshot is missing.')
    elif command!='status':raise SystemExit('Usage: fatfish-spending off|restore|status')
    current=json.loads(path.read_text()) if path.exists() else {'mode':'enforced'}
    print(json.dumps({'mode':current['mode'],'accounting':True,'saved_daily_limits_cny':{'model':9.5,'search':.5,'background_subset':2},'max_output_tokens':8192}))

if __name__=='__main__':main()
