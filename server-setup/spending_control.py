"""Hot-reloadable daily spending enforcement; accounting always stays enabled."""
import json
import os
from pathlib import Path

def limits_enabled():
    path=Path(os.environ.get('FATFISH_SPENDING_CONTROL','/etc/picoclaw-spending-control.json'))
    if not path.exists():return True
    mode=json.loads(path.read_text()).get('mode','enforced')
    if mode not in ('enforced','accounting_only'):raise ValueError('Invalid spending-control mode')
    return mode=='enforced'
