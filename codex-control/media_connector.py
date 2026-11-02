"""Independent outbound SSH worker for public video reading; no Codex executor."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
from worker import SshBridge, HIDDEN

MAX_OUTPUT = 56000


def read_result(path):
    if path.stat().st_size > MAX_OUTPUT:
        return {'state': 'error', 'code': 'result_too_large'}
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {'state': 'error', 'code': 'worker_failed'}
    if not isinstance(value, dict):
        return {'state': 'error', 'code': 'worker_failed'}
    if value.get('state') != 'ok':
        return {'state': 'error', 'code': str(value.get('reason') or value.get('code') or 'worker_failed')[:80]}
    return value


def run_one(config, job, heartbeat=None):
    request = job['request']
    # Queue normalization includes video_id as metadata; CLI accepts only url/mode.
    request = {key: request[key] for key in ('url', 'mode') if key in request}
    command = [config['python'], config['adapter'], '--video2text-root', config['video2text_root']]
    if config.get('cookie_file'):
        command.extend(['--cookie-file', config['cookie_file']])
    with tempfile.TemporaryDirectory(prefix='video-job-', dir=Path(config['state_file']).parent) as directory:
        output = Path(directory) / 'result.json'
        with output.open('wb') as sink:
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=sink, stderr=subprocess.DEVNULL,
                                       creationflags=HIDDEN)
            process.stdin.write((json.dumps(request, ensure_ascii=False) + '\n').encode('utf-8'))
            process.stdin.close()
            started = time.monotonic()
            next_heartbeat = started
            while process.poll() is None:
                if time.monotonic() - started > 480:
                    if os.name == 'nt':
                        subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=HIDDEN)
                    else:
                        process.kill()
                    process.wait(timeout=10)
                    return {'state': 'error', 'code': 'timeout'}
                if heartbeat and time.monotonic() >= next_heartbeat:
                    try:
                        heartbeat()
                    except Exception:
                        heartbeat = None  # Finish this job and retain its result through SSH reconnect.
                    next_heartbeat = time.monotonic() + 5
                time.sleep(.3)
        return read_result(output)


def run(config):
    state_path = Path(config['state_file'])
    bridge_config = json.loads(Path(config['connector_config']).read_text(encoding='utf-8'))
    token = Path(bridge_config['token_file']).read_text().strip()
    pending = None
    def status(online, busy=False):
        temporary = state_path.with_suffix('.pending')
        temporary.write_text(json.dumps({'online': online, 'busy': busy, 'pid': os.getpid(), 'at': time.time()}), encoding='utf-8')
        os.replace(temporary, state_path)
    while True:
        bridge = None
        try:
            bridge = SshBridge(bridge_config, token)
            while bridge.process.poll() is None:
                if pending:
                    answer = bridge.post(pending)
                    if not answer.get('accepted'):
                        raise RuntimeError('Video receipt not accepted')
                    pending = None
                response = bridge.post({'action': 'video_poll'})
                job = response.get('job')
                status(True, bool(job))
                if job:
                    heartbeat = lambda: bridge.post({'action': 'video_poll', 'active_id': job['id'], 'claim': job['claim']})
                    result = run_one(config, job, heartbeat)
                    pending = {'action': 'video_result', 'id': job['id'], 'claim': job['claim'], 'result': result}
                else:
                    time.sleep(2)
        except Exception:
            status(False, bool(pending))
        finally:
            if bridge:
                bridge.close()
        time.sleep(5)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text(encoding='utf-8'))
    import msvcrt
    lock = Path(config['state_file']).with_suffix('.lock').open('a+b')
    lock.seek(0); lock.write(b'0'); lock.flush(); lock.seek(0)
    try:
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        raise SystemExit(0)
    run(config)
