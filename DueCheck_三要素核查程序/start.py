"""DueCheck launcher: enforce one local instance, then open the current build."""
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

BASE = Path(__file__).resolve().parent
os.chdir(BASE)
import app

PORTS = range(8766, 8796)


def get_status(port):
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=.25) as r:
            return json.load(r)
    except Exception:
        return {}


def listener_pids(port):
    """Return listener PIDs only after /health identified a DueCheck service."""
    try:
        if shutil.which('lsof'):
            out = subprocess.check_output(
                ['lsof', '-tiTCP:' + str(port), '-sTCP:LISTEN'], text=True,
                stderr=subprocess.DEVNULL, timeout=1.5)
            return [int(x) for x in out.split() if x.isdigit()]
        if shutil.which('fuser'):
            out = subprocess.check_output(
                ['fuser', '-n', 'tcp', str(port)], text=True,
                stderr=subprocess.DEVNULL, timeout=1.5)
            return [int(x) for x in out.split() if x.isdigit()]
    except Exception:
        pass
    return []


def stop_previous_instances():
    """Latest launch takes ownership of local DueCheck.

    Old versions used a new port every time, so several native Vision batches
    could remain alive together and compete until the newest page appeared
    frozen. We only terminate listeners whose own /health endpoint identifies
    them as DueCheck; unrelated localhost services are never touched.
    """
    unresolved = []
    for p in PORTS:
        status = get_status(p)
        if status.get('app') != 'DueCheck':
            continue
        pids = [pid for pid in listener_pids(p) if pid != os.getpid()]
        if not pids:
            unresolved.append(p)
            continue
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except Exception:
                unresolved.append(p)
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and get_status(p).get('app') == 'DueCheck':
            time.sleep(.10)
        if get_status(p).get('app') == 'DueCheck':
            unresolved.append(p)
    if unresolved:
        ports = '、'.join(str(x) for x in sorted(set(unresolved)))
        raise SystemExit('检测到旧 DueCheck 仍在运行（端口 ' + ports + '）。请关闭旧 DueCheck 的终端窗口后重新双击启动。')


def stop_orphan_workers():
    """Kill DueCheck scan/OCR children left by older builds only."""
    try:
        out = subprocess.check_output(['ps', '-axo', 'pid=,command='], text=True,
                                      stderr=subprocess.DEVNULL, timeout=2.0)
    except Exception:
        return
    victims = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            pid_s, cmd = line.split(None, 1)
            pid = int(pid_s)
        except Exception:
            continue
        low = cmd.lower()
        if pid == os.getpid():
            continue
        is_worker = ('scan_worker.py' in low or 'ocr_worker.py' in low or 'pixel_batch_worker.py' in low or 'pixel_chunk_worker.py' in low or 'pixel_stream_worker.py' in low)
        is_duecheck = ('duecheck' in low or '三要素核查' in cmd)
        if is_worker and is_duecheck:
            victims.append(pid)
    for pid in victims:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except Exception:
            pass


stop_previous_instances()
stop_orphan_workers()

# Start the current build on the first free DueCheck port.
for port in PORTS:
    sock = socket.socket()
    try:
        sock.bind(('127.0.0.1', port)); sock.close(); break
    except OSError:
        sock.close()
else:
    raise SystemExit('本机端口被占用，请关闭其他本地网页程序再重试')


def open_when_ready():
    for _ in range(120):
        if get_status(port).get('build') == app.BUILD:
            webbrowser.open(f'http://127.0.0.1:{port}')
            return
        time.sleep(.25)


threading.Thread(target=open_when_ready, daemon=True).start()
print(f'DueCheck  http://127.0.0.1:{port}\n关闭此窗口会停止网页服务；任务会自动保存。')
import uvicorn
uvicorn.run(app.app, host='127.0.0.1', port=port, log_level='warning')
