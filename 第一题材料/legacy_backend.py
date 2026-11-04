"""Exam legacy prototype. Virtual planning only; no real hardware calls.
Its structure and choices may be replaced. Python 3.10+, standard library.
The proposed six network services are NOT implemented or required here.
"""
import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3


def load_case(path):
    case = json.loads(Path(path).read_text(encoding='utf-8'))
    seen = {}
    for station, spec in case['stations'].items():
        if not isinstance(station, str) or type(spec['cycle_s']) is not int or spec['cycle_s'] <= 0:
            raise ValueError('Invalid station definition')
    for job in case['jobs']:
        if (not isinstance(job['job_id'], str) or not job['job_id']
                or job['station_id'] not in case['stations']
                or type(job['ready_s']) is not int or job['ready_s'] < 0):
            raise ValueError('Invalid job')
        if job['job_id'] in seen and seen[job['job_id']] != job:
            raise ValueError('Same job_id with conflicting payload')
        seen[job['job_id']] = job
    return case


class TaskRepository:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute('CREATE TABLE IF NOT EXISTS meta(value TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS jobs(job_id TEXT PRIMARY KEY, station_id TEXT, ready_s INTEGER, start_s INTEGER, end_s INTEGER)')

    def bind(self, case):
        signature = json.dumps(case, sort_keys=True)
        row = self.db.execute('SELECT value FROM meta').fetchone()
        if row and row[0] != signature:
            raise ValueError('Case changed: use a different database for a different experiment')
        if not row:
            self.db.execute('INSERT INTO meta VALUES(?)', (signature,))
        self.db.commit()

    def exists(self, job_id):
        return self.db.execute('SELECT 1 FROM jobs WHERE job_id=?', (job_id,)).fetchone() is not None

    def end_of_reserved_window(self):
        return self.db.execute('SELECT COALESCE(MAX(end_s),0) FROM jobs').fetchone()[0]

    def insert(self, job, start, end):
        self.db.execute('INSERT INTO jobs VALUES(?,?,?,?,?)',
                        (job['job_id'], job['station_id'], job['ready_s'], start, end))
        self.db.commit()

    def rows(self):
        self.db.row_factory = sqlite3.Row
        return [dict(r) for r in self.db.execute('SELECT * FROM jobs ORDER BY start_s,job_id')]


@dataclass
class Envelope:
    route: str
    payload: dict


class LocalBus:
    def __init__(self):
        self.routes = {}

    def register(self, route, handler):
        self.routes[route] = handler

    def send(self, envelope):
        return self.routes[envelope.route](envelope.payload)


class ExecutionWindowPolicy:
    def __init__(self, repo):
        self.repo = repo

    def earliest(self, job):
        return max(job['ready_s'], self.repo.end_of_reserved_window())


class TaskPlanningService:
    def __init__(self, repo, policy, stations):
        self.repo, self.policy, self.stations = repo, policy, stations

    def accept(self, job):
        if self.repo.exists(job['job_id']):
            return False
        start = self.policy.earliest(job)
        self.repo.insert(job, start, start + self.stations[job['station_id']]['cycle_s'])
        return True


class DispatchFacade:
    def __init__(self, bus):
        self.bus = bus

    def dispatch(self, job):
        return self.bus.send(Envelope('planning', dict(job)))


class ResultProjection:
    def __init__(self, repo):
        self.repo = repo

    def export(self, destination):
        Path(destination).write_text(json.dumps({'runs': self.repo.rows()}, indent=2), encoding='utf-8')


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--case', required=True)
    p.add_argument('--db', required=True)
    p.add_argument('--out', required=True)
    p.add_argument('--max-new', type=int, default=None)
    a = p.parse_args()
    case = load_case(a.case)
    repo = TaskRepository(a.db)
    repo.bind(case)
    service = TaskPlanningService(repo, ExecutionWindowPolicy(repo), case['stations'])
    bus = LocalBus()
    bus.register('planning', service.accept)
    facade = DispatchFacade(bus)
    count = 0
    for job in sorted(case['jobs'], key=lambda j: j['ready_s']):
        if a.max_new is not None and count >= a.max_new:
            break
        count += int(facade.dispatch(job))
    ResultProjection(repo).export(a.out)
    repo.db.close()


if __name__ == '__main__':
    main()
