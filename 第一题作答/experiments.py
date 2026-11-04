"""Reproducible controlled experiment and separate-process continuation evidence."""
import argparse
from collections import Counter
import json
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
MATERIAL = HERE.parent / '第一题材料'
sys.path.insert(0, str(MATERIAL))
import legacy_backend as legacy
from check_result import evaluate
from backend import Backend, write_json


def command(argv):
    result = subprocess.run([sys.executable, *map(str, argv)], capture_output=True, text=True, encoding='utf-8')
    record = {'argv': [sys.executable, *map(str, argv)], 'exit_code': result.returncode,
              'stdout': result.stdout, 'stderr': result.stderr}
    if result.returncode:
        raise RuntimeError(record)
    return record


def corrected_legacy(case_path, db, out):
    # The sole intervention: MAX(end_s) is scoped to the assigned station.
    class StationPolicy(legacy.ExecutionWindowPolicy):
        def earliest(self, job):
            tail = self.repo.db.execute('SELECT COALESCE(MAX(end_s),0) FROM jobs WHERE station_id=?',
                                        (job['station_id'],)).fetchone()[0]
            return max(job['ready_s'], tail)
    case = legacy.load_case(case_path)
    repo = legacy.TaskRepository(db)
    repo.bind(case)
    service = legacy.TaskPlanningService(repo, StationPolicy(repo), case['stations'])
    bus = legacy.LocalBus()
    bus.register('planning', service.accept)
    facade = legacy.DispatchFacade(bus)
    for job in sorted(case['jobs'], key=lambda j: j['ready_s']):
        facade.dispatch(job)
    legacy.ResultProjection(repo).export(out)
    repo.db.close()


def wait_metrics(case, report):
    jobs = {j['job_id']: j for j in case['jobs']}
    rows = sorted(report['runs'], key=lambda r: (r['start_s'], r['job_id']))
    tail = {s: 0 for s in case['stations']}
    waits, avoidable = [], 0
    for row in rows:
        ready = jobs[row['job_id']]['ready_s']
        waits.append(row['start_s'] - ready)
        # For this ready-sorted experiment: delay beyond readiness and this station's previous completion.
        avoidable += row['start_s'] - max(ready, tail[row['station_id']])
        tail[row['station_id']] = row['end_s']
    return {'total_wait_s': sum(waits), 'mean_wait_s': statistics.mean(waits),
            'max_wait_s': max(waits), 'delay_beyond_ready_and_own_station_tail_s': avoidable,
            'interpretation': 'virtual schedule waits, not measured message processing latency; tail comparison assumes this ready-sorted batch'}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', default=str(HERE / 'evidence'))
    args = parser.parse_args()
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    case_path = MATERIAL / 'case.json'
    case = json.loads(case_path.read_text(encoding='utf-8'))
    with tempfile.TemporaryDirectory(prefix='assessment-experiment-') as folder:
        tmp = Path(folder)
        commands = []
        commands.append(command([MATERIAL/'legacy_backend.py','--case',case_path,'--db',tmp/'baseline.db','--out',out/'baseline.json']))
        corrected_legacy(case_path, tmp/'controlled.db', out/'controlled.json')
        # Each command below starts a new process; all share the SAME resume database.
        db = tmp/'resume.db'
        for name, extra in [('checkpoint.json',['--max-new','20']), ('result.json',[]), ('reimport.json',[])]:
            commands.append(command([HERE/'backend.py','--case',case_path,'--db',db,'--out',out/name,*extra]))
        reports = {n: json.loads((out/(n+'.json')).read_text(encoding='utf-8'))
                   for n in ('baseline','controlled','checkpoint','result','reimport')}
        validations = {n: evaluate(case,reports[n]) for n in ('baseline','controlled','result','reimport')}
        assert all(v['valid'] for v in validations.values())
        by_id = lambda report: {r['job_id']: r for r in report['runs']}
        checkpoint, final, retry = (by_id(reports[n]) for n in ('checkpoint','result','reimport'))
        stable = all(final[j] == row for j,row in checkpoint.items())
        assert len(checkpoint) == 20 and len(final) == len(retry) == 240 and stable and final == retry
        fields = ('job_id','station_id','start_s','end_s')
        normalized = lambda report: sorted(tuple(r[k] for k in fields) for r in report['runs'])
        assert normalized(reports['controlled']) == normalized(reports['result'])
        # Counterexample: later-ready reservation arrives before an earlier-ready job.
        gap_case = {'report_horizon_s':3600,'stations':{'S1':{'cycle_s':40}},'jobs':[
            {'job_id':'late','station_id':'S1','ready_s':1000},
            {'job_id':'early','station_id':'S1','ready_s':0}]}
        write_json(out/'counterexample_case.json', gap_case)
        backend = Backend(out/'counterexample_case.json', tmp/'gap.db')
        responses = [backend.handle({'op':'submit','job':j}) for j in gap_case['jobs']]
        gap = backend.handle({'op':'report','at_s':3600})
        backend.close()
        assert responses[1]['run']['start_s'] == 0
        write_json(out/'counterexample_result.json',gap)
        counter = {'input_submission_order':['late','early'], 'wrong_tail_only_early_start_s':1040,
                   'actual_early_start_s':0, 'late_plan_unchanged':responses[0]['run']==by_id(gap)['late'],
                   'validation':evaluate(gap_case,gap),
                   'failure': 'station tail is not earliest feasible start when an existing reservation is in the future; tail-only wastes the [0,1000) gap'}
        assert counter['validation']['valid'] and counter['late_plan_unchanged']
        write_json(out/'counterexample.json',counter)
        # Preserve the actual public CLI output, not only imported evaluator results.
        for name in ('baseline','controlled','result'):
            check = command([MATERIAL/'check_result.py',case_path,out/(name+'.json')])
            commands.append(check)
            (out/(name+'_check.txt')).write_text(check['stdout'],encoding='utf-8')
        platform = command([MATERIAL/'check_platform.py','--adapter',HERE/'adapter.json','--case',case_path,'--out',out/'platform_evidence.json'])
        commands.append(platform)
        summary = {'case': {'deliveries':len(case['jobs']), 'unique_jobs':len(final),
                            'station_job_counts':dict(Counter(j['station_id'] for j in {j['job_id']:j for j in case['jobs']}.values())),
                            'ready_s_values':sorted({j['ready_s'] for j in case['jobs']})},
                   'controlled_change':'Only ExecutionWindowPolicy.earliest scopes MAX(end_s) by station_id; same local bus, facade, repository, input order, cycles, hardware and horizon',
                   'metrics':validations,
                   'waits':{n:wait_metrics(case,reports[n]) for n in ('baseline','controlled','result')},
                   'controlled_matches_final':True,
                   'continuation':{'kind':'normal process exit and restart; no injected crash',
                                   'counts':[len(checkpoint),len(final),len(retry)],'old_plans_unchanged':stable,
                                   'all_plans_unchanged_on_reimport':final==retry,
                                   'event_counts':[reports[n]['metrics']['event_counts'] for n in ('checkpoint','result','reimport')]},
                   'dependencies':{'baseline':'Python standard library + SQLite; no network services',
                                   'controlled':'same as baseline','final':'same; adapter starts one local process per request'},
                   'operations':'No service startup. Baseline one batch command; controlled experiment one script; final one batch command, or three commands to demonstrate continuation. Fresh temp DB for each experiment arm; resume arm reuses its DB without deletion.',
                   'commands':commands}
        write_json(out/'experiment.json',summary)
        print(json.dumps({'completed':{n:v['completed_within_horizon'] for n,v in validations.items()},
                          'continuation':summary['continuation'],'platform':json.loads((out/'platform_evidence.json').read_text())['passed']},indent=2))


if __name__ == '__main__':
    main()
