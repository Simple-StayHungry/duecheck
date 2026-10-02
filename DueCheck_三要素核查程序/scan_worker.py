"""Isolated one-screenshot DueCheck worker.

The parent web service launches one process per screenshot so any native
OpenCV/Apple-Vision stall is killable without freezing the whole batch.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def main(job_path: str, out_path: str) -> int:
    job = json.loads(Path(job_path).read_text('utf-8'))
    # Test-only delay used by the stability regression. It is inactive in normal
    # use and lets us prove that importing a new task cancels old workers rather
    # than leaving them behind.
    try:
        delay = float(os.environ.get('DUECHECK_SCAN_WORKER_TEST_DELAY', '0') or 0)
    except Exception:
        delay = 0
    if delay > 0:
        import time
        time.sleep(delay)
    # Apple Vision runs inline in this disposable process.  If the framework
    # stalls, the parent kills this entire process group; there is no orphaned
    # nested OCR worker left behind.
    os.environ['DUECHECK_OCR_INLINE'] = '1'
    import vision
    bundle = vision.observe(
        Path(job['path']),
        job.get('company') or '',
        job.get('item') or {},
        Path(job['cache_dir']) if job.get('cache_dir') else None,
        identity_codes=job.get('identity_codes') or [],
        allow_second_read=bool(job.get('allow_second_read', True)),
        enable_ocr=bool(job.get('enable_ocr', True)),
    )
    Path(out_path).write_text(json.dumps(bundle, ensure_ascii=False), 'utf-8')
    return 0


if __name__ == '__main__':
    if len(sys.argv) != 3:
        raise SystemExit('usage: scan_worker.py <job.json> <out.json>')
    raise SystemExit(main(sys.argv[1], sys.argv[2]))
