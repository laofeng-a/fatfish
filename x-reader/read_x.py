"""Small, bounded X feed reader. This entry point exposes no write commands."""

import argparse
import datetime
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parent
SESSION_FILE = ROOT / ".secrets" / "x-session.json"
RESULT_FILE = ROOT / "state" / "latest-feed.json"


def peak_memory_mib(process):
    """Read the child process high-water mark, without extra dependencies."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        query = ctypes.windll.psapi.GetProcessMemoryInfo
        query.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        query.restype = wintypes.BOOL
        if query(int(process._handle), ctypes.byref(counters), counters.cb):
            return round(counters.PeakWorkingSetSize / 1024**2, 2)
    elif sys.platform.startswith("linux"):
        import resource

        return round(resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss / 1024, 2)
    return None


def count_posts(data):
    if isinstance(data, list):
        return len(data)
    if isinstance(data, dict):
        for key in ("tweets", "posts", "items", "data"):
            value = data.get(key)
            if isinstance(value, list):
                return len(value)
    return None


def main():
    parser = argparse.ArgumentParser(description="Read a small batch of X posts.")
    parser.add_argument("--kind", choices=("for-you", "following"), default="for-you")
    parser.add_argument("--limit", type=int, choices=range(1, 21), default=20)
    parser.add_argument("--check", action="store_true", help="Local checks only; no login or network.")
    args = parser.parse_args()
    worker = ROOT / "fetch_feed.py"

    if args.check:
        print(json.dumps({
            "python": sys.version.split()[0],
            "twitter_cli": importlib.metadata.version("twitter-cli"),
            "entry_point_exists": worker.is_file(),
            "session_configured": SESSION_FILE.is_file(),
            "allowed_action": "feed read only",
            "maximum_posts_per_call": 20,
            "network_requested": False,
        }, ensure_ascii=False, indent=2))
        return 0

    if not SESSION_FILE.is_file():
        print("X session is not configured. Do not paste cookies into chat.", file=sys.stderr)
        return 2

    session = json.loads(SESSION_FILE.read_text(encoding="utf-8-sig"))
    tokens = [session.get("auth_token"), session.get("ct0")]
    if not all(isinstance(token, str) and token.strip() for token in tokens):
        print("The local X session file is incomplete.", file=sys.stderr)
        return 2

    env = os.environ.copy()
    env.update({
        "TWITTER_AUTH_TOKEN": tokens[0],
        "TWITTER_CT0": tokens[1],
        "PYTHONIOENCODING": "utf-8",
        "NO_COLOR": "1",
    })
    # Fixed local worker + bounded read options. No arbitrary CLI/shell input.
    command = [sys.executable, str(worker), "--kind", args.kind, "--limit", str(args.limit)]
    started = time.monotonic()
    process = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        stdout, stderr = process.communicate(timeout=60)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        print("The X read timed out after 60 seconds.", file=sys.stderr)
        return 3

    metrics = {
        "platform": sys.platform,
        "seconds": round(time.monotonic() - started, 2),
        "reader_peak_memory_mib": peak_memory_mib(process),
    }
    if process.returncode:
        error = stderr.decode("utf-8", errors="replace")
        for token in tokens:
            error = error.replace(token, "[REDACTED]")
        print(error[-2500:], file=sys.stderr)
        return process.returncode

    try:
        posts = json.loads(stdout.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        print("The reader did not return valid JSON; raw output was not printed.", file=sys.stderr)
        return 4

    RESULT_FILE.parent.mkdir(exist_ok=True)
    payload = {
        "fetched_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "feed": args.kind,
        "requested_limit": args.limit,
        "metrics": metrics,
        "data": posts,
    }
    RESULT_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"saved_to": str(RESULT_FILE), "post_count": count_posts(posts), **metrics},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
