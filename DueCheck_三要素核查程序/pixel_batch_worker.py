"""Disposable pixel/map batch worker for DueCheck.

It deliberately never runs Apple Vision OCR. A small batch amortises Python/OpenCV
startup cost, then the parent kills the whole process if native feature matching ever
stalls. The process is recycled after each batch, preventing native-state buildup
across a 100-200 screenshot job.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def main(job_path: str, out_path: str) -> int:
    payload = json.loads(Path(job_path).read_text('utf-8'))
    import vision
    rows = []
    for job in payload.get('items') or []:
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
            rows.append({'ok': True, 'bundle': bundle})
        except Exception as exc:
            rows.append({'ok': False, 'error': str(exc)[:240]})
    Path(out_path).write_text(json.dumps({'rows': rows}, ensure_ascii=False), 'utf-8')
    return 0


if __name__ == '__main__':
    if len(sys.argv) != 3:
        raise SystemExit('usage: pixel_batch_worker.py <job.json> <out.json>')
    raise SystemExit(main(sys.argv[1], sys.argv[2]))
