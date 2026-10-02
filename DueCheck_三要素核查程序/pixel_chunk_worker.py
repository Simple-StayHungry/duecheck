"""Short-lived progressive pixel worker for DueCheck.

A clean supervisor launches this worker for a small chunk of screenshots.  The
worker imports the native image stack only inside this disposable process and
writes one JSONL result after every screenshot.  If one native call wedges, the
supervisor can kill this worker without losing results already completed in the
same chunk.

    python pixel_chunk_worker.py <job.json> <rows.jsonl> <status.json>
"""
from __future__ import annotations

import json
import os
import tempfile
import sys
from pathlib import Path


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


def main(job_path: str, rows_path: str, status_path: str) -> int:
    jobs = (json.loads(Path(job_path).read_text('utf-8')).get('items') or [])
    rows_file = Path(rows_path)
    status_file = Path(status_path)

    # Import native/OpenCV code only after the disposable process has started.
    import vision

    with rows_file.open('w', encoding='utf-8', buffering=1) as out:
        for index, job in enumerate(jobs):
            _atomic_json(status_file, {'index': index, 'state': 'working'})
            try:
                bundle = vision.observe(
                    Path(job['path']),
                    job.get('company') or '',
                    job.get('item') or {},
                    Path(job['cache_dir']) if job.get('cache_dir') else None,
                    identity_codes=job.get('identity_codes') or [],
                    allow_second_read=False,
                    enable_ocr=False,
                )
                row = {'index': index, 'ok': True, 'bundle': bundle}
            except Exception as exc:
                row = {'index': index, 'ok': False, 'error': str(exc)[:240]}
            out.write(json.dumps(row, ensure_ascii=False) + '\n')
            out.flush()
            _atomic_json(status_file, {'index': index, 'state': 'done'})

    _atomic_json(status_file, {'index': len(jobs), 'state': 'complete'})
    return 0


if __name__ == '__main__':
    if len(sys.argv) != 4:
        raise SystemExit('usage: pixel_chunk_worker.py <job.json> <rows.jsonl> <status.json>')
    raise SystemExit(main(sys.argv[1], sys.argv[2], sys.argv[3]))
