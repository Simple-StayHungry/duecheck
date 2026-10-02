"""Clean progressive pixel supervisor for DueCheck.

The multithreaded web process starts this supervisor once for Stage A.  This
supervisor deliberately imports no OpenCV / Vision frameworks.  It launches
short-lived native workers from a clean single-threaded process, and every child
writes one durable row after each screenshot.

This removes the old failure mode where the web process repeatedly launched a
native worker at 16-image boundaries.  It also means that if one screenshot
wedges native image matching, only that screenshot is retried/isolated; completed
rows in the same chunk are never lost.

    python pixel_stream_worker.py <job.json> <rows.jsonl> <status.json>
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
CHUNK_WORKER = BASE / 'pixel_chunk_worker.py'
CHUNK_SIZE = 16
# This is a no-progress limit, not a whole-chunk wall-clock limit.  A healthy
# worker resets it after every screenshot, so a 191-image task may run for as
# long as needed without ever being mistaken for a stall.
ITEM_NO_PROGRESS_TIMEOUT = 12.0
SINGLE_RETRY_TIMEOUT = 10.0
_CURRENT = None
_STOP = False


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            json.dump(payload, fh, ensure_ascii=False)
            fh.flush()
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


class _Spawned:
    """Minimal Popen-compatible wrapper for explicit posix_spawn children."""

    def __init__(self, pid):
        self.pid = int(pid)
        self.returncode = None

    def poll(self):
        if self.returncode is not None:
            return self.returncode
        try:
            got, status = os.waitpid(self.pid, os.WNOHANG)
        except ChildProcessError:
            self.returncode = 0
            return self.returncode
        if got == 0:
            return None
        try:
            self.returncode = os.waitstatus_to_exitcode(status)
        except Exception:
            self.returncode = 0 if status == 0 else 1
        return self.returncode

    def terminate(self):
        if self.poll() is not None:
            return
        try:
            os.kill(self.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    def kill(self):
        if self.poll() is not None:
            return
        try:
            os.kill(self.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def wait(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + float(timeout)
        while True:
            rc = self.poll()
            if rc is not None:
                return rc
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(str(self.pid), timeout)
            time.sleep(0.02)


def _spawn_child(argv, errh):
    """Launch native children without any fork path on POSIX/macOS."""
    argv = [str(x) for x in argv]
    if os.name == 'posix' and hasattr(os, 'posix_spawn'):
        devnull = os.open(os.devnull, os.O_RDWR)
        try:
            actions = [
                (os.POSIX_SPAWN_DUP2, devnull, 0),
                (os.POSIX_SPAWN_DUP2, devnull, 1),
                (os.POSIX_SPAWN_DUP2, errh.fileno(), 2),
            ]
            pid = os.posix_spawn(argv[0], argv, dict(os.environ), file_actions=actions)
            return _Spawned(pid)
        finally:
            os.close(devnull)
    return subprocess.Popen(
        argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=errh, close_fds=True)


def _stop_handler(_sig, _frame):
    global _STOP, _CURRENT
    _STOP = True
    proc = _CURRENT
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=.4)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    raise SystemExit(143)


def _stop_child(proc) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=.35)
    except Exception:
        try:
            proc.kill()
            proc.wait(timeout=.7)
        except Exception:
            pass


def _emit(out, status_file: Path, index: int, row: dict) -> None:
    row = dict(row or {})
    row['index'] = int(index)
    out.write(json.dumps(row, ensure_ascii=False) + '\n')
    out.flush()
    _atomic_json(status_file, {'index': int(index), 'state': 'done'})


def _run_progressive_chunk(items, global_base: int, out, global_status: Path,
                           timeout: float):
    """Run one short-lived native chunk and stream every completed image.

    Returns ``(missing_local_index, error)``.  ``missing_local_index`` is None
    only when every screenshot in the chunk produced a durable row.
    """
    global _CURRENT
    if _STOP:
        raise SystemExit(143)

    with tempfile.TemporaryDirectory(prefix='duecheck-pixel-chunk-') as td:
        td = Path(td)
        job = td / 'job.json'
        rows_path = td / 'rows.jsonl'
        child_status = td / 'status.json'
        err = td / 'stderr.txt'
        job.write_text(json.dumps({'items': items}, ensure_ascii=False), 'utf-8')

        seen = set()
        offset = 0
        buffer = b''
        last_token = None
        last_progress = time.monotonic()
        timeout_error = None

        def drain_rows():
            nonlocal offset, buffer, last_progress
            if not rows_path.exists():
                return
            with rows_path.open('rb') as fh:
                fh.seek(offset)
                chunk = fh.read()
                offset = fh.tell()
            if not chunk:
                return
            buffer += chunk
            while b'\n' in buffer:
                raw, buffer = buffer.split(b'\n', 1)
                if not raw.strip():
                    continue
                try:
                    row = json.loads(raw.decode('utf-8'))
                    local = int(row.get('index'))
                except Exception:
                    continue
                if local < 0 or local >= len(items) or local in seen:
                    continue
                seen.add(local)
                _emit(out, global_status, global_base + local, row)
                last_progress = time.monotonic()

        with err.open('w', encoding='utf-8') as errh:
            proc = _spawn_child(
                [sys.executable, str(CHUNK_WORKER), str(job), str(rows_path), str(child_status)],
                errh)
            _CURRENT = proc
            try:
                while True:
                    if _STOP:
                        _stop_child(proc)
                        raise SystemExit(143)

                    drain_rows()
                    try:
                        if child_status.exists():
                            status = json.loads(child_status.read_text('utf-8'))
                            token = (int(status.get('index', -1)), str(status.get('state', '')))
                            if token != last_token:
                                last_token = token
                                last_progress = time.monotonic()
                                local = token[0]
                                if 0 <= local < len(items) and token[1] == 'working':
                                    _atomic_json(global_status, {
                                        'index': global_base + local,
                                        'state': 'working',
                                    })
                    except Exception:
                        pass

                    rc = proc.poll()
                    if rc is not None:
                        drain_rows()
                        break
                    if time.monotonic() - last_progress >= float(timeout):
                        timeout_error = f'单张像素定位连续 {timeout:g} 秒无进展'
                        _stop_child(proc)
                        drain_rows()
                        break
                    time.sleep(.05)
            finally:
                _CURRENT = None

        missing = next((i for i in range(len(items)) if i not in seen), None)
        if missing is None:
            return None, None

        try:
            detail = err.read_text('utf-8', errors='ignore')[-240:]
        except Exception:
            detail = ''
        if timeout_error:
            reason = timeout_error
        elif proc.returncode not in (0, None):
            reason = detail or f'像素子进程异常退出（{proc.returncode}）'
        else:
            reason = detail or '像素子进程未返回完整结果'
        return missing, reason


def main(job_path: str, rows_path: str, status_path: str) -> int:
    signal.signal(signal.SIGTERM, _stop_handler)
    signal.signal(signal.SIGINT, _stop_handler)
    jobs = (json.loads(Path(job_path).read_text('utf-8')).get('items') or [])
    rows_file = Path(rows_path)
    status_file = Path(status_path)

    with rows_file.open('a', encoding='utf-8', buffering=1) as out:
        base = 0
        while base < len(jobs):
            if _STOP:
                return 143
            chunk = jobs[base:base + CHUNK_SIZE]
            _atomic_json(status_file, {
                'index': base, 'state': 'working',
                'from': base, 'to': base + len(chunk) - 1,
            })
            missing, error = _run_progressive_chunk(
                chunk, base, out, status_file, ITEM_NO_PROGRESS_TIMEOUT)
            if missing is None:
                base += len(chunk)
                continue

            # Preserve every row before the stall, retry only the first unfinished
            # screenshot in a brand-new native process, then continue from the
            # following screenshot.  No batch-wide rollback exists in r21.
            failed_global = base + int(missing)
            one = [jobs[failed_global]]
            _atomic_json(status_file, {'index': failed_global, 'state': 'single-retry'})
            one_missing, one_error = _run_progressive_chunk(
                one, failed_global, out, status_file, SINGLE_RETRY_TIMEOUT)
            if one_missing is not None:
                _emit(out, status_file, failed_global, {
                    'ok': False,
                    'error': '单张像素定位已隔离：' + str(one_error or error or '未知异常')[:180],
                })
            base = failed_global + 1

    _atomic_json(status_file, {'index': len(jobs), 'state': 'complete'})
    return 0


if __name__ == '__main__':
    if len(sys.argv) != 4:
        raise SystemExit('usage: pixel_stream_worker.py <job.json> <rows.jsonl> <status.json>')
    raise SystemExit(main(sys.argv[1], sys.argv[2], sys.argv[3]))
