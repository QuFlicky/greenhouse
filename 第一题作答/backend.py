"""Fixed-batch virtual scheduler. Standard library only; no device execution."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sqlite3

MAX_TIME = 2**63 - 1


class InputError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def require(condition, code, message):
    if not condition:
        raise InputError(code, message)


def seconds(value, name, positive=False):
    require(type(value) is int and (1 if positive else 0) <= value <= MAX_TIME,
            'invalid_time', f'{name} must be an integer in [{1 if positive else 0}, {MAX_TIME}]')
    return value


def job_payload(value, stations):
    require(isinstance(value, dict) and set(value) == {'job_id', 'station_id', 'ready_s'},
            'invalid_job', 'job requires exactly job_id, station_id, ready_s')
    require(isinstance(value['job_id'], str) and bool(value['job_id']), 'invalid_job', 'job_id must be nonempty')
    require(isinstance(value['station_id'], str) and value['station_id'] in stations,
            'invalid_station', 'station_id must name a configured station')
    seconds(value['ready_s'], 'ready_s')
    return dict(value)


def load_case(path):
    case = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    require(isinstance(case, dict), 'invalid_case', 'case must be an object')
    seconds(case.get('report_horizon_s'), 'report_horizon_s', True)
    stations = case.get('stations')
    require(isinstance(stations, dict) and bool(stations), 'invalid_case', 'stations must be nonempty')
    for name, spec in stations.items():
        require(isinstance(name, str) and bool(name) and isinstance(spec, dict), 'invalid_station', 'invalid station definition')
        require(set(spec) == {'cycle_s'}, 'unsupported_resource', 'only independent stations with cycle_s are supported')
        seconds(spec['cycle_s'], 'cycle_s', True)
    require(isinstance(case.get('jobs'), list), 'invalid_case', 'jobs must be an array')
    unique = {}
    for raw in case['jobs']:
        job = job_payload(raw, stations)
        require(job['job_id'] not in unique or unique[job['job_id']] == job,
                'case_conflict', f"conflicting batch job: {job['job_id']}")
        require(job['ready_s'] <= MAX_TIME - stations[job['station_id']]['cycle_s'],
                'time_overflow', 'job cannot finish within supported time range')
        unique[job['job_id']] = job
    return case, unique


def earliest_gap(rows, ready, cycle):
    start = ready
    blockers = []
    for row in rows:
        if row['end_s'] <= start:
            continue
        if start + cycle <= row['start_s']:
            break
        blockers.append(row['job_id'])
        start = row['end_s']
    require(start <= MAX_TIME - cycle, 'time_overflow', 'planned end exceeds supported time range')
    return start, blockers


class Backend:
    def __init__(self, case_path, db_path):
        self.case, self.jobs = load_case(case_path)
        binding = {'stations': self.case['stations'], 'report_horizon_s': self.case['report_horizon_s'],
                   'jobs': sorted(self.jobs.values(), key=lambda j: j['job_id'])}
        self.signature = hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest()
        self.db = sqlite3.connect(db_path, timeout=30, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        try:
            self.db.execute('BEGIN IMMEDIATE')
            self.db.execute('CREATE TABLE IF NOT EXISTS meta (id INTEGER PRIMARY KEY CHECK(id=1), signature TEXT NOT NULL)')
            self.db.execute('CREATE TABLE IF NOT EXISTS jobs (job_id TEXT PRIMARY KEY, station_id TEXT NOT NULL, ready_s INTEGER NOT NULL, start_s INTEGER NOT NULL, end_s INTEGER NOT NULL)')
            self.db.execute('CREATE INDEX IF NOT EXISTS station_windows ON jobs(station_id,start_s)')
            self.db.execute('CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, event TEXT NOT NULL, detail TEXT NOT NULL)')
            saved = self.db.execute('SELECT signature FROM meta WHERE id=1').fetchone()
            require(saved is None or saved['signature'] == self.signature, 'case_mismatch', 'database belongs to a different batch; use a different database')
            if saved is None:
                self.db.execute('INSERT INTO meta VALUES(1,?)', (self.signature,))
            self.db.commit()
        except Exception:
            self.db.rollback()
            self.db.close()
            raise

    def close(self):
        self.db.close()

    def event(self, jid, kind, detail):
        self.db.execute('INSERT INTO events(job_id,event,detail) VALUES(?,?,?)',
                        (jid, kind, json.dumps(detail, sort_keys=True)))

    def events(self, jid=None):
        rows = self.db.execute('SELECT * FROM events' + (' WHERE job_id=?' if jid is not None else '') + ' ORDER BY seq',
                               (jid,) if jid is not None else ())
        return [dict(row, detail=json.loads(row['detail'])) for row in rows]

    def submit(self, raw):
        job = job_payload(raw, self.case['stations'])
        jid = job['job_id']
        require(jid in self.jobs, 'unknown_job', 'task is outside the fixed batch')
        if job != self.jobs[jid]:
            self.event(jid, 'conflict', {'submitted': job, 'expected': self.jobs[jid]})
            return {'status': 'conflict', 'code': 'payload_conflict', 'message': 'job_id has different parameters'}
        old = self.db.execute('SELECT * FROM jobs WHERE job_id=?', (jid,)).fetchone()
        if old:
            self.event(jid, 'duplicate', {'reason': 'identical payload; original plan retained'})
            return {'status': 'duplicate', 'run': dict(old)}
        rows = self.db.execute('SELECT * FROM jobs WHERE station_id=? ORDER BY start_s,job_id', (job['station_id'],))
        cycle = self.case['stations'][job['station_id']]['cycle_s']
        start, blockers = earliest_gap(rows, job['ready_s'], cycle)
        run = dict(job, start_s=start, end_s=start + cycle)
        self.db.execute('INSERT INTO jobs VALUES(?,?,?,?,?)', tuple(run[k] for k in ('job_id', 'station_id', 'ready_s', 'start_s', 'end_s')))
        self.event(jid, 'accepted', {'payload': job})
        self.event(jid, 'planned', {'record_kind': 'virtual_plan', 'wait_s': start-job['ready_s'],
                                  'reason': 'station_reservations' if blockers else 'ready',
                                  'blocking_job_ids': blockers, 'run': run})
        return {'status': 'accepted', 'run': run}

    def handle(self, request):
        try:
            require(isinstance(request, dict), 'invalid_request', 'request must be an object')
            op = request.get('op')
            require(isinstance(op, str) and op in ('submit', 'get', 'report'), 'invalid_operation', 'op must be submit, get or report')
            allowed = {'submit': {'op','job'}, 'get': {'op','job_id','at_s'}, 'report': {'op','at_s'}}[op]
            require(set(request) <= allowed, 'invalid_request', 'unexpected request fields')
            if op != 'submit':
                at = seconds(request.get('at_s', 0), 'at_s')
            # Acquire the write lock BEFORE any deduplication or scheduling reads.
            self.db.execute('BEGIN IMMEDIATE' if op == 'submit' else 'BEGIN')
            if op == 'submit':
                result = self.submit(request.get('job'))
            elif op == 'get':
                jid = request.get('job_id')
                require(isinstance(jid, str) and bool(jid), 'invalid_job', 'job_id must be nonempty')
                row = self.db.execute('SELECT * FROM jobs WHERE job_id=?', (jid,)).fetchone()
                if row is None:
                    result = {'status': 'not_found'}
                else:
                    state = ('not_ready' if at < row['ready_s'] else 'waiting' if at < row['start_s']
                             else 'running' if at < row['end_s'] else 'simulated_completed')
                    result = {'status': 'ok', 'state': state, 'run': dict(row),
                              'wait': {'wait_s': row['start_s']-row['ready_s']}, 'events': self.events(jid)}
            else:
                runs = [dict(r) for r in self.db.execute('SELECT * FROM jobs ORDER BY start_s,job_id')]
                events = self.events()
                result = {'status': 'ok', 'record_kind': 'virtual_plan_and_replay', 'runs': runs, 'events': events,
                          'metrics': {'planned_count': len(runs), 'simulated_completed_count': sum(r['end_s'] <= at for r in runs),
                                      'at_s': at, 'event_counts': dict(Counter(e['event'] for e in events))}}
            self.db.commit()
            return result
        except (InputError, sqlite3.Error) as exc:
            self.db.rollback()
            return {'status': 'error', 'code': getattr(exc, 'code', 'storage_error'), 'message': str(exc)}


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case', required=True)
    parser.add_argument('--db', required=True)
    parser.add_argument('--request')
    parser.add_argument('--response')
    parser.add_argument('--out')
    parser.add_argument('--max-new', type=int)
    args = parser.parse_args()
    backend = None
    try:
        require(bool(args.request) == bool(args.response), 'invalid_arguments', '--request and --response must be paired')
        require(bool(args.request) != bool(args.out), 'invalid_arguments', 'choose request/response or batch --out')
        require(args.max_new is None or args.max_new >= 0, 'invalid_arguments', '--max-new must be nonnegative')
        backend = Backend(args.case, args.db)
        if args.request:
            result = backend.handle(json.loads(Path(args.request).read_text(encoding='utf-8-sig')))
        else:
            count = 0
            for job in sorted(backend.case['jobs'], key=lambda j: j['ready_s']):
                if args.max_new is not None and count >= args.max_new:
                    break
                response = backend.handle({'op': 'submit', 'job': job})
                require(response['status'] in ('accepted','duplicate'), response.get('code','batch_error'), response.get('message','batch submission failed'))
                count += response['status'] == 'accepted'
            result = backend.handle({'op': 'report', 'at_s': backend.case['report_horizon_s']})
        write_json(args.response or args.out, result)
        if not args.request:
            print(json.dumps(result.get('metrics', result)))
        return 0 if args.request or result['status'] != 'error' else 1
    except (InputError, OSError, ValueError, sqlite3.Error) as exc:
        result = {'status': 'error', 'code': getattr(exc, 'code', 'invalid_input'), 'message': str(exc)}
        if args.response or args.out:
            write_json(args.response or args.out, result)
        else:
            print(json.dumps(result))
        return 0 if args.response else 1
    finally:
        if backend:
            backend.close()


if __name__ == '__main__':
    raise SystemExit(main())
