"""DueCheck local web service.

The UI keeps the original skeleton — import, bind screenshots, preview, undo,
export — but every decision behind it now comes from the four-layer pipeline:

    engine (document mapping) -> spatial (screen decomposition)
      -> page_map (site/column, anchors, regions)
      -> recognition (region reads) -> families (verdicts)

Three facts about a screenshot (query subject, capture time, result feedback) are
kept separate from document issues and from the business conclusion. A missing or
wrong element is stated plainly; only evidence the recogniser genuinely could not
read is handed to the user.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zipfile
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from copy import deepcopy
from datetime import date, datetime
from pathlib import Path, PurePosixPath
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

import page_map
import vision
from engine import (GSXT, MAX_DOC_BYTES, MAX_ZIP_EXPANDED, SUCCESS, Doc,
                    DocumentError, canon_url, export_doc, is_gsxt, norm,
                    pending_conclusion, site_key, strip_placeholder)

BUILD = 'duecheck-r20-core-final-interaction-20260917'
BASE = Path(__file__).resolve().parent
DEFAULT_HOME = Path.home() / ('Library/Application Support/DueCheck/r20-final-interaction' if sys.platform == 'darwin' else '.duecheck/r20-final-interaction')
HOME = Path(os.environ.get('DUECHECK_DATA', str(DEFAULT_HOME))).expanduser().resolve()
TASKS = HOME / 'tasks'
CACHE = HOME / 'observations'
TASKS.mkdir(parents=True, exist_ok=True)
CACHE.mkdir(parents=True, exist_ok=True)
LOCKS = {}
LOCK_GUARD = threading.Lock()
USER_WRITE_GUARD = threading.Lock()
USER_WRITE_PENDING = {}
IMAGE_EXTS = {'.png', '.jpg', '.jpeg', '.webp', '.bmp', '.tiff', '.tif', '.gif'}

# Scan lifecycle is explicit. Importing a new file set must stop the previous
# batch before Apple Vision is allowed to start another one. Otherwise native
# recognisers from the old task keep consuming resources and a new batch can
# appear to freeze at an arbitrary progress count.
SCAN_CONTROLS = {}
SCAN_CONTROL_GUARD = threading.RLock()
# Native Apple Vision is serialised even while cheap pixel preflight remains parallel.
VISION_GATE = threading.Semaphore(1)


class ScanCancelled(RuntimeError):
    pass


def _control_alive(control):
    return bool(control and not control['cancel'].is_set())


def _kill_process(proc):
    if not proc or proc.poll() is not None:
        return
    # Ask supervisor workers to exit cleanly first.  The pixel supervisor's
    # SIGTERM handler also terminates its current native child, preventing an
    # orphaned OpenCV process from surviving a watchdog restart.
    try:
        if hasattr(proc, 'terminate'):
            proc.terminate()
            proc.wait(timeout=.55)
            return
    except Exception:
        pass
    try:
        proc.kill()
    except Exception:
        pass
    try:
        proc.wait(timeout=1.2)
    except Exception:
        pass


class _PosixSpawnProcess:
    """Small Popen-compatible wrapper around an explicit ``os.posix_spawn`` PID.

    The previous r20 build only *kept subprocess eligible* for posix_spawn.  That
    still left launch-path selection inside ``subprocess.Popen``.  On macOS we
    now call posix_spawn directly, so a runtime worker can never fall back to a
    fork from the multithreaded web process.
    """

    def __init__(self, pid):
        self.pid = int(pid)
        self.returncode = None

    def poll(self):
        if self.returncode is not None:
            return self.returncode
        try:
            got, status = os.waitpid(self.pid, os.WNOHANG)
        except ChildProcessError:
            # The child has already been reaped by the OS/runtime.  Treat it as
            # exited rather than leaving the watchdog in an endless wait.
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


def _spawn_runtime_worker(argv, errh):
    """Spawn a killable worker; macOS uses ``os.posix_spawn`` explicitly.

    r20 merely arranged ``Popen`` arguments so CPython *could* choose
    posix_spawn.  The user's repeated stop at the first work of the third document
    showed that merely being "eligible" for posix_spawn was not a sufficient invariant.  This
    build removes that ambiguity on macOS and never executes a fork path for
    scan workers.
    """
    argv = [str(x) for x in argv]
    if sys.platform == 'darwin' and hasattr(os, 'posix_spawn'):
        devnull = os.open(os.devnull, os.O_RDWR)
        try:
            actions = [
                (os.POSIX_SPAWN_DUP2, devnull, 0),
                (os.POSIX_SPAWN_DUP2, devnull, 1),
                (os.POSIX_SPAWN_DUP2, errh.fileno(), 2),
            ]
            pid = os.posix_spawn(argv[0], argv, dict(os.environ), file_actions=actions)
            return _PosixSpawnProcess(pid)
        finally:
            os.close(devnull)
    # Non-macOS fallback is retained for development/regression environments.
    return subprocess.Popen(
        argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=errh,
        text=True, close_fds=False)

def _cancel_control(control, reason='cancelled'):
    if not control:
        return
    control['reason'] = reason
    control['cancel'].set()
    with control['proc_lock']:
        procs = list(control['processes'])
    for proc in procs:
        _kill_process(proc)


def _cancel_all_scans(except_tid=None, reason='new_task'):
    with SCAN_CONTROL_GUARD:
        rows = [(tid, ctl) for tid, ctl in SCAN_CONTROLS.items() if tid != except_tid]
    for _tid, ctl in rows:
        _cancel_control(ctl, reason)
    # Give the old coordinator threads a brief chance to observe cancellation
    # before their task directories are pruned. The actual OCR subprocesses have
    # already been killed above, so this never waits on Apple Vision itself.
    deadline = time.monotonic() + 2.5
    for _tid, ctl in rows:
        th = ctl.get('thread')
        if th and th is not threading.current_thread():
            th.join(max(0.0, deadline - time.monotonic()))


def _register_process(control, proc):
    if not control:
        return
    with control['proc_lock']:
        control['processes'].add(proc)


def _unregister_process(control, proc):
    if not control:
        return
    with control['proc_lock']:
        control['processes'].discard(proc)


def _scan_token_matches(tid, job_id):
    try:
        m = load(tid)
        return m.get('scan', {}).get('job_id') == job_id
    except Exception:
        return False


def _launch_scan(tid, *, reason='scan'):
    job_id = uuid.uuid4().hex[:12]
    # Cancel any older generation for the same task before registering the new
    # one. A stale thread is never allowed to commit into a fresh scan.
    with SCAN_CONTROL_GUARD:
        old = SCAN_CONTROLS.get(tid)
    if old:
        _cancel_control(old, 'rescan')
    control = {
        'tid': tid, 'job_id': job_id, 'reason': reason,
        'cancel': threading.Event(), 'proc_lock': threading.RLock(),
        'processes': set(), 'thread': None, 'started_at': time.monotonic(),
    }
    with SCAN_CONTROL_GUARD:
        SCAN_CONTROLS[tid] = control
    with lock(tid):
        m = load(tid)
        m.setdefault('scan', {})['state'] = 'working'
        m['scan']['checked'] = 0
        m['scan']['total'] = len(_scan_units(m))
        m['scan']['job_id'] = job_id
        m['scan']['started_at'] = now()
        m['scan']['last_progress_at'] = now()
        m['scan']['failed'] = 0
        m['scan']['pixel_done'] = 0
        for key in ('phase', 'batch', 'batches', 'active_from', 'active_to', 'ocr', 'ocr_total', 'heartbeat_at'):
            m['scan'].pop(key, None)
        m['scan']['engine'] = vision.available()
        m['scan']['logic_version'] = vision.LOGIC_VERSION
        m['scan'].pop('error', None)
        save(m)
    th = threading.Thread(target=scan_task, args=(tid, job_id, control), daemon=True,
                          name=f'duecheck-scan-{job_id}')
    control['thread'] = th
    th.start()
    return job_id

app = FastAPI(title='DueCheck', docs_url=None, redoc_url=None)
app.mount('/static', StaticFiles(directory=BASE / 'static'), name='static')

# Each task is judged from its own Word, its own screenshots and the user's own
# actions. No historical file takes part in a production decision.

TRIAD_KEYS = ('company', 'time', 'result')


def lock(tid):
    with LOCK_GUARD:
        return LOCKS.setdefault(tid, threading.RLock())


def _user_write_begin(tid):
    # Scheduling only: user edits should not be starved by the background scan.
    # This does not alter R20 recognition or any screenshot verdict.
    with USER_WRITE_GUARD:
        USER_WRITE_PENDING[tid] = USER_WRITE_PENDING.get(tid, 0) + 1


def _user_write_end(tid):
    with USER_WRITE_GUARD:
        n = USER_WRITE_PENDING.get(tid, 0) - 1
        if n > 0:
            USER_WRITE_PENDING[tid] = n
        else:
            USER_WRITE_PENDING.pop(tid, None)


def _user_write_is_pending(tid):
    with USER_WRITE_GUARD:
        return USER_WRITE_PENDING.get(tid, 0) > 0


def _scan_yield_to_user(tid, control=None):
    while _user_write_is_pending(tid):
        if control and control['cancel'].is_set():
            raise ScanCancelled('扫描任务已取消')
        time.sleep(0.01)


def root(tid):
    if not re.fullmatch(r'[0-9a-f]{16}', tid):
        raise HTTPException(404, '任务不存在')
    p = TASKS / tid
    if not (p / 'task.json').exists():
        raise HTTPException(404, '任务不存在')
    return p


def now():
    return datetime.now().isoformat(timespec='seconds')


def load(tid):
    return json.loads((root(tid) / 'task.json').read_text('utf-8'))


def save(m, edited=False):
    if edited:
        m['version'] = m.get('version', 0) + 1
        m['generation'] = None
        m['single_generation'] = None
    m['updated_at'] = now()
    path = TASKS / m['id'] / 'task.json'
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(m, ensure_ascii=False), 'utf-8')
    tmp.replace(path)



def _scan_stamp_age_seconds(scan):
    stamps = []
    for key in ('heartbeat_at', 'last_progress_at', 'started_at'):
        value = (scan or {}).get(key)
        if not value:
            continue
        try:
            stamps.append(datetime.fromisoformat(str(value)))
        except Exception:
            pass
    if not stamps:
        return 0.0
    freshest = max(stamps)
    return max(0.0, (datetime.now() - freshest).total_seconds())


def _maybe_recover_stalled_scan(tid):
    """Self-heal a coordinator that stopped heartbeating.

    The stream worker normally guarantees progress or a per-image timeout.  This
    outer watchdog covers the final class of failures: the coordinator thread
    itself disappearing or blocking before a child process can be registered.
    The browser polls this endpoint every ~1 second, so recovery does not require
    the user to close/reopen the program.
    """
    restart = False
    cancel_only = None
    with lock(tid):
        m = load(tid)
        scan = m.get('scan', {})
        if scan.get('state') != 'working':
            return False
        with SCAN_CONTROL_GUARD:
            control = SCAN_CONTROLS.get(tid)
        thread = (control or {}).get('thread')
        same_job = bool(control and control.get('job_id') == scan.get('job_id'))
        alive = bool(same_job and thread and thread.is_alive() and not control['cancel'].is_set())
        age = _scan_stamp_age_seconds(scan)
        stale = age >= 50.0
        if alive and not stale:
            return False

        restarts = int(scan.get('watchdog_restarts', 0) or 0)
        if restarts >= 2:
            scan.update(
                state='error',
                error='本机识别进程连续无响应，已自动停止，避免任务永久卡住。请重新启动本程序后再试。',
                watchdog_at=now(),
            )
            save(m)
            cancel_only = control
        else:
            scan['watchdog_restarts'] = restarts + 1
            scan['watchdog_at'] = now()
            scan['last_progress_at'] = now()
            save(m)
            restart = True
            cancel_only = control

    if cancel_only:
        _cancel_control(cancel_only, 'watchdog_recovery')
    if restart:
        _launch_scan(tid, reason='watchdog_recovery')
        return True
    return False

def shortname(name):
    name = Path(str(name).replace('\\', '/')).name
    return re.sub(r'[\x00-\x1f<>:"/\\|?*]', '_', name)[:180] or '文件'


def safe_child(folder, name):
    if '/' in name or '\\' in name or name in ('.', '..'):
        raise HTTPException(400, '文件名无效')
    p = folder / name
    if not p.is_file():
        raise HTTPException(404, '文件不存在')
    return p


def action_key(did, uid):
    return did + ':' + uid


# --------------------------------------------------------------------------
# Verdict helpers
# --------------------------------------------------------------------------
def _triad_ok(bundle):
    if not bundle or bundle.get('state') != 'reviewed':
        return False
    return all(bundle.get('evidence', {}).get(k) == 'pass' for k in TRIAD_KEYS)


def _issues_of(bundle):
    return list((bundle or {}).get('issues') or [])


def _aggregate_element(images, key):
    """Aggregate one element across a screenshot set.

    Multiple screenshots under one site are complementary evidence, not a
    requirement that every screenshot independently contain all three elements.
    A positive observation therefore wins. Only when no screenshot proves the
    element do we surface a definite missing state or a genuine unknown.
    """
    vals = [((im.get('review') or {}).get('evidence') or {}).get(key) for im in images]
    vals = [v for v in vals if v]
    if 'pass' in vals:
        return 'pass'
    if any(v in ('missing', 'mismatch') for v in vals):
        return 'missing'
    if vals:
        return 'unreadable'
    return None


def _normalize_image_issues(issues, images):
    """Keep review only for cases that genuinely require human judgement.

    1. If any screenshot in the same site already proves an element, weaker
       evidence from companion screenshots is redundant and is discarded.
    2. A blank/no-result ROI is an actionable screenshot problem, not a human
       judgement problem.
    3. A known site's *new/uncovered layout* is not proof that the screenshot is
       wrong. It stays in manual review instead of being forced into replacement.
    """
    agg = {k: _aggregate_element(images, k) for k in TRIAD_KEYS}
    out = []
    for issue in issues:
        x = dict(issue)
        if x.get('source') != 'image':
            out.append(x); continue
        element = x.get('element')
        if element and agg.get(element) == 'pass':
            continue
        idx = x.get('image_index')
        rev = (images[idx].get('review') or {}) if isinstance(idx, int) and 0 <= idx < len(images) else {}
        reason = x.get('reason_code')
        if x.get('code') == 'result_uncertain' and reason in {'result_area_blank', 'no_result_region'}:
            loc = (rev.get('locate') or {}).get('locator_status')
            if loc in {'unmapped_variant', 'variant_not_covered', 'unregistered_column', 'failed'}:
                x['kind'] = 'review'
                x['text'] = '网站出现新页面或未覆盖版式，结果区域无法自动确定，待确认'
            else:
                x['kind'] = 'update'
                x['text'] = '查询结果区域为空白或未形成有效结果画面，需补截图'
        elif x.get('code') == 'company_uncertain':
            loc = (rev.get('locate') or {}).get('locator_status')
            q = (rev.get('regions') or {}).get('company_query')
            # New site / redesigned known site is an uncertainty, not a bad image.
            # Existing 82-site mappings never fall back to another search box; if
            # their local registration no longer covers the page, surface it for
            # review so the rule library can be extended deliberately.
            if loc in {'unmapped_variant', 'variant_not_covered', 'unregistered_column', 'failed'} or not q:
                x['kind'] = 'review'
                x['text'] = '网站出现新页面或未覆盖版式，自动定位无法确定，待确认'
        out.append(x)
    return out


def _public_bundle(bundle):
    if not bundle:
        return None
    out = deepcopy(bundle)
    out.pop('recognized_text', None)
    out.pop('diagnostics', None)
    diag = (bundle.get('diagnostics') or {})
    out['diagnostics'] = {
        'screen': diag.get('screen'),
        'identity': diag.get('identity'),
        'anchors': diag.get('anchors'),
        'locator': diag.get('locator'),
        'region_reads': diag.get('region_reads'),
        'counts': diag.get('counts'),
        'engine': diag.get('engine'),
    }
    return out


# --------------------------------------------------------------------------
# Task creation and scanning
# --------------------------------------------------------------------------
def _prune_tasks(keep=None):
    rows = []
    for folder in TASKS.iterdir():
        if not folder.is_dir():
            continue
        try:
            m = json.loads((folder / 'task.json').read_text('utf-8'))
            stamp = m.get('updated_at', '')
        except Exception:
            stamp = ''
        rows.append((stamp, folder))
    rows.sort(key=lambda x: x[0], reverse=True)
    keep_id = keep or (rows[0][1].name if rows else None)
    for _stamp, folder in rows:
        if folder.name == keep_id:
            continue
        shutil.rmtree(folder, ignore_errors=True)
        LOCKS.pop(folder.name, None)


def _prune_cache():
    CACHE.mkdir(parents=True, exist_ok=True)
    files = sorted(CACHE.glob('*.json'), key=lambda f: f.stat().st_mtime, reverse=True)
    for f in files[2048:]:
        try:
            f.unlink()
        except OSError:
            pass


def _ocr_workers():
    # Pixel analysis runs in disposable child processes, so four coordinator
    # workers are safe and keep known-site scans fast. Apple Vision itself is
    # still protected by VISION_GATE and remains strictly single-channel.
    default = min(4, max(2, os.cpu_count() or 4))
    try:
        return max(1, min(4, int(os.environ.get('DUECHECK_OCR_WORKERS', str(default)))))
    except Exception:
        return default


def _scan_total(m):
    total = sum(1 for d in m.get('docs', []) for i in d.get('items', [])
                for im in i.get('images', []) if im.get('file'))
    files = set()
    for a in m.get('actions', {}).values():
        if a.get('kind') == 'replace':
            files.update(a.get('images', []))
    return total + len(files)


def init_task(files):
    tid = uuid.uuid4().hex[:16]
    rd = TASKS / tid
    for sub in ('source', 'assets', 'uploads', 'outputs'):
        (rd / sub).mkdir(parents=True, exist_ok=True)
    docs = []
    hashes = set()
    ignored = []
    errors = []
    expanded = 0

    def ingest(name, data):
        nonlocal expanded
        name = shortname(name)
        if name.startswith(('~$', '._')):
            return
        if Path(name).suffix.lower() != '.docx':
            ignored.append(name)
            return
        if len(data) > MAX_DOC_BYTES:
            raise DocumentError('文件超过 250 MB')
        h = hashlib.sha256(data).hexdigest()
        if h in hashes:
            return
        hashes.add(h)
        did = uuid.uuid4().hex[:10]
        dest = rd / 'source' / f'{did}.docx'
        dest.write_bytes(data)
        try:
            parsed = Doc(dest)
            asset_dir = rd / 'assets' / did
            meta = parsed.public(asset_dir, required_gsxt=False)
            docs.append({'id': did, 'filename': name, 'source': str(dest), 'source_hash': h,
                         'identity_codes': [], **meta})
        except Exception as exc:
            errors.append(f'{name}：{str(exc)[:180]}')

    try:
        for filename, data in files:
            if Path(filename).suffix.lower() == '.zip':
                try:
                    with zipfile.ZipFile(io.BytesIO(data)) as z:
                        if len(z.infolist()) > 4000:
                            raise DocumentError('压缩包文件数超过 4000')
                        for inf in z.infolist():
                            pn = PurePosixPath(inf.filename.replace('\\', '/'))
                            if inf.is_dir() or '__MACOSX' in pn.parts or pn.name.startswith('._'):
                                continue
                            if pn.is_absolute() or '..' in pn.parts:
                                raise DocumentError('压缩包包含不安全路径')
                            if (inf.external_attr >> 16) & 0o170000 == 0o120000:
                                raise DocumentError('不接受压缩包内的符号链接')
                            expanded += inf.file_size
                            if expanded > MAX_ZIP_EXPANDED or inf.file_size > MAX_DOC_BYTES:
                                raise DocumentError('压缩包解压体积过大')
                            if pn.suffix.lower() == '.docx':
                                ingest(pn.name, z.read(inf))
                            else:
                                ignored.append(pn.name)
                except zipfile.BadZipFile:
                    raise DocumentError('ZIP 文件损坏')
            else:
                ingest(filename, data)
        if not docs:
            raise DocumentError('没有读到可用的诚信查询 Word。' + '；'.join(errors[:3]))
        m = {'id': tid, 'version': 1, 'created_at': now(), 'updated_at': now(), 'name': '诚信核查',
             'docs': docs, 'actions': {}, 'uploads': [],
             'settings': {'date': date.today().isoformat(), 'extra_sites': []},
             'generation': None, 'single_generation': None,
             'import_errors': errors, 'ignored_count': len(ignored),
             'scan': {'state': 'working', 'checked': 0, 'total': _scan_total({'docs': docs}),
                      'pixel_done': 0, 'engine': vision.available(), 'logic_version': vision.LOGIC_VERSION,
                      'root_causes': {}}}
        save(m)
        # The application intentionally keeps only the newest imported task.
        # Stop the previous batch *before* deleting its files and before the new
        # Apple Vision batch is launched.
        _cancel_all_scans(except_tid=tid, reason='new_import')
        _prune_tasks(keep=tid)
        _prune_cache()
        _launch_scan(tid, reason='initial_import')
        return tid
    except Exception:
        shutil.rmtree(rd, ignore_errors=True)
        raise


def _scan_units(m):
    """Every current screenshot of the task, in document order."""
    units = []
    for d in m['docs']:
        for i in d['items']:
            for j, im in enumerate(i.get('images', [])):
                if im.get('file'):
                    units.append({'kind': 'item', 'did': d['id'], 'company': d['company'],
                                  'uid': i['uid'], 'index': j, 'file': im['file'],
                                  'name': i['name'], 'url': i['url']})
    # User-supplied replacement screenshots are final human corrections.
    # They go straight into Word and are intentionally excluded from automatic
    # triad scanning. Automatic inspection only applies to original screenshots.
    return units


SCAN_WORKER = BASE / 'scan_worker.py'
PIXEL_BATCH_WORKER = BASE / 'pixel_batch_worker.py'
PIXEL_STREAM_WORKER = BASE / 'pixel_stream_worker.py'


def _isolated_observe(path, company, item, codes, *, enable_ocr=True, timeout=45, control=None):
    """Run one screenshot in a disposable process with a hard wall-clock limit.

    The process is registered with the active scan generation so importing a new
    task can kill it immediately. We intentionally poll instead of one long
    ``communicate(timeout=...)`` call: cancellation is observed within 100 ms and
    the previous task cannot keep Apple Vision busy behind the new task.
    """
    if control and control['cancel'].is_set():
        raise ScanCancelled('扫描任务已取消')
    payload = {
        'path': str(path), 'company': company, 'item': item or {},
        'cache_dir': str(CACHE), 'identity_codes': list(codes or []),
        'allow_second_read': True, 'enable_ocr': bool(enable_ocr),
    }
    with tempfile.TemporaryDirectory(prefix='duecheck-scan-') as td:
        td = Path(td)
        job_path = td / 'job.json'
        out_path = td / 'out.json'
        err_path = td / 'stderr.txt'
        job_path.write_text(json.dumps(payload, ensure_ascii=False), 'utf-8')
        # A file, rather than PIPE, also prevents a noisy native framework from
        # ever blocking because an unread stderr pipe filled up.
        with err_path.open('w', encoding='utf-8') as errh:
            proc = _spawn_runtime_worker(
                [sys.executable, str(SCAN_WORKER), str(job_path), str(out_path)], errh)
            _register_process(control, proc)
            deadline = time.monotonic() + float(timeout)
            try:
                while True:
                    if control and control['cancel'].is_set():
                        _kill_process(proc)
                        raise ScanCancelled('扫描任务已取消')
                    rc = proc.poll()
                    if rc is not None:
                        break
                    if time.monotonic() >= deadline:
                        _kill_process(proc)
                        raise TimeoutError(f'单张截图处理超过 {timeout:g} 秒')
                    time.sleep(0.10)
            finally:
                _unregister_process(control, proc)
        stderr = ''
        try:
            stderr = err_path.read_text('utf-8', errors='ignore')
        except Exception:
            pass
        if proc.returncode != 0 or not out_path.exists():
            raise RuntimeError((stderr or '单张截图处理失败')[-240:])
        return json.loads(out_path.read_text('utf-8'))

def _isolated_pixel_batch(payloads, *, timeout=24, control=None):
    """Run a small pixel/map batch in a recyclable child process.

    OpenCV local registration is fast but can accumulate native state after many
    screenshots in one long-lived process. A 12-16 image child amortises startup
    cost while remaining fully killable. No Apple Vision OCR is ever used here.
    """
    if control and control['cancel'].is_set():
        raise ScanCancelled('扫描任务已取消')
    with tempfile.TemporaryDirectory(prefix='duecheck-pixel-batch-') as td:
        td = Path(td)
        job_path = td / 'job.json'
        out_path = td / 'out.json'
        err_path = td / 'stderr.txt'
        job_path.write_text(json.dumps({'items': payloads}, ensure_ascii=False), 'utf-8')
        with err_path.open('w', encoding='utf-8') as errh:
            proc = _spawn_runtime_worker(
                [sys.executable, str(PIXEL_BATCH_WORKER), str(job_path), str(out_path)], errh)
            _register_process(control, proc)
            deadline = time.monotonic() + float(timeout)
            try:
                while True:
                    if control and control['cancel'].is_set():
                        _kill_process(proc)
                        raise ScanCancelled('扫描任务已取消')
                    rc = proc.poll()
                    if rc is not None:
                        break
                    if time.monotonic() >= deadline:
                        _kill_process(proc)
                        raise TimeoutError(f'像素批次处理超过 {timeout:g} 秒')
                    time.sleep(0.10)
            finally:
                _unregister_process(control, proc)
        stderr = ''
        try:
            stderr = err_path.read_text('utf-8', errors='ignore')
        except Exception:
            pass
        if proc.returncode != 0 or not out_path.exists():
            raise RuntimeError((stderr or '像素批次处理失败')[-240:])
        data = json.loads(out_path.read_text('utf-8'))
        return data.get('rows') or []



def _isolated_pixel_stream(payloads, *, timeout_per_item=20, control=None,
                           on_row=None, on_progress=None):
    """Process a sequence in one streaming child with a per-image watchdog.

    Unlike the old 16-image batch model, this child writes one durable JSONL row
    after every screenshot.  If native OpenCV/map code stops making progress,
    the parent kills the child, preserves every completed row, identifies the
    first unfinished screenshot, and lets the caller resume after that one.

    The return value never hides a partial run::
        {'rows': {index: row}, 'failed_index': int|None, 'error': str|None}
    """
    payloads = list(payloads or [])
    if not payloads:
        return {'rows': {}, 'failed_index': None, 'error': None, 'started': True}
    if control and control['cancel'].is_set():
        raise ScanCancelled('扫描任务已取消')

    with tempfile.TemporaryDirectory(prefix='duecheck-pixel-stream-') as td:
        td = Path(td)
        job_path = td / 'job.json'
        rows_path = td / 'rows.jsonl'
        status_path = td / 'status.json'
        err_path = td / 'stderr.txt'
        job_path.write_text(json.dumps({'items': payloads}, ensure_ascii=False), 'utf-8')

        rows = {}
        offset = 0
        buffer = b''
        last_status = None
        last_progress = time.monotonic()
        timeout_reason = None

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
                    idx = int(row.get('index'))
                except Exception:
                    continue
                if idx < 0 or idx >= len(payloads) or idx in rows:
                    continue
                rows[idx] = row
                last_progress = time.monotonic()
                if on_row:
                    on_row(idx, row)
                    # User-state commits may take measurable time on a large
                    # task.json.  That time is not a worker stall.
                    last_progress = time.monotonic()

        with err_path.open('w', encoding='utf-8') as errh:
            proc = _spawn_runtime_worker(
                [sys.executable, str(PIXEL_STREAM_WORKER), str(job_path),
                 str(rows_path), str(status_path)], errh)
            _register_process(control, proc)
            try:
                while True:
                    if control and control['cancel'].is_set():
                        _kill_process(proc)
                        raise ScanCancelled('扫描任务已取消')

                    drain_rows()

                    try:
                        if status_path.exists():
                            status = json.loads(status_path.read_text('utf-8'))
                            token = (int(status.get('index', -1)), str(status.get('state', '')))
                            if token != last_status:
                                last_status = token
                                last_progress = time.monotonic()
                                if on_progress:
                                    on_progress(token[0], token[1], len(rows))
                                    last_progress = time.monotonic()
                    except Exception:
                        pass

                    rc = proc.poll()
                    if rc is not None:
                        drain_rows()
                        break
                    if time.monotonic() - last_progress >= float(timeout_per_item):
                        timeout_reason = f'单张像素定位超过 {timeout_per_item:g} 秒'
                        _kill_process(proc)
                        drain_rows()
                        break
                    time.sleep(0.08)
            finally:
                _unregister_process(control, proc)

        stderr = ''
        try:
            stderr = err_path.read_text('utf-8', errors='ignore')
        except Exception:
            pass

        # The worker is strictly sequential.  The first absent index is the
        # screenshot that was active when it stalled/crashed; later indices were
        # never started and must be retried, not marked bad en masse.
        failed_index = next((i for i in range(len(payloads)) if i not in rows), None)
        error = None
        if failed_index is not None:
            if timeout_reason:
                error = timeout_reason
            elif proc.returncode not in (0, None):
                error = (stderr or f'像素工作进程异常退出（{proc.returncode}）')[-240:]
            else:
                error = (stderr or '像素工作进程未返回完整结果')[-240:]
        return {'rows': rows, 'failed_index': failed_index, 'error': error,
                'started': last_status is not None}

def _should_escalate_ocr(bundle):
    """Escalate to Apple Vision only when pixel/mapping rules leave a true review item."""
    if not isinstance(bundle, dict) or bundle.get('state') == 'error':
        return True
    return any((issue or {}).get('kind') == 'review' for issue in (bundle.get('issues') or []))


def _acquire_vision_gate(control, step=0.20):
    while True:
        if control and control['cancel'].is_set():
            raise ScanCancelled('扫描任务已取消')
        if VISION_GATE.acquire(timeout=step):
            return


def scan_task(tid, job_id=None, control=None):
    """Read every current screenshot with cancellation and hard process limits.

    Pixel mapping runs in one streaming disposable child that reports after each
    screenshot; a no-progress watchdog can kill and resume it after one bad image.
    Apple Vision escalation still runs one screenshot per disposable child. The
    coordinator therefore remains cancellable without owning a large native queue.
    """
    control = control or SCAN_CONTROLS.get(tid)
    job_id = job_id or ((control or {}).get('job_id'))
    if not control or not job_id:
        return
    causes = {}
    try:
        if control['cancel'].is_set() or not _scan_token_matches(tid, job_id):
            return
        with lock(tid):
            m = load(tid)
            if m.get('scan', {}).get('job_id') != job_id:
                return
            units = _scan_units(m)
            codes_by_doc = {d['id']: list(d.get('identity_codes') or []) for d in m['docs']}
            # Do not let old verdicts masquerade as the current run while the new
            # generation is in flight.
            for d in m.get('docs', []):
                for i in d.get('items', []):
                    for im in i.get('images', []):
                        if im.get('file'):
                            im['scan_state'] = 'working'
                            rv = im.get('review') or {}
                            if rv.get('schema') != vision.LOGIC_VERSION:
                                im.pop('review', None)
            for u in m.get('uploads', []):
                if u.get('file'):
                    u['scan_state'] = 'working'
                    rv = u.get('review') or {}
                    if rv.get('schema') != vision.LOGIC_VERSION:
                        u['review'] = None
                        u['logic_version'] = None
            m['scan'].update(state='working', checked=0, total=len(units), pixel_done=0,
                             logic_version=vision.LOGIC_VERSION,
                             last_progress_at=now(), failed=0)
            save(m, True)

        def unit_path(unit):
            if unit['kind'] == 'item':
                return TASKS / tid / 'assets' / unit['did'] / unit['file']
            return TASKS / tid / 'uploads' / unit['file']

        def failure_bundle(code, text, note=''):
            return {'schema': vision.LOGIC_VERSION, 'state': 'error', 'engine': code,
                    'issues': [{'code': code, 'text': text, 'kind': 'update', 'source': 'image'}],
                    'evidence': {k: 'engine_error' for k in TRIAD_KEYS},
                    'verdicts': {}, 'diagnostics': {}, 'recognized_text': '',
                    'notes': [note[:160]] if note else []}

        def commit(unit, bundle):
            if control['cancel'].is_set():
                return False
            cause = vision.root_cause(bundle)
            if cause:
                cause_s = str(cause)
                causes[cause_s] = causes.get(cause_s, 0) + 1
            _scan_yield_to_user(tid, control)
            with lock(tid):
                m = load(tid)
                if m.get('scan', {}).get('job_id') != job_id or control['cancel'].is_set():
                    return False
                target = None
                if unit['kind'] == 'item':
                    d = next((x for x in m['docs'] if x['id'] == unit['did']), None)
                    if d:
                        target = next((x for x in d['items'] if x['uid'] == unit['uid']), None)
                        if target is not None and unit['index'] is not None and unit['index'] < len(target.get('images', [])):
                            target['images'][unit['index']]['review'] = bundle
                            target['images'][unit['index']]['scan_state'] = 'done'
                        if target is not None and bundle.get('identity_candidates'):
                            d['identity_codes'] = list(dict.fromkeys(
                                (d.get('identity_codes') or []) + bundle['identity_candidates']))
                else:
                    u = next((x for x in m.get('uploads', []) if x.get('file') == unit['file']), None)
                    if u:
                        u['review'] = bundle
                        u['diagnostics'] = bundle.get('diagnostics')
                        u['scan_state'] = 'done'
                        u['logic_version'] = vision.LOGIC_VERSION
                        if not u.get('review_for'):
                            u['suggestion'] = vision.match_screenshot(
                                u['filename'], bundle.get('recognized_text', ''), m['docs'])['assigned']
                sc = m.setdefault('scan', {})
                sc['checked'] = min(sc.get('total', 1), sc.get('checked', 0) + 1)
                sc['last_progress_at'] = now()
                sc['root_causes'] = causes
                if bundle.get('state') == 'error':
                    sc['failed'] = sc.get('failed', 0) + 1
                save(m, True)
            return True

        if units:
            # Stage A — one clean streaming *supervisor* from the web process.
            #
            # r20 launched a native batch worker from the multithreaded server
            # every 16 screenshots. The repeated 64/191 stop occurs exactly at
            # the transition into the third 32-item document. r21 removes both
            # the repeated web-process launch boundary and batch-wide coupling:
            # it starts one supervisor instead;
            # that clean single-threaded process recycles small native workers and
            # streams one result per screenshot back here. If a batch wedges it
            # retries that small group one-by-one, so one bad image cannot stall
            # or invalidate the rest.
            ambiguous = []
            engine_enabled = bool(vision.available().get('enabled'))

            def pixel_payload(seq):
                return [{
                    'path': str(unit_path(unit)),
                    'company': unit['company'],
                    'item': {'name': unit['name'], 'url': unit['url']},
                    'cache_dir': str(CACHE),
                    'identity_codes': codes_by_doc.get(unit['did'], []),
                } for unit in seq]

            def handle_pixel_row(global_index, row, allow_ocr=True):
                unit = units[global_index]
                if row.get('ok') and isinstance(row.get('bundle'), dict):
                    pixel_bundle = row['bundle']
                else:
                    pixel_bundle = failure_bundle(
                        'pixel_analysis_error',
                        '本张截图自动定位异常，已保留待确认并继续后续检查',
                        row.get('error', '像素分析失败'))
                    for issue in pixel_bundle.get('issues', []):
                        issue['kind'] = 'review'
                if allow_ocr and _should_escalate_ocr(pixel_bundle) and engine_enabled:
                    ambiguous.append((unit, pixel_bundle))
                else:
                    pixel_bundle.setdefault('notes', []).append(
                        '栏目映射与像素规则已完成判断，未调用文字识别')
                    commit(unit, pixel_bundle)

            cursor = 0
            zero_progress_restarts = 0
            while cursor < len(units):
                if control['cancel'].is_set() or not _scan_token_matches(tid, job_id):
                    break
                base = cursor
                last_ui = [0.0]

                def progress(local_index, state, completed, base_index=base):
                    if local_index < 0:
                        return
                    t = time.monotonic()
                    # UI only needs human-scale feedback.  Throttling avoids
                    # rewriting a growing task.json for every fast screenshot.
                    if state not in ('complete',) and t - last_ui[0] < 0.45:
                        return
                    last_ui[0] = t
                    _scan_yield_to_user(tid, control)
                    with lock(tid):
                        live = load(tid)
                        if live.get('scan', {}).get('job_id') != job_id:
                            return
                        active = min(len(units), base_index + local_index + 1)
                        live['scan'].update(
                            phase='pixel', active_from=active, active_to=active,
                            pixel_done=min(len(units), base_index + completed),
                            heartbeat_at=now())
                        save(live)

                def got_row(local_index, row, base_index=base):
                    handle_pixel_row(base_index + local_index, row)

                try:
                    stream = _isolated_pixel_stream(
                        pixel_payload(units[cursor:]), timeout_per_item=20,
                        control=control, on_row=got_row, on_progress=progress)
                except ScanCancelled:
                    raise

                failed_local = stream.get('failed_index')
                if failed_local is None:
                    cursor = len(units)
                    break

                # Rows before failed_local were already committed/queued by
                # got_row.  Degrade exactly the first unfinished image, then
                # restart the stream *after* it.  No 16-image group is sacrificed.
                failed_global = base + int(failed_local)
                err = stream.get('error') or '像素工作进程异常'
                handle_pixel_row(failed_global, {
                    'ok': False,
                    'error': '单张像素定位已隔离：' + str(err)[:180],
                })
                made_progress = bool(stream.get('rows')) or failed_local > 0 or bool(stream.get('started'))
                zero_progress_restarts = 0 if made_progress else zero_progress_restarts + 1
                cursor = failed_global + 1

                # A machine whose native worker cannot even start should still
                # never sit forever.  After two consecutive zero-progress starts,
                # finish Stage A conservatively instead of spending 14 seconds on
                # every remaining screenshot.  They remain review items; no false
                # automatic pass is created.
                if zero_progress_restarts >= 2 and cursor < len(units):
                    # Stability fallback only: do not turn an infrastructure failure
                    # into a business-level '待确认'. Queue every remaining screenshot
                    # through the unchanged R20 Vision judgement path instead.
                    for gi in range(cursor, len(units)):
                        handle_pixel_row(gi, {
                            'ok': False,
                            'error': '本机像素工作进程连续无法启动；已切换原始文字识别继续完成任务',
                        }, allow_ocr=True)
                    cursor = len(units)
                    break

            # Stage B — only genuinely ambiguous screenshots reach Apple Vision.
            # The gate is intentionally single-channel; every OCR call is itself
            # a killable process with a 20-second wall-clock limit.
            for ocr_index, (unit, pixel_bundle) in enumerate(ambiguous, 1):
                if control['cancel'].is_set() or not _scan_token_matches(tid, job_id):
                    break
                _scan_yield_to_user(tid, control)
                with lock(tid):
                    live = load(tid)
                    if live.get('scan', {}).get('job_id') == job_id:
                        live['scan'].update(
                            phase='ocr', ocr=ocr_index, ocr_total=len(ambiguous),
                            heartbeat_at=now())
                        save(live)
                gate = False
                try:
                    _acquire_vision_gate(control)
                    gate = True
                    bundle = _isolated_observe(
                        unit_path(unit), unit['company'],
                        {'name': unit['name'], 'url': unit['url']},
                        codes_by_doc.get(unit['did'], []),
                        enable_ocr=True, timeout=20, control=control)
                except ScanCancelled:
                    raise
                except TimeoutError:
                    bundle = pixel_bundle
                    bundle.setdefault('notes', []).append('文字识别超时；保留像素判断并继续')
                except Exception as exc:
                    bundle = pixel_bundle
                    bundle.setdefault('notes', []).append('文字识别异常；保留像素判断：' + str(exc)[:100])
                finally:
                    if gate:
                        VISION_GATE.release()
                commit(unit, bundle)

        if not control['cancel'].is_set():
            with lock(tid):
                m = load(tid)
                if m.get('scan', {}).get('job_id') == job_id:
                    m['scan']['state'] = 'done'
                    m['scan']['checked'] = m['scan'].get('total', m['scan'].get('checked', 0))
                    m['scan']['logic_version'] = vision.LOGIC_VERSION
                    m['scan']['root_causes'] = causes
                    m['scan']['completed_at'] = now()
                    for key in ('phase', 'batch', 'batches', 'active_from', 'active_to',
                                'ocr', 'ocr_total', 'heartbeat_at', 'pixel_done'):
                        m['scan'].pop(key, None)
                    save(m)
    except ScanCancelled:
        pass
    except Exception as exc:
        try:
            with lock(tid):
                m = load(tid)
                if m.get('scan', {}).get('job_id') == job_id:
                    m['scan'].update(state='error', error=str(exc)[:180], last_progress_at=now())
                    save(m)
        except Exception:
            pass
    finally:
        with SCAN_CONTROL_GUARD:
            if SCAN_CONTROLS.get(tid) is control:
                SCAN_CONTROLS.pop(tid, None)


# --------------------------------------------------------------------------
# Effective state of an item
# --------------------------------------------------------------------------
def _effective_images(m, item, action):
    if action and action.get('kind') == 'replace':
        by_file = {u.get('file'): u for u in m.get('uploads', [])}
        return [{'file': fn, 'hash': (by_file.get(fn) or {}).get('hash'),
                 'review': deepcopy((by_file.get(fn) or {}).get('review')),
                 'scan_state': (by_file.get(fn) or {}).get('scan_state')}
                for fn in action.get('images', []) if fn]
    return deepcopy(item.get('images', []))


def _apply_manual(images, action):
    """Bind a user decision to the exact ordered bytes of these images.

    ``keep`` means explicit human sign-off. ``replace`` means the user supplied a
    final corrected screenshot; by product rule it is never re-judged by OCR.
    """
    manual_final = bool(action and action.get('kind') == 'replace' and action.get('manual_final'))
    if not action or (not manual_final and (not action.get('review_confirmed') or action.get('confirmed_by') != 'user')):
        return images
    hashes = [im.get('hash') or (im.get('review') or {}).get('hash') for im in images]
    if not hashes or not all(hashes) or hashes != action.get('image_hashes'):
        return images
    out = deepcopy(images)
    for im in out:
        if im.get('error'):
            continue
        engine_label = '人工补图' if manual_final else '人工核对'
        basis_label = '用户补图，作为最终人工修正直接写入 Word' if manual_final else '已逐张人工核对'
        reason_code = 'manual_replacement' if manual_final else 'manual'
        im['review'] = {'state': 'reviewed', 'engine': engine_label,
                        'logic_version': vision.LOGIC_VERSION,
                        'evidence': {k: 'pass' for k in TRIAD_KEYS},
                        'verdicts': {k: {'state': 'pass', 'basis': basis_label,
                                         'reason_code': reason_code, 'boxes': [], 'observed_text': ''}
                                     for k in TRIAD_KEYS},
                        'issues': [], 'hash': im.get('hash'),
                        'confirmed_at': action.get('confirmed_at'),
                        'diagnostics': (im.get('review') or {}).get('diagnostics')}
    return out


def issues_for(item, images):
    """Issues of the *current* document and the *current* screenshots only."""
    out = []
    seen = set()
    for issue in item.get('issues', []):
        code = issue.get('code')
        if code in seen:
            continue
        seen.add(code)
        out.append(issue)
    multi = len(images) > 1
    for j, im in enumerate(images):
        rev = im.get('review') or {}
        if im.get('error'):
            key = ('image_broken', j)
            if key not in seen:
                seen.add(key)
                out.append({'code': 'image_broken', 'text': '截图无法读取', 'kind': 'update',
                            'source': 'image', 'image_index': j})
            continue
        for issue in rev.get('issues', []):
            code = issue.get('code')
            key = (code, j)
            if not code or key in seen:
                continue
            seen.add(key)
            x = dict(issue)
            x['image_index'] = j
            if multi:
                x['text'] = f'第{j + 1}张：' + x.get('text', '')
            out.append(x)
    return out


def _resolved_by_action(issues, action):
    if not action:
        return issues
    if action.get('kind') == 'exclude':
        return []
    out = []
    conclusion = action.get('conclusion')
    explicit = conclusion is not None and not pending_conclusion(conclusion)
    for issue in issues:
        code = issue.get('code')
        if action.get('date') and code in {'date_empty', 'date_invalid'}:
            continue
        if action.get('success') in SUCCESS and code == 'query_failed':
            continue
        if explicit and code in {'conclusion_empty', 'conclusion_placeholder',
                                 'conclusion_conflict', 'comment_review'}:
            continue
        if code == 'comment_anomaly' and explicit and action.get('acknowledge_anomaly'):
            continue
        out.append(issue)
    codes = {i.get('code') for i in out}
    if conclusion is not None and pending_conclusion(conclusion) and 'conclusion_empty' not in codes:
        out.append({'code': 'conclusion_empty', 'text': '核查结论仍为空',
                    'kind': 'metadata', 'source': 'document'})
    if 'success' in action and action.get('success') not in SUCCESS and 'query_failed' not in codes:
        out.append({'code': 'query_failed', 'text': '查询仍标记为未成功',
                    'kind': 'update', 'source': 'document'})
    return out


def effective(m, item, action):
    images = _effective_images(m, item, action)
    images = _apply_manual(images, action)
    drop = {'image_missing', 'image_broken', 'required_site'} if images else set()
    base = [i for i in issues_for(item, images) if i.get('code') not in drop]
    base = _normalize_image_issues(base, images)
    return _resolved_by_action(base, action), images


SUMMARY = {
    'company_missing': '补图（无公司名）',
    'company_uncertain': '看图确认主体',
    'clock_not_visible': '补图（无系统时间）',
    'time_uncertain': '看图确认时间',
    'page_incomplete': '更新截图',
    'result_uncertain': '看图确认结果',
    'site_mismatch': '核对网站',
}


def _summary(state, all_issues):
    hard = [x for x in all_issues if x.get('kind') != 'review']
    codes = {x.get('code') for x in all_issues}
    if 'image_missing' in codes or 'required_site' in codes:
        return '缺截图'
    if 'image_broken' in codes or 'corrupt' in codes:
        return '截图损坏'
    if 'site_mismatch' in codes:
        return '核对网站'
    for code in ('company_missing', 'clock_not_visible', 'page_incomplete', 'result_uncertain'):
        if code in codes:
            return SUMMARY[code]
    if 'query_failed' in codes:
        return '重新核查'
    if 'comment_anomaly' in codes:
        return '核实异常'
    if hard and all(x.get('kind') == 'metadata' for x in hard):
        return '补表格'
    if hard:
        return '需处理'
    if any(x.get('code') in SUMMARY for x in all_issues):
        for x in all_issues:
            if x.get('code') in SUMMARY:
                return SUMMARY[x['code']]
    return ''


def decorate(m):
    res = deepcopy(m)
    tid = m['id']
    counts = {'companies': len(m['docs']), 'needs': 0, 'unread': 0, 'done': 0,
              'replaced': 0, 'metadata': 0, 'scanning': 0}
    scanning = m.get('scan', {}).get('state') == 'working'
    upload_by_file = {u.get('file'): u for u in m.get('uploads', [])}
    for d in res['docs']:
        d.pop('source', None)
        dc = {'needs': 0, 'unread': 0, 'done': 0, 'scanning': 0}
        for i in d['items']:
            key = action_key(d['id'], i['uid'])
            act = m.get('actions', {}).get(key)
            for im in i.get('images', []):
                if im.get('file'):
                    im['url'] = f'/api/tasks/{tid}/asset/{d["id"]}/{im["file"]}'
                if im.get('review'):
                    im['review'] = _public_bundle(im['review'])
            if act:
                i['action'] = deepcopy(act)
                i['action']['images'] = [
                    {'file': fn, 'url': f'/api/tasks/{tid}/upload/{fn}',
                     'review': _public_bundle((upload_by_file.get(fn) or {}).get('review'))}
                    for fn in act.get('images', [])]
                i['action'].pop('image_hashes', None)
                if act.get('kind') == 'replace':
                    counts['replaced'] += 1
            all_issues, images = effective(m, i, act)
            hard = [x for x in all_issues if x.get('kind') != 'review']
            review = [x for x in all_issues if x.get('kind') == 'review']
            i['all_issues'] = all_issues
            i['hard_issues'] = hard
            i['review_issues'] = review
            if act and act.get('kind') == 'exclude':
                state = 'done'
            elif hard:
                state = 'needs'
            else:
                reviewed = bool(images) and all((im.get('review') or {}).get('state') == 'reviewed'
                                                for im in images)
                waiting = any(im.get('scan_state') == 'working' or (scanning and not im.get('review'))
                              for im in images)
                state = 'scanning' if waiting else ('done' if reviewed and not review else 'unread')
            i['state'] = state
            counts[state] += 1
            dc[state] += 1
            i['triad'] = {}
            for k in TRIAD_KEYS:
                v = _aggregate_element(images, k)
                i['triad'][k] = ('yes' if v == 'pass' else 'no' if v == 'missing' else 'unknown')
            i['review_method'] = ('manual' if any((im.get('review') or {}).get('engine') in {'人工核对', '人工补图'}
                                                  for im in images) else 'automatic')
            target = i.get('action', {}).get('images', []) if act and act.get('kind') == 'replace' else i.get('images', [])
            for shown, eff in zip(target, images):
                if eff.get('review'):
                    shown['review'] = _public_bundle(eff['review'])
                shown['scan_state'] = eff.get('scan_state')
            if state == 'needs' and hard and all(x.get('kind') == 'metadata' for x in hard):
                counts['metadata'] += 1
            i['summary'] = ('检查中' if state == 'scanning'
                            else _summary(state, all_issues))
            if state == 'done' and act:
                i['summary'] = {'replace': '已更新', 'keep': '已保留', 'metadata': '已更新',
                                'exclude': '本次不适用'}.get(act.get('kind'), '已处理')
        d['counts'] = dc
    res['counts'] = counts
    for u in res.get('uploads', []):
        u['url'] = f'/api/tasks/{tid}/upload/{u["file"]}'
        u['review'] = _public_bundle(u.get('review'))
    if res.get('generation'):
        for d in res['generation']['docs']:
            d.pop('path', None)
            d['url'] = f'/api/tasks/{tid}/download/{d["id"]}?v={res["version"]}'
        res['generation']['url'] = f'/api/tasks/{tid}/download-all?v={res["version"]}'
        res['generation'].pop('zip_path', None)
    return res


def find_item(m, did, uid):
    d = next((x for x in m['docs'] if x['id'] == did), None)
    if d is None:
        raise HTTPException(404, '公司不存在')
    i = next((x for x in d['items'] if x['uid'] == uid), None)
    if i is None:
        raise HTTPException(404, '核查项目不存在')
    return d, i


def unresolved(m):
    d = decorate(m)
    return [(co['id'], i['uid'], co['company'], i['no'], i['name'], i['state'])
            for co in d['docs'] for i in co['items'] if i['state'] != 'done']


def _sync_extra_sites(m):
    sites = m.setdefault('settings', {}).setdefault('extra_sites', [])
    old_new = {action_key(d.get('id', ''), i.get('uid', ''))
               for d in m.get('docs', []) for i in d.get('items', []) if i.get('new')}
    valid_new = set()
    for d in m.get('docs', []):
        originals = [i for i in d.get('items', []) if not i.get('new')]
        d['items'] = originals
        existing = {site_key(i.get('name', ''), i.get('url', '')) for i in originals}
        no = max((int(i.get('no') or 0) for i in originals), default=0) + 1
        for site in sites:
            name = norm(site.get('name', ''))
            url = norm(site.get('url', ''))
            key = site_key(name, url)
            if not name or not url or key in existing:
                continue
            uid = hashlib.sha256((key + '#1').encode()).hexdigest()[:16]
            from engine import Item
            it = Item(uid, no, name, url, 1, [], new=True, mapping_error='').public()
            it['issues'] = [{'code': 'required_site', 'text': f'补{name}截图',
                             'kind': 'update', 'source': 'policy'}]
            d['items'].append(it)
            existing.add(key)
            valid_new.add(action_key(d.get('id', ''), uid))
            no += 1
    for key in list(m.get('actions', {})):
        if key in old_new and key not in valid_new:
            m['actions'].pop(key, None)
    used = {f for a in m.get('actions', {}).values() for f in a.get('images', [])}
    for u in m.get('uploads', []):
        u['used'] = u.get('file') in used


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
@app.middleware('http')
async def local_only(request: Request, call_next):
    host = request.headers.get('host', '').split(':')[0]
    if host not in {'127.0.0.1', 'localhost', 'testserver', '[::1]'}:
        return JSONResponse({'detail': '仅允许本机访问'}, 400)
    origin = request.headers.get('origin')
    if request.method not in ('GET', 'HEAD', 'OPTIONS') and origin and \
            origin not in ('http://' + request.headers.get('host', ''), 'https://' + request.headers.get('host', '')):
        return JSONResponse({'detail': '拒绝跨站请求'}, 403)
    resp = await call_next(request)
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    resp.headers['Cache-Control'] = 'no-store'
    resp.headers['Referrer-Policy'] = 'no-referrer'
    return resp


@app.exception_handler(DocumentError)
async def document_error(_, exc):
    return JSONResponse({'detail': str(exc)}, 400)


@app.get('/')
def home():
    index = BASE / 'static' / 'index.html'
    if index.is_file():
        return FileResponse(index)
    return HTMLResponse('<!doctype html><meta charset="utf-8"><title>DueCheck</title>'
                        '<body style="font-family:-apple-system,BlinkMacSystemFont,sans-serif;padding:40px">'
                        'DueCheck 页面文件缺失，请关闭当前终端窗口后重新解压并启动。</body>', status_code=503)


@app.get('/health')
def health():
    return {'ok': True, 'app': 'DueCheck', 'build': BUILD, 'recognition': vision.available(),
            'logic_version': vision.LOGIC_VERSION, 'columns': len((page_map.registry().get('columns') or {}))}


@app.get('/api/tasks')
def list_tasks():
    _prune_tasks()
    out = []
    for f in TASKS.glob('*/task.json'):
        try:
            m = json.loads(f.read_text('utf-8'))
            out.append({'id': m['id'], 'date': m['updated_at'],
                        'companies': [d['company'] for d in m['docs']]})
        except Exception:
            continue
    return sorted(out, key=lambda x: x['date'], reverse=True)[:1]


@app.post('/api/tasks')
async def create_task(files: list[UploadFile] = File(...)):
    if len(files) > 30:
        raise HTTPException(400, '一次最多上传 30 个文件')
    data = []
    total = 0
    for f in files:
        blob = await f.read(MAX_DOC_BYTES + 1)
        total += len(blob)
        if len(blob) > MAX_DOC_BYTES or total > 650 * 1024 * 1024:
            raise HTTPException(413, '上传体积过大，请分批导入')
        data.append((f.filename or '文件.docx', blob))
    tid = await run_in_threadpool(init_task, data)
    return {'id': tid}


@app.get('/api/tasks/{tid}')
def get_task(tid: str):
    _maybe_recover_stalled_scan(tid)
    with lock(tid):
        return decorate(load(tid))


@app.get('/api/tasks/{tid}/asset/{did}/{filename}')
def asset(tid: str, did: str, filename: str):
    if not re.fullmatch(r'[a-f0-9]{10}', did):
        raise HTTPException(404)
    return FileResponse(safe_child(root(tid) / 'assets' / did, filename))


@app.get('/api/tasks/{tid}/upload/{filename}')
def upload_image(tid: str, filename: str):
    return FileResponse(safe_child(root(tid) / 'uploads', filename))


@app.get('/api/tasks/{tid}/diagnostics/{did}/{uid}')
def item_diagnostics(tid: str, did: str, uid: str, index: int = 0, source: str = 'original'):
    """Full developer diagnostics for one screenshot of one item."""
    with lock(tid):
        m = load(tid)
        d, it = find_item(m, did, uid)
        if source == 'replacement':
            act = m.get('actions', {}).get(action_key(did, uid)) or {}
            fn = (act.get('images') or [None])[index]
            if not fn:
                raise HTTPException(404, '没有对应的补图')
            u = next((x for x in m.get('uploads', []) if x.get('file') == fn), None)
            bundle = (u or {}).get('review') or {}
        else:
            if index >= len(it.get('images', [])):
                raise HTTPException(404, '图片序号超出范围')
            bundle = it['images'][index].get('review') or {}
        return {'company': d['company'], 'no': it['no'], 'name': it['name'],
                'url': it['url'], 'index': index, 'column': page_map.column_for(it['name'], it['url']),
                'logic_version': vision.LOGIC_VERSION, 'bundle': bundle}


@app.get('/api/tasks/{tid}/columns')
def column_coverage(tid: str):
    """Per-column coverage: located / read / judged, with real sample counts."""
    with lock(tid):
        m = load(tid)
    reg = page_map.registry()
    columns = deepcopy(reg.get('columns') or {})
    seen = {}
    for d in m['docs']:
        for it in d['items']:
            key = page_map.column_key(it['name'], it['url'])
            row = seen.setdefault(key, {'screens': 0, 'located': 0, 'valued': 0, 'judged': 0,
                                        'pass': 0, 'blocked': 0, 'name': it['name']})
            for im in it.get('images', []):
                rev = im.get('review') or {}
                if not rev:
                    continue
                row['screens'] += 1
                loc = ((rev.get('diagnostics') or {}).get('locator') or {})
                if (loc.get('company_query') or {}).get('rect') and (loc.get('result_container') or {}).get('rect'):
                    row['located'] += 1
                ev = rev.get('evidence') or {}
                if ev.get('company') in ('pass', 'mismatch') and ev.get('time') == 'pass':
                    row['valued'] += 1
                if ev.get('company') == 'pass' and ev.get('time') == 'pass' and ev.get('result') == 'pass':
                    row['judged'] += 1
                    row['pass'] += 1
                else:
                    row['blocked'] += 1
    for key, row in seen.items():
        entry = columns.setdefault(key, {'key': key, 'name': row['name'],
                                         'family': page_map.classify(row['name'], ''), 'host': ''})
        entry['verification'] = {'samples': row['screens'], 'located': row['located'],
                                 'valued': row['valued'], 'judged': row['judged'],
                                 'positives': row['pass'], 'negatives': row['blocked'],
                                 'status': ('verified' if row['screens'] and row['judged'] == row['screens']
                                            else 'partial' if row['located'] else 'unverified')}
        entry['column_key'] = key
    return {'logic_version': vision.LOGIC_VERSION, 'engine': vision.available(),
            'columns': columns, 'summary': {
                'columns_in_corpus': len(seen),
                'screens': sum(r['screens'] for r in seen.values()),
                'located': sum(r['located'] for r in seen.values()),
                'judged': sum(r['judged'] for r in seen.values()),
                'needs_attention': sum(r['blocked'] for r in seen.values())}}


class ExtraSite(BaseModel):
    name: str
    url: str


class Settings(BaseModel):
    date: str
    extra_sites: list[ExtraSite] = Field(default_factory=list)


@app.put('/api/tasks/{tid}/settings')
def settings(tid: str, value: Settings):
    try:
        date.fromisoformat(value.date)
    except ValueError:
        raise HTTPException(400, '日期应为 YYYY-MM-DD')
    sites = []
    seen = set()
    for site in value.extra_sites[:8]:
        name = norm(site.name)
        url = norm(site.url)
        if not name or not re.match(r'^https?://', url, re.I):
            raise HTTPException(400, '新增网站需要填写名称和 http(s) 网址')
        key = site_key(name, url)
        if key in seen:
            continue
        seen.add(key)
        sites.append({'name': name, 'url': url})
    with lock(tid):
        m = load(tid)
        m['settings'] = {'date': value.date, 'extra_sites': sites}
        _sync_extra_sites(m)
        save(m, True)
    return {'ok': True}


def inspect_uploads(tid, files):
    with lock(tid):
        m = load(tid)
        rows = []
        for filename in files:
            u = next((x for x in m.get('uploads', []) if x['file'] == filename), None)
            if not u:
                continue
            binding = u.get('review_for')
            if binding:
                did, uid = binding.split(':', 1)
                try:
                    d, i = find_item(m, did, uid)
                    rows.append((filename, binding, d['company'],
                                 {'name': i['name'], 'url': i['url']},
                                 list(d.get('identity_codes') or [])))
                except HTTPException:
                    rows.append((filename, binding, '', {}, []))
            else:
                rows.append((filename, '', '', {}, []))

    def job(row):
        filename, binding, company, item, codes = row
        path = TASKS / tid / 'uploads' / filename
        try:
            res = _isolated_observe(path, company, item, codes, enable_ocr=True, timeout=45)
        except TimeoutError:
            res = _isolated_observe(path, company, item, codes, enable_ocr=False, timeout=15)
            res.setdefault('notes', []).append('文字识别超时，已自动采用像素规则完成判断')
        return row, res

    try:
        with ThreadPoolExecutor(max_workers=min(_ocr_workers(), max(1, len(rows))),
                                thread_name_prefix='duecheck-upload') as pool:
            futures = [pool.submit(job, r) for r in rows]
            for fut in as_completed(futures):
                row, res = fut.result()
                filename, binding, company, item, codes = row
                with lock(tid):
                    m = load(tid)
                    u = next((x for x in m.get('uploads', []) if x.get('file') == filename), None)
                    if not u:
                        continue
                    actual = u.get('review_for')
                    if actual and actual != binding:
                        u['review'] = None
                        u['scan_state'] = 'working'
                        save(m, True)
                        threading.Thread(target=inspect_uploads, args=(tid, [filename]), daemon=True).start()
                        continue
                    u['review'] = res
                    u['diagnostics'] = res.get('diagnostics')
                    u['scan_state'] = 'done'
                    u['logic_version'] = vision.LOGIC_VERSION
                    if not actual:
                        u['suggestion'] = vision.match_screenshot(
                            u['filename'], res.get('recognized_text', ''), m['docs'])['assigned']
                    save(m, True)
    except Exception as outer:
        for filename in files:
            try:
                with lock(tid):
                    m = load(tid)
                    u = next((x for x in m.get('uploads', []) if x.get('file') == filename), None)
                    if u and u.get('scan_state') == 'done':
                        continue
                    if u:
                        u['scan_state'] = 'error'
                        u['review'] = {'state': 'error', 'recognition_error': str(outer)[:120],
                                       'evidence': {k: 'engine_error' for k in TRIAD_KEYS}}
                    save(m, True)
            except Exception:
                pass


@app.post('/api/tasks/{tid}/images')
async def image_upload(tid: str, files: list[UploadFile] = File(...),
                       doc_id: Optional[str] = Form(None), uid: Optional[str] = Form(None)):
    rd = root(tid)
    out = []
    jobs = []
    if bool(doc_id) != bool(uid):
        raise HTTPException(400, '公司与网站必须同时指定')
    if len(files) > 120:
        raise HTTPException(400, '一次最多上传 120 张截图')
    from PIL import Image
    binding = action_key(doc_id, uid) if doc_id and uid else ''

    # Direct item replacement is a user edit. Give it scheduling priority so a
    # long background scan cannot repeatedly reacquire the same task lock first.
    if binding:
        _user_write_begin(tid)
    try:
        for f in files:
            ext = Path(f.filename or '').suffix.lower()
            if ext not in IMAGE_EXTS:
                raise HTTPException(400, '只接受图片文件')
            data = await f.read(35 * 1024 * 1024 + 1)
            if len(data) > 35 * 1024 * 1024:
                raise HTTPException(413, '单张图片超过 35 MB')
            name = hashlib.sha256(data + binding.encode()).hexdigest()[:32] + ext
            path = rd / 'uploads' / name
            try:
                with Image.open(io.BytesIO(data)) as probe:
                    probe.verify()
            except Exception:
                raise HTTPException(400, '上传的文件无法作为图片读取')
            path.write_bytes(data)
            with lock(tid):
                m = load(tid)
                suggestion = None
                if binding:
                    d, i = find_item(m, doc_id, uid)
                    suggestion = {'doc_id': doc_id, 'uid': uid, 'company': d['company'],
                                  'name': i['name'], 'no': i['no'], 'reason': '在该项目上传'}
                u = next((x for x in m['uploads'] if x['file'] == name), None)
                if not u:
                    manual_final = bool(binding)
                    u = {'file': name, 'hash': hashlib.sha256(data).hexdigest(),
                         'filename': shortname(f.filename), 'review': None,
                         'review_for': binding or None, 'suggestion': suggestion,
                         'scan_state': ('manual_final' if manual_final else 'working'),
                         'manual_final': manual_final, 'used': False,
                         'logic_version': vision.LOGIC_VERSION if manual_final else None}
                    m['uploads'].append(u)
                    if not manual_final:
                        jobs.append(name)
                elif binding:
                    # Direct replacement is a human correction: never re-run triad OCR.
                    u['review_for'] = binding
                    u['manual_final'] = True
                    u['scan_state'] = 'manual_final'
                    u['review'] = None
                    u['logic_version'] = vision.LOGIC_VERSION
                elif u.get('logic_version') != vision.LOGIC_VERSION or not u.get('review'):
                    u['scan_state'] = 'working'
                    jobs.append(name)
                out.append(deepcopy(u))
                save(m, True)

        # A bound upload is only staged. It must never change the item action here.
        # The user confirms the screenshot, date, status and conclusion together in
        # the item dialog; only that explicit save is allowed to commit the edit.
    finally:
        if binding:
            _user_write_end(tid)

    if jobs:
        threading.Thread(target=inspect_uploads, args=(tid, jobs), daemon=True).start()
    return {'images': [_public_bundle(u) or {k: v for k, v in u.items() if k != 'review'} for u in out],
            'applied': False}


@app.post('/api/tasks/{tid}/items/{did}/{uid}/replace')
async def replace_item_with_files(
        tid: str, did: str, uid: str,
        files: list[UploadFile] = File(...),
        date_value: Optional[str] = Form(None),
        success: Optional[str] = Form(None),
        conclusion: Optional[str] = Form(None),
        review_confirmed: bool = Form(False),
        acknowledge_anomaly: bool = Form(False)):
    """Atomically save a user-reviewed screenshot replacement and its metadata.

    Selecting a file in the browser does not call this endpoint. The browser keeps
    the new screenshots as local previews until the user presses Save, then sends
    the screenshots and form fields together. Recognition logic is intentionally
    untouched: a manual replacement is final user input and is not re-scanned.
    """
    if not files:
        raise HTTPException(400, '请先选择截图')
    if len(files) > 120:
        raise HTTPException(400, '一次最多上传 120 张截图')
    if success is not None and success not in ('是', '查询失败', ''):
        raise HTTPException(400, '查询状态无效')
    if date_value:
        try:
            date.fromisoformat(date_value)
        except ValueError:
            raise HTTPException(400, '日期无效')
    if conclusion is not None and len(conclusion) > 4000:
        raise HTTPException(400, '结论文字过长')

    rd = root(tid)
    key = action_key(did, uid)
    from PIL import Image
    prepared = []
    for f in files:
        ext = Path(f.filename or '').suffix.lower()
        if ext not in IMAGE_EXTS:
            raise HTTPException(400, '只接受图片文件')
        data = await f.read(35 * 1024 * 1024 + 1)
        if len(data) > 35 * 1024 * 1024:
            raise HTTPException(413, '单张图片超过 35 MB')
        try:
            with Image.open(io.BytesIO(data)) as probe:
                probe.verify()
        except Exception:
            raise HTTPException(400, '上传的文件无法作为图片读取')
        digest = hashlib.sha256(data).hexdigest()
        name = hashlib.sha256(data + key.encode()).hexdigest()[:32] + ext
        prepared.append((name, digest, shortname(f.filename), data))

    _user_write_begin(tid)
    try:
        # File writes happen only after every selected file has passed validation.
        for name, _digest, _filename, data in prepared:
            (rd / 'uploads' / name).write_bytes(data)
        with lock(tid):
            m = load(tid)
            _d, it = find_item(m, did, uid)
            names = []
            hashes = []
            for name, digest, filename, _data in prepared:
                u = next((x for x in m.get('uploads', []) if x.get('file') == name), None)
                if not u:
                    u = {'file': name, 'hash': digest, 'filename': filename,
                         'review': None, 'review_for': key, 'suggestion': {
                             'doc_id': did, 'uid': uid, 'company': _d['company'],
                             'name': it['name'], 'no': it['no'], 'reason': '用户确认替换'},
                         'scan_state': 'manual_final', 'manual_final': True,
                         'used': False, 'logic_version': vision.LOGIC_VERSION}
                    m.setdefault('uploads', []).append(u)
                else:
                    if u.get('review_for') and u['review_for'] != key:
                        raise HTTPException(409, '这张截图已对应其他项目，请在当前项目重新选择')
                    u.update({'hash': digest, 'filename': filename, 'review_for': key,
                              'manual_final': True, 'scan_state': 'manual_final',
                              'review': None, 'logic_version': vision.LOGIC_VERSION})
                names.append(name)
                hashes.append(digest)

            val = {'kind': 'replace', 'images': names,
                   'review_confirmed': True, 'manual_final': True,
                   'acknowledge_anomaly': bool(acknowledge_anomaly),
                   'image_hashes': hashes, 'confirmed_at': now(),
                   'logic_version': vision.LOGIC_VERSION, 'confirmed_by': 'user'}
            if date_value:
                val['date'] = date_value
            if success is not None:
                val['success'] = success
            if conclusion is not None:
                val['conclusion'] = conclusion
            m.setdefault('actions', {})[key] = val
            used = {f for a in m['actions'].values() for f in a.get('images', [])}
            for u in m.get('uploads', []):
                u['used'] = u.get('file') in used
            save(m, True)
    finally:
        _user_write_end(tid)
    return {'ok': True, 'images': names}


@app.get('/api/tasks/{tid}/upload-status/{filename}')
def upload_status(tid: str, filename: str):
    with lock(tid):
        m = load(tid)
        u = next((x for x in m.get('uploads', []) if x.get('file') == filename), None)
        if not u:
            raise HTTPException(404, '截图不存在')
        result = {k: v for k, v in u.items() if k != 'review'}
        result['review'] = _public_bundle(u.get('review'))
        return result


class Action(BaseModel):
    kind: str
    images: list[str] = Field(default_factory=list)
    date: Optional[str] = None
    success: Optional[str] = None
    conclusion: Optional[str] = None
    review_confirmed: bool = False
    manual_final: bool = False
    acknowledge_anomaly: bool = False


@app.put('/api/tasks/{tid}/items/{did}/{uid}')
def update_item(tid: str, did: str, uid: str, value: Action):
    if value.kind not in {'replace', 'keep', 'metadata', 'exclude', 'reset'}:
        raise HTTPException(400, '处理方式无效')
    rescans = []
    _user_write_begin(tid)
    try:
        with lock(tid):
            m = load(tid)
            d, it = find_item(m, did, uid)
            key = action_key(did, uid)
            if value.kind == 'reset':
                m['actions'].pop(key, None)
            else:
                val = value.model_dump(exclude_none=True)
                if value.kind == 'replace' and not val['images']:
                    raise HTTPException(400, '请先上传截图')
                if value.kind != 'replace' and val['images']:
                    raise HTTPException(400, '仅替换截图操作可以携带图片')
                if value.kind in ('keep', 'metadata') and not it['images'] and not val['images']:
                    raise HTTPException(400, '该项原稿缺图，不能确认保留')
                if it.get('new') and value.kind == 'exclude':
                    raise HTTPException(400, '请在设置中删除这个新增网站')
                if value.success is not None and value.success not in ('是', '查询失败', ''):
                    raise HTTPException(400, '查询状态无效')
                if value.date is not None:
                    try:
                        date.fromisoformat(value.date)
                    except ValueError:
                        raise HTTPException(400, '日期无效')
                if value.conclusion is not None and len(value.conclusion) > 4000:
                    raise HTTPException(400, '结论文字过长')
                for fn in val['images']:
                    safe_child(root(tid) / 'uploads', fn)
                if value.kind == 'replace':
                    for fn in val['images']:
                        u = next((x for x in m['uploads'] if x['file'] == fn), None)
                        if not u:
                            continue
                        if u.get('review_for') and u['review_for'] != key:
                            raise HTTPException(409, '这张截图已对应其他项目，请在当前项目重新上传')
                        u['review_for'] = key
                        u['manual_final'] = True
                        u['scan_state'] = 'manual_final'
                        u['review'] = None
                        u['logic_version'] = vision.LOGIC_VERSION
                    # A replacement image is the user's final human correction. It is
                    # written straight into Word; automatic triad recognition must not
                    # inspect or second-guess it afterwards.
                    val['manual_final'] = True
                    val['review_confirmed'] = True
                chosen = _effective_images(m, it, val) if value.kind == 'replace' else it.get('images', [])
                if value.review_confirmed or value.kind == 'replace':
                    hashes = [im.get('hash') for im in chosen]
                    if not hashes or not all(hashes):
                        raise HTTPException(400, '截图尚未准备完整，不能确认')
                    if any(im.get('error') for im in chosen):
                        raise HTTPException(400, '无法读取的图片不能确认通过')
                    val['image_hashes'] = hashes
                    val['confirmed_at'] = now()
                val['logic_version'] = vision.LOGIC_VERSION
                val['confirmed_by'] = 'user'
                m['actions'][key] = val
            used = {f for a in m['actions'].values() for f in a.get('images', [])}
            for u in m['uploads']:
                u['used'] = u['file'] in used
            save(m, True)
    finally:
        _user_write_end(tid)
    # Replacement screenshots are manual-final and intentionally never rescanned.
    return {'ok': True}

@app.post('/api/tasks/{tid}/confirm-unchanged')
def confirm_unchanged(tid: str):
    """Explicit sign-off of unflagged original screenshots; no text rewriting."""
    with lock(tid):
        m = load(tid)
        n = 0
        for d in m['docs']:
            for i in d['items']:
                key = action_key(d['id'], i['uid'])
                if key in m['actions']:
                    continue
                issues, images = effective(m, i, None)
                if images and _triad_ok({'state': 'reviewed', 'evidence': {
                        k: ('pass' if all((im.get('review') or {}).get('evidence', {}).get(k) == 'pass'
                                          for im in images) else 'x') for k in TRIAD_KEYS}}) and not issues:
                    m['actions'][key] = {'kind': 'keep', 'images': [],
                                         'logic_version': vision.LOGIC_VERSION}
                    n += 1
        save(m, True)
    return {'confirmed': n}


class Generate(BaseModel):
    allow_draft: bool = False


def _export_actions(m, d):
    rd = TASKS / m['id']
    actions = {}
    for it in d['items']:
        a = m['actions'].get(action_key(d['id'], it['uid']))
        if a:
            a = deepcopy(a)
            a['images'] = [str(rd / 'uploads' / f) for f in a.get('images', [])]
            actions[it['uid']] = a
    return actions


def generate_impl(tid, allow_draft):
    with lock(tid):
        m = load(tid)
        pending = unresolved(m)
        version = m['version']
    if pending and not allow_draft:
        raise HTTPException(409, {'message': '还有未确认项目', 'pending': len(pending),
                                  'items': [{'company': x[2], 'no': x[3], 'name': x[4]} for x in pending]})
    if m['scan']['state'] == 'working':
        raise HTTPException(409, '图片检查尚未结束，请稍候')
    rd = root(tid)
    outdir = rd / 'outputs' / ('batch-' + str(version) + '-' + uuid.uuid4().hex[:8])
    outdir.mkdir(parents=True, exist_ok=True)
    docs = []
    used_names = set()
    for d in m['docs']:
        actions = _export_actions(m, d)
        fn = d['filename']
        if fn in used_names:
            fn = d['id'] + '_' + fn
        used_names.add(fn)
        if pending:
            fn = Path(fn).stem + '_待核.docx'
        dest = outdir / fn
        stats = export_doc(Path(d['source']), dest, actions, m['settings']['date'],
                           extra_sites=m['settings'].get('extra_sites', []), finalize=not bool(pending))
        docs.append({'id': d['id'], 'company': d['company'], 'filename': fn, 'path': str(dest),
                     'summary': {'items': stats['items'], 'changed': len(stats['changes']),
                                 'checks': len(stats['checks'])},
                     'structural_pass': stats['structural_pass']})
    archive = outdir / ('待核Word.zip' if pending else '整理完成Word.zip')
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as z:
        for d in docs:
            z.write(d['path'], d['filename'])
    with lock(tid):
        current = load(tid)
        if current['version'] != version:
            raise HTTPException(409, '生成期间内容发生变化，请重新生成')
        current['generation'] = {'docs': docs, 'zip_path': str(archive), 'draft': bool(pending),
                                 'pending': len(pending), 'at': now(), 'version': version}
        save(current)
    return {'ok': True, 'draft': bool(pending)}


@app.post('/api/tasks/{tid}/generate')
async def generate(tid: str, value: Generate):
    return await run_in_threadpool(generate_impl, tid, value.allow_draft)


def generate_one_impl(tid, did):
    with lock(tid):
        m = load(tid)
        d = next((x for x in m['docs'] if x['id'] == did), None)
        if d is None:
            raise HTTPException(404, '公司不存在')
        version = m['version']
        view = next(x for x in decorate(m)['docs'] if x['id'] == did)
        pending = sum(int(view['counts'].get(k, 0)) for k in ('needs', 'unread', 'scanning'))
    if m['scan']['state'] == 'working':
        raise HTTPException(409, '图片检查尚未结束，请稍候')
    rd = root(tid)
    outdir = rd / 'outputs' / ('single-' + str(version) + '-' + uuid.uuid4().hex[:8])
    outdir.mkdir(parents=True, exist_ok=True)
    actions = _export_actions(m, d)
    fn = d['filename']
    if pending:
        fn = Path(fn).stem + '_待核.docx'
    dest = outdir / fn
    stats = export_doc(Path(d['source']), dest, actions, m['settings']['date'],
                       extra_sites=m['settings'].get('extra_sites', []), finalize=not bool(pending))
    with lock(tid):
        current = load(tid)
        if current['version'] != version:
            raise HTTPException(409, '生成期间内容发生变化，请重新生成')
        current['single_generation'] = {'doc_id': did, 'path': str(dest), 'filename': fn,
                                        'pending': pending, 'at': now(), 'version': version,
                                        'structural_pass': stats['structural_pass']}
        save(current)
    return {'ok': True, 'pending': pending, 'url': f'/api/tasks/{tid}/download-one/{did}?v={version}'}


@app.post('/api/tasks/{tid}/generate-one/{did}')
async def generate_one(tid: str, did: str):
    return await run_in_threadpool(generate_one_impl, tid, did)


@app.get('/api/tasks/{tid}/download-one/{did}')
def download_one(tid: str, did: str, v: int):
    m = load(tid)
    g = m.get('single_generation')
    if not g or g.get('doc_id') != did or g.get('version') != v or m['version'] != v:
        raise HTTPException(410, '文件已过期，请重新生成')
    return FileResponse(g['path'], filename=g['filename'],
                        media_type='application/vnd.openxmlformats-officedocument.wordprocessingml.document')


@app.get('/api/tasks/{tid}/download/{did}')
def download(tid: str, did: str, v: int):
    m = load(tid)
    g = m.get('generation')
    if not g or g['version'] != v or m['version'] != v:
        raise HTTPException(410, '文件已过期，请重新生成')
    d = next((d for d in g['docs'] if d['id'] == did), None)
    if not d:
        raise HTTPException(404)
    return FileResponse(d['path'], filename=d['filename'],
                        media_type='application/vnd.openxmlformats-officedocument.wordprocessingml.document')


@app.get('/api/tasks/{tid}/download-all')
def download_all(tid: str, v: int):
    m = load(tid)
    g = m.get('generation')
    if not g or g['version'] != v or m['version'] != v:
        raise HTTPException(410, '文件已过期，请重新生成')
    return FileResponse(g['zip_path'], filename=Path(g['zip_path']).name)


@app.on_event('shutdown')
def stop_scans_on_shutdown():
    # scan_worker.py lives in a new process group; without an explicit shutdown
    # cleanup it can outlive uvicorn and keep Apple Vision busy after a restart.
    _cancel_all_scans(reason='app_shutdown')


@app.on_event('startup')
def resume():
    _prune_tasks()
    for f in TASKS.glob('*/task.json'):
        try:
            m = json.loads(f.read_text('utf-8'))
            stale = m.get('scan', {}).get('logic_version') != vision.LOGIC_VERSION
            if stale:
                m['generation'] = None
                m['single_generation'] = None
                m['version'] = m.get('version', 0) + 1
                for d in m.get('docs', []):
                    for i in d.get('items', []):
                        for im in i.get('images', []):
                            im.pop('review', None)
                for u in m.get('uploads', []):
                    u['review'] = None
                    u['logic_version'] = None
            if m.get('scan', {}).get('state') == 'working' or stale:
                m.setdefault('scan', {})['state'] = 'working'
                m['scan']['checked'] = 0
                m['scan']['total'] = _scan_total(m)
                m['scan']['engine'] = vision.available()
                m['scan']['logic_version'] = vision.LOGIC_VERSION
                save(m)
                _launch_scan(m['id'], reason='startup_resume')
        except Exception:
            pass


if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='127.0.0.1', port=int(os.environ.get('PORT', '8766')), log_level='warning')
