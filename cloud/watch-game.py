#!/usr/bin/env python3
"""Start the inspector UI and observation bridge after SWGoH starts (Linux)."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from urllib.parse import urlparse
from urllib.request import Request, build_opener, ProxyHandler

ROOT = Path(__file__).resolve().parent.parent
STOP = False


def game_processes(proc=Path('/proc')):
    found = set()
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if entry.stat().st_uid != os.getuid():
                continue
            comm = (entry/'comm').read_text().strip().casefold()
            argv = (entry/'cmdline').read_bytes().split(b'\0')
            first = argv[0].decode(errors='replace').replace('\\', '/').rsplit('/', 1)[-1].casefold()
            if comm != 'swgoh.exe' and first != 'swgoh.exe':
                continue
            fields = (entry/'stat').read_text().rsplit(')', 1)[1].split()
            if fields[0] == 'Z':
                continue
            found.add((int(entry.name), fields[19]))  # PID + kernel start time, safe against PID reuse.
        except (OSError, IndexError):
            continue
    return found


def ready(url_file):
    try:
        url = urlparse(Path(url_file).read_text().strip())
        if url.scheme != 'http' or url.hostname != '127.0.0.1' or url.port != 18181:
            return False
        key = url.fragment
        if len(key) != 48 or any(c not in '0123456789abcdefABCDEF' for c in key):
            return False
        request = Request('http://127.0.0.1:18181/api/http?enabled=1', headers={'X-Inspector-Key': key})
        with build_opener(ProxyHandler({})).open(request, timeout=2) as response:
            data = json.load(response)
            return response.status == 200 and not data.get('error')
    except (OSError, ValueError):
        return False


def stop_child(child):
    if child is None or child.poll() is not None:
        return
    child.terminate()
    try:
        child.wait(timeout=8)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait()


def watch(delay, opener, config, root=ROOT):
    global STOP
    STOP = False
    state = root/'cloud/.state'
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (state/'game-watch.lock').open('a') as lock:
        os.chmod(lock.name, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('Game watcher already running', flush=True)
            return
        settings = json.loads(config.read_text())
        child = None
        current = set()
        deadline = None
        opened = False
        next_retry = 0
        warned = False
        env = dict(os.environ, PYTHONPATH=str(root/'cloud'))
        print(f'Watching SWGoH.exe; startup delay {delay:g}s', flush=True)
        try:
            while not STOP:
                active = game_processes()
                now = time.monotonic()
                if active and not current:
                    deadline = now + delay
                    opened = False
                    warned = False
                    next_retry = 0
                    print('SWGoH detected; waiting for startup delay', flush=True)
                if not active and current:
                    stop_child(child)
                    child = None
                    deadline = None
                    print('SWGoH closed; bridge stopped', flush=True)
                current = active
                if active and deadline is not None and now >= deadline:
                    if child is None or child.poll() is not None:
                        if now >= next_retry:
                            child = subprocess.Popen([
                                str(root/'work/cloud-pg-venv/bin/python'), '-u', '-m', 'swgoh_cloud.bridge',
                                '--config', str(config)], cwd=root, env=env)
                            next_retry = now + 15
                            print('Cloud bridge started', flush=True)
                    if not opened and ready(settings['inspector_url_file']):
                        # Suppress browser output: the local inspector URL contains a private key.
                        result = subprocess.run([sys.executable, str(opener)], cwd=root, env=env,
                                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
                        if result.returncode == 0:
                            opened = True
                            print('Inspector browser opened', flush=True)
                    elif not opened and not warned:
                        print('Waiting for inspector server; bridge will retry automatically', flush=True)
                        warned = True
                time.sleep(1)
        finally:
            stop_child(child)
            print('Game watcher stopped', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--delay', type=float, default=20)
    parser.add_argument('--opener', type=Path, required=True)
    parser.add_argument('--config', type=Path, default=ROOT/'cloud/.state/bridge.local.json')
    args = parser.parse_args()
    if args.delay < 0 or not args.opener.is_file() or not args.config.is_file():
        parser.error('Provide an existing opener/config and a nonnegative startup delay')
    def terminate(*_):
        global STOP
        STOP = True
    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    watch(args.delay, args.opener.resolve(), args.config.resolve())


if __name__ == '__main__':
    main()
