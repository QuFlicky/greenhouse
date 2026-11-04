"""Validate exported virtual runs, not architecture or physical execution.
Usage: python3 check_result.py case.json result.json
Only the stdlib is required. This checker also supports the offline card.
"""
import argparse
import json
import math
from pathlib import Path


def evaluate(case, report):
    if not isinstance(report, dict):
        return {'valid': False, 'errors': ['result must be an object']}
    jobs, errors = {}, []
    for job in case['jobs']:
        if job['job_id'] in jobs and jobs[job['job_id']] != job:
            raise ValueError('Conflicting input job_id; reject this case before planning')
        jobs[job['job_id']] = job
    runs = report.get('runs')
    if not isinstance(runs, list):
        return {'valid': False, 'errors': ['runs must be an array']}
    used, intervals, valid_runs = set(), {}, []
    def reserve(resource, start, end, job_id):
        intervals.setdefault(resource, []).append((start, end, job_id))
    for index, row in enumerate(runs):
        if not isinstance(row, dict):
            errors.append(f'runs[{index}] must be an object')
            continue
        jid = row.get('job_id')
        if not isinstance(jid, str) or not jid:
            errors.append(f'runs[{index}].job_id must be a nonempty string')
            continue
        if jid not in jobs or jid in used:
            errors.append(f'Unknown or repeated job_id: {jid}')
            continue
        used.add(jid)
        job = jobs[jid]
        if row.get('station_id') != job['station_id']:
            errors.append(f'{jid}: changed station assignment')
            continue
        spec = case['stations'][job['station_id']]
        start, end = row.get('start_s'), row.get('end_s')
        if any(type(v) not in (int, float) or (type(v) is float and not math.isfinite(v)) for v in (start, end)):
            errors.append(f'{jid}: times must be finite numbers')
            continue
        if not 0 <= start <= end <= 2**63-1:
            errors.append(f'{jid}: times outside supported range')
            continue
        if start < job['ready_s'] or abs(end - start - spec['cycle_s']) > 1e-7:
            errors.append(f'{jid}: readiness or cycle length violated')
            continue
        reserve('station:' + job['station_id'], start, end, jid)
        if spec.get('pump_id'):
            fill = spec['fill_s']
            if not 0 < fill <= spec['cycle_s']:
                raise ValueError('Invalid filling phase')
            reserve('pump:' + spec['pump_id'], start, start + fill, jid)
        valid_runs.append(row)
    missing = sorted(set(jobs) - used)
    if missing:
        errors.append(f'Missing jobs: {len(missing)}')
    for resource, windows in intervals.items():
        previous_end = -1
        previous_id = None
        for start, end, jid in sorted(windows):
            if start < previous_end - 1e-7:
                errors.append(f'{resource}: overlap between {previous_id} and {jid}')
            if end > previous_end:
                previous_end, previous_id = end, jid
    horizon = case['report_horizon_s']
    completed = sum(0 <= row['end_s'] <= horizon for row in valid_runs)
    busy = {s: 0 for s in case['stations']}
    for row in valid_runs:
        busy[row['station_id']] += max(0, min(row['end_s'], horizon) - max(0, row['start_s']))
    return {'valid': not errors, 'errors': errors, 'unique_jobs': len(jobs),
            'planned_jobs': len(runs), 'completed_within_horizon': completed,
            'horizon_s': horizon, 'last_completion_s': max((r['end_s'] for r in valid_runs), default=0),
            'station_utilization_within_horizon': {s: round(v / horizon, 4) for s, v in busy.items()},
            'note': 'Virtual replay metrics; validation does not grade problem selection or production readiness'}


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('case')
    p.add_argument('result')
    a = p.parse_args()
    try:
        result = evaluate(json.loads(Path(a.case).read_text()), json.loads(Path(a.result).read_text()))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        result = {'valid': False, 'errors': [str(exc)]}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result['valid'] else 1)
